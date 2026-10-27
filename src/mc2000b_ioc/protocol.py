"""Pure (no I/O) description of the MC2000B serial protocol.

Source: Thorlabs MC2000B User Guide TTN102010-D02 Rev C, chapters 7 and 8.
The blade-dependent reference index tables (manual 8.3/8.4) are ambiguous in
the PDF; the mapping here agrees with Patrick Tapping's thorlabs-mc2000b
package, which was written against hardware.

Wire format: 115200 8N1, no flow control. Commands are ``key=value\\r`` or
``key?\\r``. The unit echoes the command, prints any reply on following
line(s), then a ``> `` prompt.
"""

from __future__ import annotations

import re
from enum import IntEnum

BAUD = 115200
TERMINATOR = b"\r"
PROMPT = b"> "


class ProtocolError(RuntimeError):
    """Malformed or missing reply."""


class CommandError(ProtocolError):
    """The unit rejected a command (``Command error ...``)."""


class Blade(IntEnum):
    """Blade model -> protocol index (manual 8.2)."""

    MC1F2 = 0
    MC1F10 = 1
    MC1F15 = 2
    MC1F30 = 3
    MC1F60 = 4
    MC1F100 = 5
    MC1F10HP = 6
    MC1F2P10 = 7
    MC1F6P10 = 8
    MC1F10A = 9
    MC2F330 = 10
    MC2F47 = 11
    MC2F57B = 12
    MC2F860 = 13
    MC2F5360 = 14


class RefIn(IntEnum):
    """Logical reference-input sources (union over all blades)."""

    INTERNAL = 0
    EXTERNAL = 1
    INT_OUTER = 2
    INT_INNER = 3
    EXT_OUTER = 4
    EXT_INNER = 5


class RefOut(IntEnum):
    """Logical reference-output sources (union over all blades)."""

    TARGET = 0
    ACTUAL = 1
    OUTER = 2
    INNER = 3
    SUM = 4
    DIFF = 5


REF_IN_LABELS = ("INTERNAL", "EXTERNAL", "INT-OUTER", "INT-INNER", "EXT-OUTER", "EXT-INNER")
REF_OUT_LABELS = ("TARGET", "ACTUAL", "OUTER", "INNER", "SUM", "DIFF")

#: Two-slot-track "higher precision" blades (inner/outer reference selection).
PRECISION_BLADES = frozenset({Blade.MC1F2, Blade.MC1F10HP, Blade.MC1F2P10})
#: Two-frequency blades (sum/difference outputs).
DUAL_BLADES = frozenset({Blade.MC2F330, Blade.MC2F47, Blade.MC2F57B, Blade.MC2F860, Blade.MC2F5360})
#: Blades with a user-adjustable duty cycle (``oncycle``).
ADJUSTABLE_BLADES = frozenset({Blade.MC1F10A})


def ref_in_choices(blade: Blade) -> tuple[RefIn, ...]:
    """Logical ref-in sources, ordered by device index, for ``blade``."""
    if blade in PRECISION_BLADES:
        return (RefIn.INT_OUTER, RefIn.INT_INNER, RefIn.EXT_OUTER, RefIn.EXT_INNER)
    return (RefIn.INTERNAL, RefIn.EXTERNAL)


def ref_out_choices(blade: Blade) -> tuple[RefOut, ...]:
    """Logical ref-out sources, ordered by device index, for ``blade``."""
    if blade in PRECISION_BLADES:
        return (RefOut.TARGET, RefOut.OUTER, RefOut.INNER)
    if blade in DUAL_BLADES:
        return (RefOut.TARGET, RefOut.OUTER, RefOut.INNER, RefOut.SUM, RefOut.DIFF)
    return (RefOut.TARGET, RefOut.ACTUAL)


def freq_resolution(blade: Blade) -> float:
    """Frequency setpoint resolution in Hz (manual 12.2)."""
    if blade in (Blade.MC1F2, Blade.MC1F2P10):
        return 0.01
    if blade is Blade.MC1F10HP:
        return 0.1
    return 1.0


def format_value(value: float, resolution: float = 1.0) -> str:
    """Format a setpoint for the wire, quantised to ``resolution``.

    Whole numbers are always sent without a decimal point, so integer-only
    parameters never see ``"1.0"``.
    """
    q = round(float(value) / resolution) * resolution
    if abs(q - round(q)) < 1e-9:
        return str(int(round(q)))
    decimals = max(0, -int(f"{resolution:e}".split("e")[1]))
    return f"{q:.{decimals}f}"


def encode(command: str) -> bytes:
    return command.encode("ascii") + TERMINATOR


def reply_complete(buf: bytes) -> bool:
    """True once ``buf`` ends with a prompt at the start of a line.

    Requiring the line break stops a stray ``> `` inside an error message
    (seen on firmware 4.19: ``Command error> CMD_NOT_DEFINED``) from ending
    the read early and leaving the rest of the reply in the buffer.
    """
    return buf.endswith((b"\r" + PROMPT, b"\n" + PROMPT)) or buf == PROMPT


def parse_reply(command: str, raw: bytes) -> str:
    """Strip echo and prompt from ``raw``; raise on device-reported errors.

    Returns the reply payload (possibly empty, possibly multi-line joined by
    ``\\n``).
    """
    if not reply_complete(raw):
        raise ProtocolError(f"no prompt in reply to {command!r}: {raw!r}")
    text = raw[: -len(PROMPT)].decode("ascii", errors="replace")
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text)]
    lines = [ln.lstrip("> ").strip() for ln in lines]
    lines = [ln for ln in lines if ln]
    if lines and lines[0] == command:
        lines = lines[1:]
    for ln in lines:
        if ln.startswith("Command error") or ln.startswith("CMD_"):
            raise CommandError(f"{command!r}: {ln}")
    return "\n".join(lines)


_NUMBER = re.compile(r"[-+]?\d+(?:\.\d*)?")


def parse_number(text: str) -> float:
    """Parse the first number in a query reply."""
    m = _NUMBER.search(text)
    if m is None:
        raise ProtocolError(f"expected a number, got {text!r}")
    return float(m.group(0))
