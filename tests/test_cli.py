from vcactl.cli import main
from conftest import PROFILES


def test_check_pass_and_fail(capsys):
    assert main(["check", "--shaker", "tv51110", "--profile",
                 str(PROFILES / "example_flat.toml")]) == 0
    assert main(["check", "--shaker", "tv52110", "--profile",
                 str(PROFILES / "example_wideband.toml"), "--level", "13"]) == 2
    assert "FAIL" in capsys.readouterr().out


def test_run_sim(tmp_path, capsys):
    rc = main(["run", "--sim", "--shaker", "tv52110", "--profile",
               str(PROFILES / "example_sloped.toml"), "--duration", "3",
               "--log-dir", str(tmp_path), "--seed", "1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "result : completed" in out
    assert len(list(tmp_path.iterdir())) == 1


def test_config_error_exit_code(capsys):
    assert main(["check", "--shaker", "nope", "--profile", str(PROFILES / "example_flat.toml")]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_check_with_daq(capsys):
    assert main(["check", "--daq", "pxie4468", "--shaker", "tv51110", "--profile",
                 str(PROFILES / "example_flat.toml")]) == 0
    assert "NI PXIe-4468" in capsys.readouterr().out


def test_check_sine_profiles(capsys):
    for name in ("example_sine_sweep", "example_stepped_sine", "example_ringdown"):
        assert main(["check", "--shaker", "tv51110", "--profile",
                     str(PROFILES / f"{name}.toml")]) == 0
    out = capsys.readouterr().out
    assert "accel peak" in out and "ring-down 2 s" in out


def test_run_sim_ringdown(tmp_path, capsys):
    rc = main(["run", "--sim", "--shaker", "tv51110", "--profile",
               str(PROFILES / "example_ringdown.toml"), "--level", "-6",
               "--log-dir", str(tmp_path), "--seed", "1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "result : completed" in out and "3 ring-down(s)" in out
    (run_dir,) = tmp_path.iterdir()
    files = {p.name for p in run_dir.iterdir()}
    assert {"steps.csv", "events.csv", "ringdown_L3F1.csv", "status.csv"} <= files
    header = (run_dir / "status.csv").read_text().splitlines()[0]
    assert "meas_pk_g" in header
