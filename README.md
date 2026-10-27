# mc2000b-ioc

EPICS IOC for the Thorlabs MC2000B / MC2000B-EC optical chopper, written
against the USB serial command set in the user guide (TTN102010-D02 Rev C,
chapters 7–8). Pure Python: [pythonSoftIOC](https://github.com/DiamondLightSource/pythonSoftIOC)
+ pyserial, no EPICS build, no StreamDevice.

Layout:

| Module | Role |
| --- | --- |
| `protocol.py` | Pure protocol description: blade indices, blade-dependent ref tables, reply parsing, setpoint formatting |
| `device.py` | Blocking, lock-serialised serial transport (`MC2000B` class), usable on its own from scripts |
| `sim.py` | Protocol-level simulator; `mc2000b-sim` serves it on a pty |
| `ioc.py` | pythonSoftIOC records and poll loop; `mc2000b-ioc` entry point |

## Install and run

```sh
python -m venv .venv && . .venv/bin/activate
pip install .            # or: pip install -e '.[dev]' && pytest
mc2000b-ioc --port /dev/serial/by-id/usb-Thorlabs_...-if00-port0 --prefix 24IDC:CHOP:
```

The unit enumerates as a USB-serial device (the manual calls it "RS232
emulated"; on Linux no vendor driver is needed). Use the `/dev/serial/by-id/`
path rather than `/dev/ttyUSB0`, and make sure the IOC user is in the
`dialout` group. Run under procServ as usual; `--no-shell` disables the
interactive Python shell.

Without hardware:

```sh
mc2000b-sim --link /tmp/chop &
mc2000b-ioc --port /tmp/chop --prefix TEST:CHOP:
```

Options: `--poll` (readback period, default 1 s), `--timeout` (per-command
reply timeout, default 1 s), `-v` (debug logging, shows all serial traffic).

## PVs

All under `$(PREFIX)`, which is used verbatim (include the trailing `:`). Writable parameters are a setpoint plus `_RBV`.

| PV | Type | Device command | Notes |
| --- | --- | --- | --- |
| `ENABLE` / `_RBV` | bo/bi Stop, Run | `enable` | |
| `FREQ` / `_RBV` | ao/ai Hz | `freq` | Internal reference frequency; quantised to the blade's resolution (0.01 / 0.1 / 1 Hz) |
| `PHASE` / `_RBV` | ao/ai deg | `phase` | 0–360 |
| `BLADE` / `_RBV` | mbbo/mbbi | `blade` | MC1F2 … MC2F5360, manual 8.2 |
| `REF_IN` / `_RBV` | mbbo/mbbi | `ref` | INTERNAL, EXTERNAL, INT-OUTER, INT-INNER, EXT-OUTER, EXT-INNER |
| `REF_OUT` / `_RBV` | mbbo/mbbi | `output` | TARGET, ACTUAL, OUTER, INNER, SUM, DIFF |
| `REF_IN_CHOICES`, `REF_OUT_CHOICES` | string | – | Which of the above the current blade accepts |
| `NHARM` / `_RBV` | longout/longin | `nharmonic` | 1–15 |
| `DHARM` / `_RBV` | longout/longin | `dharmonic` | 1–15 |
| `ONCYCLE` / `_RBV` | longout/longin % | `oncycle` | 1–50; MC1F10A only, RBV is INVALID for other blades |
| `INTENSITY` / `_RBV` | longout/longin | `intensity` | Display brightness 1–10 |
| `REF_OUT_FREQ_RBV` | ai Hz | `refoutfreq?` | Frequency on the REF OUTPUT BNC |
| `EXT_FREQ_RBV` | ai Hz | `input?` | Measured frequency on EXT INPUT |
| `RESTORE` | bo | `restore` | Factory defaults; setpoints re-seeded afterwards |
| `IDN` | string | `id?` | |
| `CONNECTED` | bi | – | |
| `LAST_ERROR` | string | – | Last rejected write / comms error; cleared by the next good write |
| `IOC_VERSION` | string | – | |

`REF_IN` and `REF_OUT` present one fixed set of labels; the IOC maps them to
the blade-dependent index the firmware expects (manual 8.3/8.4) and refuses
labels the current blade does not support, with an explanation in
`LAST_ERROR`. This avoids an mbbo whose meaning silently changes when the
blade changes.

Behaviour:

* Writes are sent immediately, then all readbacks are refreshed. A write the
  unit rejects sets `LAST_ERROR` and the setpoint snaps back to the readback.
* On (re)connect setpoints are seeded from the unit, so front-panel changes
  made while the IOC was down are not overwritten.
* If the unit goes away every `_RBV` goes INVALID/COMM, `CONNECTED` drops,
  and the IOC keeps retrying every poll period.
* `verbose=0` is forced at connect so unsolicited status text cannot
  interleave with replies.

Per the manual, blade, reference and harmonic settings can only be changed
in standby (only frequency and phase while running); expect the unit to
reject those writes with `ENABLE=Run`.

## Not yet verified on hardware

Everything above is tested against the simulator and the manual, not a real
unit. Check on first connection (run with `-v` to see the traffic):

1. **Reply framing.** Assumed `<echo>\r<value>\r> `, consistent with
   thorlabs-mc2000b, which was written against hardware. If the firmware
   echoes differently `protocol.parse_reply` is the only place to change.
2. **Decimal setpoints.** `freq=123.5` is sent for 0.1/0.01 Hz blades
   (MC1F10HP, MC1F2, MC1F2P10). If the firmware only accepts integers there
   (or wants e.g. tenths of Hz) adjust `protocol.freq_resolution`. Phase is
   always sent as an integer.
3. **REF_OUT indices for dual-frequency blades** (TARGET, OUTER, INNER, SUM,
   DIFF = 0–4) are taken from thorlabs-mc2000b; the manual's table does not
   survive PDF extraction unambiguously.
4. **Error strings.** Only `Command error CMD_NOT_DEFINED` is documented;
   any line starting `Command error` or `CMD_` is treated as a rejection.
5. **Rejected writes in run mode** — confirm the firmware rejects (rather
   than silently ignores) blade/ref changes while running. If it ignores
   them the setpoint will stay at the requested value until the next
   re-seed; the `_RBV` is always truthful.
6. **Command throughput.** Each poll is 11–12 round trips. If 1 s is too
   tight at the unit's response speed, raise `--poll`.

Known gaps: there is no "LOCKED" indication over serial (the front-panel
lock status is not in the documented command set; verbose-mode status
messages might carry it but their format is undocumented), and presets
(store/recall) are front-panel only.
