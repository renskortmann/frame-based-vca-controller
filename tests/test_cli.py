from vcactl.cli import main
from conftest import PROFILES, TEST_SENSOR


def test_check_pass_and_fail(capsys):
    assert main(["check", "--sensor", TEST_SENSOR, "--shaker", "tv51110", "--profile",
                 str(PROFILES / "flat.toml")]) == 0
    assert main(["check", "--sensor", TEST_SENSOR, "--shaker", "tv52110", "--profile",
                 str(PROFILES / "wideband.toml"), "--level", "13"]) == 2
    assert "FAIL" in capsys.readouterr().out


def test_run_sim(tmp_path, capsys):
    rc = main(["run", "--sensor", TEST_SENSOR, "--sim", "--shaker", "tv52110", "--profile",
               str(PROFILES / "sloped.toml"), "--duration", "3",
               "--log-dir", str(tmp_path), "--seed", "1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "result : completed" in out
    assert len(list(tmp_path.iterdir())) == 1


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
    assert "AI range +/-5 V (auto; expected peak 2.57 V)" in capsys.readouterr().out
    assert main(args + ["--level", "-12"]) == 0
    assert "AI range +/-1 V (auto" in capsys.readouterr().out
    assert main(args + ["--ai-range", "1"]) == 2          # 2.57 V + 3 dB does not fit 1 V
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
