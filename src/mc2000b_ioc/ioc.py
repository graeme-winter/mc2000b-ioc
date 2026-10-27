"""pythonSoftIOC front end for the MC2000B.

Every writable parameter is a setpoint record ``NAME`` with a readback
``NAME_RBV``. Writes go straight to the unit and are followed by an
immediate readback poll; a rejected write sets ``LAST_ERROR`` and snaps the
setpoint back to the readback. Readbacks are refreshed every ``--poll``
seconds and go INVALID/COMM while the unit is unreachable; the IOC
reconnects on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from softioc import alarm, asyncio_dispatcher, builder, softioc

from . import __version__
from . import protocol as p
from .device import MC2000B

log = logging.getLogger(__name__)

BLADE_LABELS = tuple(b.name for b in p.Blade)


class ChopperIOC:
    def __init__(self, dev: MC2000B, poll_period: float = 1.0):
        self.dev = dev
        self.poll_period = poll_period
        self.blade = p.Blade.MC1F10HP
        self._synced = False
        self._lock = asyncio.Lock()
        self.out: dict[str, object] = {}
        self.rbv: dict[str, object] = {}
        self._build()

    # -- records ----------------------------------------------------------

    def _pair(self, key, name, out_factory, in_factory, *args, **fields):
        async def on_update(value, key=key):
            await self.write(key, value)

        # initial_value clears UDF so setpoints are not INVALID before sync
        self.out[key] = out_factory(name, *args, on_update=on_update, always_update=True,
                                    initial_value=0 if out_factory is not builder.longOut
                                    else fields.get("DRVL", 0), **fields)
        ro = {k: v for k, v in fields.items() if k not in ("DRVL", "DRVH")}
        self.rbv[key] = in_factory(f"{name}_RBV", *args, **ro)

    def _build(self) -> None:
        b = builder
        self._pair("enable", "ENABLE", b.boolOut, b.boolIn, ZNAM="Stop", ONAM="Run")
        self._pair("freq", "FREQ", b.aOut, b.aIn, EGU="Hz", PREC=2, DRVL=0, DRVH=10000)
        self._pair("phase", "PHASE", b.aOut, b.aIn, EGU="deg", PREC=0, DRVL=0, DRVH=360)
        self._pair("blade", "BLADE", b.mbbOut, b.mbbIn, *BLADE_LABELS)
        self._pair("ref", "REF_IN", b.mbbOut, b.mbbIn, *p.REF_IN_LABELS)
        self._pair("output", "REF_OUT", b.mbbOut, b.mbbIn, *p.REF_OUT_LABELS)
        self._pair("nharmonic", "NHARM", b.longOut, b.longIn, DRVL=1, DRVH=15)
        self._pair("dharmonic", "DHARM", b.longOut, b.longIn, DRVL=1, DRVH=15)
        self._pair("oncycle", "ONCYCLE", b.longOut, b.longIn, EGU="%", DRVL=1, DRVH=50)
        self._pair("intensity", "INTENSITY", b.longOut, b.longIn, DRVL=1, DRVH=10)
        self.rbv["refoutfreq"] = b.aIn("REF_OUT_FREQ_RBV", EGU="Hz", PREC=2)
        self.rbv["input"] = b.aIn("EXT_FREQ_RBV", EGU="Hz", PREC=2)
        self.ref_in_choices = b.longStringIn("REF_IN_CHOICES", length=128)
        self.ref_out_choices = b.longStringIn("REF_OUT_CHOICES", length=128)
        self.idn = b.longStringIn("IDN", length=128)
        self.connected = b.boolIn("CONNECTED", ZNAM="Disconnected", ONAM="Connected",
                                  initial_value=0)
        self.last_error = b.longStringIn("LAST_ERROR", length=256)
        self.restore = b.boolOut("RESTORE", ZNAM="Idle", ONAM="Restore",
                                 on_update=self._on_restore, always_update=True)
        b.stringIn("IOC_VERSION", initial_value=__version__)

    # -- helpers ----------------------------------------------------------

    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def _ensure_connected(self) -> None:
        if not self.dev.is_open:
            idn = await self._call(self.dev.open)
            self.idn.set(idn)
            self.connected.set(1)
            self._synced = False

    def _error(self, msg: str) -> None:
        log.warning(msg)
        self.last_error.set(msg[:255])

    def _to_device(self, key: str, value) -> tuple[float, float]:
        """Translate a setpoint value to (device value, resolution)."""
        if key == "ref":
            choices = p.ref_in_choices(self.blade)
            want = p.RefIn(int(value))
        elif key == "output":
            choices = p.ref_out_choices(self.blade)
            want = p.RefOut(int(value))
        elif key == "freq":
            return value, p.freq_resolution(self.blade)
        else:
            return value, 1.0
        if want not in choices:
            labels = p.REF_IN_LABELS if key == "ref" else p.REF_OUT_LABELS
            raise ValueError(f"{labels[want]} not available for blade {self.blade.name}")
        return choices.index(want), 1.0

    # -- writes -----------------------------------------------------------

    async def write(self, key: str, value) -> None:
        ok = False
        try:
            async with self._lock:
                await self._ensure_connected()
                dev_value, res = self._to_device(key, value)
                await self._call(self.dev.set, key, dev_value, res)
            self.last_error.set("")
            ok = True
        except Exception as exc:
            self._error(f"{key}={value}: {exc}")
        await self.poll_once()
        if not ok and key in self.rbv:
            self.out[key].set(self.rbv[key].get(), process=False)

    async def _on_restore(self, value) -> None:
        if not value:
            return
        try:
            async with self._lock:
                await self._ensure_connected()
                await self._call(self.dev.restore_defaults)
            self._synced = False
            self.last_error.set("")
        except Exception as exc:
            self._error(f"restore: {exc}")
        self.restore.set(0, process=False)
        await self.poll_once()

    # -- polling ----------------------------------------------------------

    def _read_all(self) -> dict[str, float | None]:
        """Runs in a worker thread.

        Only I/O failures and timeouts propagate (=> lost contact). A query
        the unit still rejects after a retry yields None for that key and is
        recorded in ``self._poll_errors``.
        """
        self._poll_errors: list[str] = []

        def q(key: str) -> float | None:
            try:
                return self.dev.query(key)
            except p.CommandError as exc:
                self._poll_errors.append(str(exc))
                return None

        values: dict[str, float | None] = {"blade": q("blade")}
        blade = p.Blade(int(values["blade"])) if values["blade"] is not None else self.blade
        for key in ("enable", "freq", "phase", "ref", "output", "nharmonic",
                    "dharmonic", "intensity", "refoutfreq", "input"):
            values[key] = q(key)
        values["oncycle"] = q("oncycle") if blade in p.ADJUSTABLE_BLADES else None
        return values

    async def poll_once(self) -> None:
        async with self._lock:
            try:
                await self._ensure_connected()
                values = await self._call(self._read_all)
            except Exception as exc:
                self._disconnected(exc)
                return
        for msg in self._poll_errors:
            self._error(f"poll: {msg}")
        self._publish(values)

    def _disconnected(self, exc: Exception) -> None:
        if self.connected.get():
            self._error(f"lost contact with {self.dev.port}: {exc}")
        elif not self.last_error.get():
            self._error(f"cannot reach {self.dev.port}: {exc}")
        self.dev.close()
        self.connected.set(0)
        for rec in self.rbv.values():
            rec.set(rec.get(), severity=alarm.INVALID_ALARM, alarm=alarm.COMM_ALARM)

    def _publish(self, values: dict[str, float | None]) -> None:
        self.connected.set(1)
        if values["blade"] is not None:
            self.blade = p.Blade(int(values["blade"]))
        in_choices = p.ref_in_choices(self.blade)
        out_choices = p.ref_out_choices(self.blade)
        self.ref_in_choices.set(" ".join(p.REF_IN_LABELS[c] for c in in_choices))
        self.ref_out_choices.set(" ".join(p.REF_OUT_LABELS[c] for c in out_choices))

        for key, value in values.items():
            rec = self.rbv[key]
            if value is None:  # not applicable to this blade, or query rejected
                rec.set(rec.get(), severity=alarm.INVALID_ALARM, alarm=alarm.SOFT_ALARM)
                continue
            if key == "ref":
                value = int(in_choices[int(value)])
            elif key == "output":
                value = int(out_choices[int(value)])
            elif key in ("enable", "blade", "nharmonic", "dharmonic", "oncycle", "intensity"):
                value = int(value)
            rec.set(value)

        if not self._synced and None not in (values[k] for k in self.out if k != "oncycle"):
            for key, out in self.out.items():
                if values.get(key) is not None:
                    out.set(self.rbv[key].get(), process=False)
            self._synced = True

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.poll_period)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="EPICS IOC for the Thorlabs MC2000B optical chopper")
    ap.add_argument("--port", required=True,
                    help="serial device, ideally /dev/serial/by-id/... (any pyserial URL works)")
    ap.add_argument("--prefix", required=True,
                    help="PV prefix, used verbatim, e.g. 24IDC:CHOP: (include the trailing ':')")
    ap.add_argument("--poll", type=float, default=1.0, help="readback poll period / s")
    ap.add_argument("--timeout", type=float, default=1.0, help="per-command reply timeout / s")
    ap.add_argument("--no-shell", action="store_true", help="run without the interactive shell")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    dispatcher = asyncio_dispatcher.AsyncioDispatcher()
    builder.SetSeparator("")
    builder.SetPrefix(args.prefix)
    ioc = ChopperIOC(MC2000B(args.port, timeout=args.timeout), args.poll)
    builder.LoadDatabase()
    softioc.iocInit(dispatcher)
    dispatcher(ioc.run)

    if args.no_shell:
        softioc.non_interactive_ioc()
    else:
        softioc.interactive_ioc({"ioc": ioc, "dev": ioc.dev})


if __name__ == "__main__":
    main()
