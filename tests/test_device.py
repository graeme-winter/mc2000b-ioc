import os
import subprocess
import sys
import time

import pytest

from mc2000b_ioc import protocol as p
from mc2000b_ioc.device import MC2000B
from mc2000b_ioc.sim import FakeSerial, SimulatedMC2000B


@pytest.fixture
def sim():
    return SimulatedMC2000B()


@pytest.fixture
def dev(sim):
    d = MC2000B("fake://", serial_factory=lambda *a, **k: FakeSerial(sim))
    d.open()
    yield d
    d.close()


def test_open_identifies_and_disables_verbose(dev, sim):
    assert "MC2000B" in dev.idn
    assert "verbose=0" in sim.log


def test_query_and_set(dev, sim):
    assert dev.query("blade") == p.Blade.MC1F10HP
    dev.set("freq", 123.46, resolution=0.1)
    assert sim.log[-1] == "freq=123.5"
    assert dev.query("freq") == pytest.approx(123.5)


def test_rejected_set_raises_and_keeps_port_open(dev):
    with pytest.raises(p.CommandError):
        dev.set("nharmonic", 16)
    assert dev.is_open
    assert dev.query("nharmonic") == 1


def test_timeout_closes_port(sim):
    class Mute(FakeSerial):
        def write(self, data):
            self.sim.feed(data)  # swallow the reply
            return len(data)

    d = MC2000B("fake://", timeout=0.1, serial_factory=lambda *a, **k: FakeSerial(sim))
    d.open()
    d._ser = Mute(sim)
    with pytest.raises(p.ProtocolError):
        d.query("freq")
    assert not d.is_open


def test_wrong_device_rejected(sim):
    sim.handle = lambda line: b"SOMETHING ELSE\r" if line == "id?" else b""
    d = MC2000B("fake://", serial_factory=lambda *a, **k: FakeSerial(sim))
    with pytest.raises(p.ProtocolError):
        d.open()
    assert not d.is_open


@pytest.mark.skipif(sys.platform != "linux", reason="pty test is Linux-only")
def test_over_real_pty(tmp_path):
    link = str(tmp_path / "chopper")
    proc = subprocess.Popen([sys.executable, "-m", "mc2000b_ioc.sim", "--link", link],
                            stdout=subprocess.PIPE, text=True)
    try:
        proc.stdout.readline()
        for _ in range(50):
            if os.path.exists(link):
                break
            time.sleep(0.05)
        d = MC2000B(link)
        d.open()
        d.set("enable", 1)
        assert d.query("refoutfreq") == 1000
        d.close()
    finally:
        proc.terminate()
        proc.wait()


def test_dropped_characters_are_retried(dev, sim):
    sim.garble = 1
    assert dev.query("blade") == p.Blade.MC1F10HP
    assert dev.is_open
    sim.garble = 1
    dev.set("freq", 100)
    assert sim.state["freq"] == 100


def test_persistent_rejection_still_raises(dev, sim):
    sim.garble = 5
    with pytest.raises(p.CommandError):
        dev.query("blade")
    assert dev.is_open
