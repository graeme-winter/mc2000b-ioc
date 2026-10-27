"""Software model of an MC2000B, for tests and hardware-free IOC bring-up.

This models the *wire protocol* (echo, reply, prompt, error strings and
argument validation) well enough to exercise the IOC. It does not model the
PLL, lock time or the exact relationship between inner/outer frequencies.

Run ``mc2000b-sim`` to expose a simulated unit on a pseudo-terminal, then
point ``mc2000b-ioc --port`` at the printed path.
"""

from __future__ import annotations

import argparse
import os
import select
import tty

from . import protocol as p

_RANGES = {
    "blade": (0, 14), "nharmonic": (1, 15), "dharmonic": (1, 15),
    "phase": (0, 360), "enable": (0, 1), "oncycle": (1, 50),
    "intensity": (1, 10), "verbose": (0, 1), "freq": (0.01, 10000),
}


class SimulatedMC2000B:
    def __init__(self) -> None:
        self.state: dict[str, float] = {}
        self.restore()
        self._line = b""
        self.log: list[str] = []
        #: Fault injection: drop the first character of the next N commands,
        #: mimicking the busy firmware dropping input.
        self.garble = 0

    def restore(self) -> None:
        self.state = dict(freq=1000, blade=int(p.Blade.MC1F10HP), nharmonic=1,
                          dharmonic=1, phase=0, enable=0, ref=0, output=0,
                          oncycle=50, intensity=5, verbose=0)

    # -- byte interface ---------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        out = b""
        for byte in data:
            ch = bytes([byte])
            if ch in (b"\r", b"\n"):
                if ch == b"\n" and not self._line:
                    continue
                line, self._line = self._line.decode("ascii", "replace"), b""
                if self.garble and line:
                    self.garble -= 1
                    line = line[1:]
                out += line.encode() + b"\r" + self.handle(line) + b"> "
            else:
                self._line += ch
        return out

    # -- command handling -------------------------------------------------

    def _blade(self) -> p.Blade:
        return p.Blade(int(self.state["blade"]))

    def refoutfreq(self) -> float:
        if not self.state["enable"]:
            return 0.0
        f = self.state["freq"]
        blade = self._blade()
        choice = p.ref_out_choices(blade)[int(self.state["output"])]
        if choice is p.RefOut.INNER and blade in p.PRECISION_BLADES:
            return f / (50 if blade is not p.Blade.MC1F10HP else 10)
        return f

    def handle(self, line: str) -> bytes:
        self.log.append(line)
        err = b"Command error %s\r"
        if line == "id?":
            return b"THORLABS MC2000B (SIMULATED) VERSION 1.0.0\r"
        if line == "restore":
            self.restore()
            return b""
        if line.endswith("?"):
            key = line[:-1]
            if key == "refoutfreq":
                return f"{self.refoutfreq():g}\r".encode()
            if key == "input":
                return b"0\r"
            if key == "oncycle" and self._blade() not in p.ADJUSTABLE_BLADES:
                return err % b"CMD_NOT_DEFINED"
            if key in self.state:
                return f"{self.state[key]:g}\r".encode()
            return err % b"CMD_NOT_DEFINED"
        if "=" in line:
            key, _, arg = line.partition("=")
            if key not in self.state:
                return err % b"CMD_NOT_DEFINED"
            try:
                value = float(arg)
            except ValueError:
                return err % b"CMD_ARG_INVALID"
            if key in ("ref", "output"):
                n = len(p.ref_in_choices(self._blade()) if key == "ref"
                        else p.ref_out_choices(self._blade()))
                lo, hi = 0, n - 1
            else:
                lo, hi = _RANGES[key]
            if not lo <= value <= hi:
                return err % b"CMD_ARG_INVALID"
            if key != "freq" and value != int(value):
                return err % b"CMD_ARG_INVALID"
            self.state[key] = value
            if key == "blade":
                self.state["ref"] = 0
                self.state["output"] = 0
            return b""
        return err % b"CMD_NOT_DEFINED"


class FakeSerial:
    """Minimal in-memory stand-in for ``serial.Serial`` backed by the model."""

    def __init__(self, sim: SimulatedMC2000B):
        self.sim = sim
        self._rx = b""
        self.is_open = True

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise OSError("closed")
        self._rx += self.sim.feed(data)
        return len(data)

    def read(self, n: int = 1) -> bytes:
        out, self._rx = self._rx[:n], self._rx[n:]
        return out

    def reset_input_buffer(self) -> None:
        self._rx = b""

    def close(self) -> None:
        self.is_open = False


def serve_pty(link: str | None = None) -> None:
    sim = SimulatedMC2000B()
    master, slave = os.openpty()
    tty.setraw(slave)
    path = os.ttyname(slave)
    if link:
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(path, link)
        path = f"{link} -> {path}"
    print(f"simulated MC2000B on {path}", flush=True)
    try:
        while True:
            select.select([master], [], [])
            data = os.read(master, 1024)
            reply = sim.feed(data)
            if reply:
                os.write(master, reply)
    except KeyboardInterrupt:
        pass
    finally:
        if link and os.path.islink(link):
            os.unlink(link)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--link", help="also create this symlink to the pty (e.g. /tmp/mc2000b)")
    serve_pty(ap.parse_args().link)


if __name__ == "__main__":
    main()
