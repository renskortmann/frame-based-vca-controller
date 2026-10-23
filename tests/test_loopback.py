"""Loopback self-test (vcactl loopback) against the simulated DAQ."""

import pytest

from vcactl.cli import main
from vcactl.config import load_settings, load_shaker
from vcactl.daq.sim import SimulatedDaq
from vcactl.loopback import loopback_settings, run_loopback


@pytest.mark.parametrize("daq,fs,delay", [
    ("usb6211", 250000.0, 0.0),
    ("usb4431", 51200.0, 62.0),                 # 62.1, rounded by the simulator
    ("pxie4468", 200000.0, 106.0),
    ("cdaq9234_9260", 51200.0, 71.0),
    ("cdaq9232_9263", 102400.0, 31.0),
    ("dt9837a", 52734.0, 0.0),
])
def test_reports_the_converter_delay(daq, fs, delay):
    settings = loopback_settings(load_settings(daq=daq))
    backend = SimulatedDaq(settings, load_shaker("tv51110"), seed=1, loopback_channel="ai1")
    result = run_loopback(settings, backend, duration_s=12.0, seed=1)
    assert result.ok, result.format()
    assert result.fs_io_hz == fs
    assert result.delay_start == pytest.approx(delay, abs=0.01)
    assert result.drift_ppm == pytest.approx(0.0, abs=1e-3)
    assert all(abs(g) < 0.2 for g in result.gain_db.values())
    assert backend.stopped_at_zero


def test_wrong_delay_fails():
    """A DAQ file whose filter_delay_samples does not match the hardware fails the test."""
    import dataclasses
    settings = loopback_settings(load_settings(daq="cdaq9234_9260"))
    backend = SimulatedDaq(settings, load_shaker("tv51110"), seed=1, loopback_channel="ai1")
    wrong = dataclasses.replace(settings, daq=dataclasses.replace(
        settings.daq, limits=dataclasses.replace(settings.daq.limits, filter_delay_samples=60.0)))
    result = run_loopback(wrong, backend, duration_s=12.0, seed=1)
    assert not result.ok and result.delay_start == pytest.approx(71.0, abs=0.01)


def test_cli(capsys, monkeypatch):
    args = ["loopback", "--daq", "cdaq9234_9260", "--loopback-channel", "ai1", "--sim",
            "--duration", "12", "--seed", "1"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "71.00 samples at the start" in out and "PASS" in out
    assert main(args[:-4] + ["--loopback-channel", "ai0"]) == 2   # the control channel
    assert "must differ from daq.ai_channel" in capsys.readouterr().err
    assert main(args + ["--level-v", "2"]) == 2
    assert "--level-v" in capsys.readouterr().err


def test_cli_asks_before_driving_hardware(capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert main(["loopback", "--daq", "usb6211", "--loopback-channel", "ai2"]) == 1
    out = capsys.readouterr().out
    assert "Disconnect the amplifier" in out and "cancelled" in out
