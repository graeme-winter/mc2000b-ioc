"""Blocking serial transport for the MC2000B.

All traffic is serialised by a lock so the class can be shared between the
IOC poll loop and write handlers (which call it from worker threads).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import serial

from . import protocol as p

log = logging.getLogger(__name__)

#: Read-write parameters (manual 8.1).
SETTABLE = ("freq", "blade", "nharmonic", "dharmonic", "phase", "enable",
            "ref", "output", "oncycle", "intensity")
#: Read-only queries returning numbers.
READ_ONLY = ("refoutfreq", "input")


class MC2000B:
    """One MC2000B on a serial port (or any pyserial URL, e.g. a pty)."""

    def __init__(self, port: str, timeout: float = 1.0,
                 serial_factory: Callable[..., serial.SerialBase] = serial.serial_for_url):
        self.port = port
        self.timeout = timeout
        self._factory = serial_factory
        self._ser: serial.SerialBase | None = None
        self._lock = threading.Lock()
        self.idn = ""

    # -- connection -------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def open(self) -> str:
        """Open the port, resynchronise, disable verbose output, return ID."""
        self.close()
        self._ser = self._factory(
            self.port, baudrate=p.BAUD, bytesize=8, parity="N", stopbits=1,
            timeout=0.05, write_timeout=self.timeout,
            xonxoff=False, rtscts=False, dsrdtr=False,
        )
        try:
            # A bare CR flushes any half-typed line and yields a fresh prompt;
            # the unit answers it with CMD_NOT_DEFINED, which we ignore.
            try:
                self.transact("")
            except p.CommandError:
                pass
            self.set("verbose", 0)
            self.idn = self.transact("id?")
            if "MC2000B" not in self.idn:
                raise p.ProtocolError(f"unexpected id reply {self.idn!r}")
        except Exception:
            self.close()
            raise
        log.info("connected to %s on %s", self.idn, self.port)
        return self.idn

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self._ser = None

    # -- primitives -------------------------------------------------------

    def transact(self, command: str) -> str:
        """Send one command, wait for the prompt, return the reply payload.

        Raises ``CommandError`` if the unit rejects it, ``ProtocolError`` on
        timeout/garbage, ``serial.SerialException`` / ``OSError`` on I/O
        failure. Any non-CommandError failure closes the port so the caller
        can reconnect.
        """
        with self._lock:
            if self._ser is None:
                raise p.ProtocolError("port not open")
            try:
                self._ser.reset_input_buffer()
                self._ser.write(p.encode(command))
                raw = self._read_to_prompt(command)
                return p.parse_reply(command, raw)
            except p.CommandError:
                raise
            except Exception:
                self.close()
                raise

    def _read_to_prompt(self, command: str) -> bytes:
        assert self._ser is not None
        buf = b""
        deadline = time.monotonic() + self.timeout
        while not p.reply_complete(buf):
            if time.monotonic() > deadline:
                raise p.ProtocolError(f"timeout waiting for reply to {command!r} (got {buf!r})")
            buf += self._ser.read(max(1, self._ser.in_waiting))
        return buf

    def query(self, key: str) -> float:
        return p.parse_number(self.transact(f"{key}?"))

    def set(self, key: str, value: float, resolution: float = 1.0) -> None:
        reply = self.transact(f"{key}={p.format_value(value, resolution)}")
        if reply:
            log.debug("unexpected payload after %s=%s: %r", key, value, reply)

    def restore_defaults(self) -> None:
        self.transact("restore")
