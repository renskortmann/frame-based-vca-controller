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
