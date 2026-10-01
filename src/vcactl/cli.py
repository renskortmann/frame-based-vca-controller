"""Command-line interface: ``vcactl {list-devices,check,pretest,run}``."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .config import (DEFAULT_CONFIG_DIR, ConfigError, load_shaker, load_settings,
                     validate_setup)
from .controller import Controller, State
from .logger import RunLogger
from .profile import load_profile
from .safety import preflight


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--shaker", required=True,
                   help="shaker key (tv51110, tv52110, bk4809) or path to a shaker TOML file")
    p.add_argument("--profile", required=True, help="path to a test profile TOML file")
    p.add_argument("--daq", default="usb6211",
                   help="DAQ device key (usb6211, usb4431, pxie4468) or path to a DAQ TOML file "
                        "(default usb6211)")
    p.add_argument("--settings", "--station", dest="settings", default=None,
                   help=f"settings TOML (default: {DEFAULT_CONFIG_DIR / 'settings.toml'})")
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                   help="directory containing shakers/*.toml and daq/*.toml")
    p.add_argument("--level", type=float, default=0.0,
                   help="target level in dB relative to the profile (default 0)")


def _add_run_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sim", action="store_true", help="use the simulated shaker instead of the DAQ")
    p.add_argument("--sim-realtime", action="store_true", help="pace the simulation in real time")
    p.add_argument("--log-dir", type=Path, default=Path("runs"), help="root directory for run logs")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--seed", type=int, default=None, help="random seed (reproducible drive)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="vcactl", description=__doc__)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-devices", help="list NI-DAQmx devices")

    p = sub.add_parser("check", help="pre-flight check of a profile against shaker limits (no output)")
    _add_common(p)

    p = sub.add_parser("pretest", help="measure noise floor and FRF at low level, then stop")
    _add_common(p)
    _add_run_opts(p)

    p = sub.add_parser("run", help="run a closed-loop random test")
    _add_common(p)
    _add_run_opts(p)
    p.add_argument("--duration", type=float, default=None,
                   help="test duration at full level in s (default: from profile)")
    return ap


def _print_status(row: dict, last: list) -> None:
    now = time.monotonic()
    if now - last[0] < 0.25:
        return
    last[0] = now
    if "meas_rms_g" in row:
        text = (f"{row['state']:<8} {row['level_db']:+6.1f} dB  "
                f"ref {row['ref_rms_g']:6.3f} g  meas {row['meas_rms_g']:6.3f} g  "
                f"err {row['rms_err_db']:+5.2f} dB  drive {row['drive_rms_v']:5.3f} Vrms  "
                f"alarm {row['lines_alarm']:3d}  abort {row['lines_abort']:3d}  "
                f"t {row['run_time_s']:6.1f}/{row['duration_s']:.0f} s")
    else:
        text = (f"{row['state']:<8} drive {row['drive_rms_v']:5.3f} Vrms  "
                f"response {row['block_rms_g']:6.3f} g rms")
    sys.stdout.write("\r" + text.ljust(130))
    sys.stdout.flush()


def _make_backend(args, settings, shaker, block_io):
    if args.sim:
        from .daq.sim import SimulatedDaq
        return SimulatedDaq(settings, shaker, realtime=args.sim_realtime, seed=args.seed)
    from .daq.nidaq import NiDaq
    return NiDaq(settings, block_io)


def cmd_list_devices() -> int:
    from .daq.nidaq import list_devices
    try:
        devices = list_devices()
    except Exception as exc:
        print(f"cannot query NI-DAQmx: {exc}", file=sys.stderr)
        return 1
    if not devices:
        print("no NI-DAQmx devices found")
    for name, product, serial in devices:
        print(f"{name:<10} {product:<16} serial {serial}")
    return 0


def cmd_check(args, settings, shaker, profile) -> int:
    report = preflight(profile, shaker, settings, args.level)
    print(f"shaker : {shaker.name}\ndaq    : {settings.daq.model}\nprofile: {profile.name} at {args.level:+.1f} dB "
          f"({profile.f_lo:g}-{profile.f_hi:g} Hz, {profile.accel_rms_g() * 10 ** (args.level / 20):.3f} g rms)\n")
    print(report.format())
    print("\nPASS" if report.ok else "\nFAIL")
    return 0 if report.ok else 2


def cmd_run(args, settings, shaker, profile, pretest_only: bool) -> int:
    report = preflight(profile, shaker, settings, args.level)
    print(report.format())
    if not report.ok:
        print("\npre-flight check FAILED; nothing was output")
        return 2
    if not args.sim and not args.yes:
        print(f"\nAbout to drive {shaker.name} via {settings.daq.model} "
              f"{settings.daq.device}/{settings.daq.ao_channel} "
              f"(max {shaker.max_drive_v:g} V).\nCheck the amplifier gain, the accelerometer "
              "mounting and the charge amplifier setting.")
        if input("Type 'yes' to start: ").strip().lower() != "yes":
            print("cancelled")
            return 1

    tag = f"{'pretest' if pretest_only else 'run'}_{Path(args.shaker).stem}_{Path(args.profile).stem}"
    logger = RunLogger(args.log_dir, tag + ("_sim" if args.sim else ""))
    block_io = settings.control.frame_size // 2 * settings.daq.decimation
    backend = _make_backend(args, settings, shaker, block_io)
    last = [0.0]
    ctl = Controller(settings, shaker, profile, backend, target_level_db=args.level,
                     duration_s=getattr(args, "duration", None), logger=logger, seed=args.seed,
                     on_status=lambda row: _print_status(row, last))
    logger.meta({"version": __version__, "shaker": shaker, "settings": settings,
                 "profile": profile, "level_db": args.level, "sim": args.sim,
                 "pretest_only": pretest_only, "started": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def request_stop(signum, frame):
        if ctl.stop_requested:          # second Ctrl-C: stop immediately (AO forced to 0 V)
            raise KeyboardInterrupt
        ctl.stop_requested = True
        sys.stdout.write("\nstopping: ramping down (press Ctrl-C again to stop immediately)\n")

    old_int = signal.signal(signal.SIGINT, request_stop)
    old_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        result = ctl.run(pretest_only=pretest_only)
    except KeyboardInterrupt:
        print("\nstopped immediately; AO set to 0 V")
        return 130
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        logger.close()

    print()
    info = getattr(ctl, "pretest_info", None)
    if info:
        print("pretest: response {response_rms_g:.3g} g rms, noise {noise_rms_g:.3g} g rms, "
              "SNR {snr_db:.1f} dB, mean coherence {coherence_mean:.3f}".format(**info)
              + (f", predicted drive {info['predicted_drive_rms_v']:.3f} V rms"
                 if "predicted_drive_rms_v" in info else ""))
    print(f"result : {result.reason}")
    if result.meas_rms_g is not None:
        print(f"final  : ref {result.ref_rms_g:.3f} g rms, meas {result.meas_rms_g:.3f} g rms "
              f"({result.rms_err_db:+.2f} dB), {result.run_time_s:.1f} s at full level")
    print(f"logs   : {logger.dir}")
    return 0 if result.completed else (1 if result.state == State.ABORTED else 3)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list-devices":
        return cmd_list_devices()
    try:
        settings = load_settings(args.settings, args.daq, args.config_dir)
        shaker = load_shaker(args.shaker, args.config_dir)
        profile = load_profile(args.profile)
        validate_setup(settings, shaker)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        return cmd_check(args, settings, shaker, profile)
    return cmd_run(args, settings, shaker, profile, pretest_only=args.cmd == "pretest")
