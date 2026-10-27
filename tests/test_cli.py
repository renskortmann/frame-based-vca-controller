import json

from vcactl.cli import main
from conftest import PROFILES, TEST_SENSOR


def test_check_pass_and_fail(capsys):
    assert main(["check", "--sensor", TEST_SENSOR, "--shaker", "tv51110", "--profile",
                 str(PROFILES / "flat.toml")]) == 0
    assert main(["check", "--sensor", TEST_SENSOR, "--shaker", "tv52110", "--profile",
                 str(PROFILES / "wideband.toml"), "--level", "13"]) == 2
    assert "FAIL" in capsys.readouterr().out


def test_open_loop_needs_a_sine_sweep(capsys):
    base = ["run", "--sim", "--shaker", "bk4809", "--open-loop", "1000"]
    assert main(base + ["--profile", "low_g"]) == 2
    assert main(base + ["--profile", "stepped_sine"]) == 2
    assert main(["run", "--sim", "--shaker", "bk4809", "--profile", "sine_sweep",
                 "--open-loop", "5000"]) == 2                     # outside 20-2000 Hz
    assert capsys.readouterr().err.count("--open-loop needs a sine sweep") == 3


def test_run_sim(tmp_path, capsys):
    rc = main(["run", "--sensor", TEST_SENSOR, "--sim", "--shaker", "tv52110", "--profile",
               str(PROFILES / "sloped.toml"), "--duration", "3",
               "--log-dir", str(tmp_path), "--seed", "1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "result : completed" in out
    (run_dir,) = tmp_path.iterdir()
    meta = json.loads((run_dir / "meta.json").read_text())
    for key in ("fs_io_hz", "decimation"):                   # the resolved values are logged
        assert meta["rates"][key] == meta["settings"]["daq"][key] != "auto"
    assert meta["rates"]["frame_size"] == meta["settings"]["control"]["frame_size"] != "auto"


def test_check_prints_rates(capsys):
    assert main(["check", "--shaker", "bk4809", "--profile", "low_g"]) == 0
    assert ("rates  : I/O 100 kS/s, decimation 10, control 10 kS/s, frame 2048 (df 4.88 Hz), "
            "band to 3 kHz (auto)") in capsys.readouterr().out


def test_incompatible_profile_is_a_config_error(tmp_path, capsys):
    p = tmp_path / "to5k.toml"
    p.write_text("breakpoints = [{ f_hz = 20.0, psd_g2_hz = 1e-5 }, "
                 "{ f_hz = 5000.0, psd_g2_hz = 1e-5 }]\n")
    args = ["check", "--shaker", "bk4809", "--profile", str(p)]
    assert main(args + ["--sensor", "pcb356b18"]) == 2                     # 356B18: 3 kHz
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configuration error: the profile (20-5000 Hz) does not fit" in captured.err
    assert "f_max 3000 Hz of the sensor (PCB 356B18" in captured.err
    assert main(args) == 0                                                 # 352B: 10 kHz


def test_config_error_exit_code(capsys):
    assert main(["check", "--sensor", TEST_SENSOR, "--shaker", "nope", "--profile", str(PROFILES / "flat.toml")]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_check_with_daq(capsys):
    assert main(["check", "--sensor", TEST_SENSOR, "--daq", "pxie4468", "--shaker", "tv51110", "--profile",
                 str(PROFILES / "flat.toml")]) == 0
    assert "NI PXIe-4468" in capsys.readouterr().out


def test_check_sine_profiles(capsys):
    for name in ("sine_sweep", "stepped_sine", "ringdown"):
        assert main(["check", "--sensor", TEST_SENSOR, "--shaker", "tv51110", "--profile",
                     str(PROFILES / f"{name}.toml")]) == 0
    out = capsys.readouterr().out
    assert "accel peak" in out and "ring-down 2 s" in out


def test_run_sim_ringdown(tmp_path, capsys):
    rc = main(["run", "--sensor", TEST_SENSOR, "--sim", "--shaker", "tv51110", "--profile",
               str(PROFILES / "ringdown.toml"), "--level", "-6",
               "--log-dir", str(tmp_path), "--seed", "1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "result : completed" in out and "3 ring-down(s)" in out
    (run_dir,) = tmp_path.iterdir()
    files = {p.name for p in run_dir.iterdir()}
    assert {"steps.csv", "events.csv", "ringdown_L3F1.csv", "status.csv"} <= files
    header = (run_dir / "status.csv").read_text().splitlines()[0]
    assert "meas_pk_g" in header


def test_profile_and_settings_keys(capsys):
    assert main(["check", "--sensor", TEST_SENSOR, "--shaker", "tv51110", "--profile", "flat",
                 "--settings", "default"]) == 0
    assert "Flat 20-2000 Hz" in capsys.readouterr().out


def test_unknown_profile_key(capsys):
    assert main(["check", "--shaker", "bk4809", "--profile", "nope"]) == 2
    assert "nope.toml" in capsys.readouterr().err


def test_check_ai_range(capsys):
    args = ["check", "--shaker", "bk4809", "--profile", "low_g"]
    assert main(args) == 0
    assert "AI range +/-5 V (auto; expected peak 2.39 V)" in capsys.readouterr().out
    assert main(args + ["--level", "-12"]) == 0
    assert "AI range +/-1 V (auto" in capsys.readouterr().out
    assert main(args + ["--ai-range", "1"]) == 2          # 2.39 V + 3 dB does not fit 1 V
    out = capsys.readouterr().out
    assert "AI range +/-1 V (manual" in out and "FAIL" in out
    assert main(args + ["--ai-range", "2"]) == 2          # not a USB-6211 range
    assert "--ai-range must be" in capsys.readouterr().err


def test_pretest_sim_auto_range(tmp_path, capsys):
    rc = main(["pretest", "--sim", "--yes", "--shaker", "bk4809", "--profile", "low_g",
               "--level", "-12", "--log-dir", str(tmp_path), "--seed", "1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "AI range: +/-1 V (auto" in out and "result : pretest completed" in out
