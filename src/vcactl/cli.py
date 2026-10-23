"""Command-line interface: ``vcactl {list-devices,check,pretest,run,loopback}``."""

from __future__ import annotations

import argparse
import importlib
import signal
import sys
import time
from pathlib import Path

import numpy as np

from . import __version__
from .config import (DEFAULT_CONFIG_DIR, ConfigError, load_shaker, load_settings,
                     resolve_config_path, validate_setup)
from .controller import Controller, State
from .daq.base import DaqFault, channel_number
from .logger import STATUS_FIELDS, RunLogger
from .profile import SineProfile, load_profile
from .rates import resolve_rates
from .safety import preflight, select_ai_range
from .sine import SINE_STATUS_FIELDS, SineController


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--shaker", required=True,
                   help="shaker key (tv51110, tv52110, bk4809, bk4801_4812) or path to a shaker TOML file")
    p.add_argument("--profile", required=True,
                   help="test profile key (low_g, flat, sloped, wideband, sine_sweep, stepped_sine, "
                        "ringdown) or path to a profile TOML file (random, sine_sweep or stepped_sine)")
    p.add_argument("--daq", default="usb6211",
                   help="DAQ device key (usb6211, usb4431, pxie4468, cdaq9234_9260, cdaq9232_9263, "
                        "dt9837a) or path to a DAQ TOML file (default usb6211)")
    p.add_argument("--sensor", default="pcb352b",
                   help="control accelerometer key (pcb352b, pcb356b18) or path to a sensor TOML "
                        "file (default pcb352b)")
    p.add_argument("--settings", "--station", dest="settings", default="default",
                   help="settings key (default) or path to a settings TOML file (default: default)")
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                   help="directory containing shakers/, daq/, sensors/, settings/ and "
                        "test_profiles/ (*.toml); keys resolve there")
    p.add_argument("--level", type=float, default=0.0,
                   help="target level in dB relative to the profile (default 0)")
    p.add_argument("--ai-range", default=None,
                   help="AI range in V (one of the DAQ's ranges) or 'auto': the smallest range "
                        "for the expected peak at --level (default: daq.ai_range_v, normally auto)")


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

    p = sub.add_parser("run", help="run a closed-loop random or sine test")
    _add_common(p)
    _add_run_opts(p)
    p.add_argument("--duration", type=float, default=None,
                   help="random: test duration at full level in s (default: from profile); "
                        "ignored for sine profiles")

    p = sub.add_parser("loopback", help="self-test: AO looped back into a spare AI channel "
                                        "(amplifier disconnected); measures delay and drift")
    p.add_argument("--daq", default="usb6211",
                   help="DAQ device key (usb6211, usb4431, pxie4468, cdaq9234_9260, "
                        "cdaq9232_9263, dt9837a) or path to a DAQ TOML file (default usb6211)")
    p.add_argument("--loopback-channel", required=True,
                   help="AI channel wired to the AO, for example ai1 (IEPE off, DC coupling)")
    p.add_argument("--sensor", default="pcb352b", help=argparse.SUPPRESS)
    p.add_argument("--settings", "--station", dest="settings", default="default",
                   help="settings key or path (default: default)")
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    p.add_argument("--duration", type=float, default=120.0, help="test duration in s (default 120)")
    p.add_argument("--level-v", type=float, default=0.05,
                   help="noise level on the AO in V rms (default 0.05, at most 0.5)")
    p.add_argument("--sim", action="store_true", help="use the simulated DAQ")
    p.add_argument("--shaker", default="tv51110", help="shaker for --sim (default tv51110)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--seed", type=int, default=None, help="random seed")
    return ap


def _print_status(row: dict, last: list) -> None:
    now = time.monotonic()
    if now - last[0] < 0.25:
        return
    last[0] = now
    if row.get("f_hz") is not None and "meas_pk_g" in row:
        text = (f"{row['state']:<11} {row['step']:<7} {row['f_hz']:8.2f} Hz  "
                f"ref {row['ref_pk_g']:7.3f} g  meas {row['meas_pk_g']:7.3f} g  "
                f"err {row['err_db']:+5.2f} dB  drive {row['drive_pk_v']:5.3f} Vpk  "
                f"phase {row['phase_deg']:+6.1f} deg")
    elif "meas_rms_g" in row:
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


def _make_backend(args, settings, shaker, block_io, loopback_channel=None):
    if args.sim:
        from .daq.sim import SimulatedDaq
        return SimulatedDaq(settings, shaker, realtime=getattr(args, "sim_realtime", False),
                            seed=args.seed, loopback_channel=loopback_channel)
    if settings.daq.limits.driver == "uldaq":
        from .daq.ul import UlDaq
        return UlDaq(settings, block_io, loopback_channel)
    from .daq.nidaq import NiDaq
    return NiDaq(settings, block_io, loopback_channel)


def cmd_list_devices() -> int:
    queried = found = 0
    for library, module in (("NI-DAQmx", ".daq.nidaq"), ("UL for Linux", ".daq.ul")):
        try:
            devices = importlib.import_module(module, __package__).list_devices()
        except Exception as exc:
            print(f"cannot query {library}: {exc}", file=sys.stderr)
            continue
        queried += 1
        for name, product, serial in devices:
            print(f"{name:<10} {product:<16} serial {serial}   ({library})")
            found += 1
    if queried and not found:
        print("no DAQ devices found")
    return 0 if queried else 1


def cmd_loopback(args) -> int:
    from .loopback import block_samples, loopback_settings, run_loopback

    try:
        settings = loopback_settings(load_settings(args.settings, args.daq, args.config_dir,
                                                   args.sensor))
        shaker = load_shaker(args.shaker, args.config_dir) if args.sim else None
        d = settings.daq
        if channel_number(args.loopback_channel) == channel_number(d.ai_channel):
            raise ConfigError(f"--loopback-channel must differ from daq.ai_channel ({d.ai_channel})")
        if not 0 < args.level_v <= 0.5:
            raise ConfigError("--level-v must be in (0, 0.5] V rms")
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    print(f"daq    : {d.model}, AO {d.ao_channel} -> AI {args.loopback_channel}, "
          f"{d.fs_io_hz:g} S/s, noise {args.level_v:g} V rms for {args.duration:g} s\n")
    if not args.sim and not args.yes:
        print(f"Disconnect the amplifier from the AO and connect AO {d.ao_channel} to AI "
              f"{args.loopback_channel} (IEPE is switched off on that channel).")
        if input("Type 'yes' to start: ").strip().lower() != "yes":
            print("cancelled")
            return 1
    backend = _make_backend(args, settings, shaker, block_samples(settings), args.loopback_channel)
    try:
        result = run_loopback(settings, backend, duration_s=args.duration, level_v=args.level_v,
                              seed=args.seed)
    except DaqFault as exc:
        print(f"DAQ fault: {exc}")
        return 1
    print(result.format())
    print("\nPASS" if result.ok else "\nFAIL")
    return 0 if result.ok else 2


def _sensor_text(settings) -> str:
    sn = settings.sensor
    return (f"{sn.name} ({settings.v_per_g * 1000:g} mV/g at the DAQ input, "
            f"+/-{sn.range_g_pk:g} g, {sn.f_min_hz:g}-{sn.f_max_hz:g} Hz)")


def _profile_text(profile, level_db: float) -> str:
    if not isinstance(profile, SineProfile):
        return (f"{profile.name} at {level_db:+.1f} dB ({profile.f_lo:g}-{profile.f_hi:g} Hz, "
                f"{profile.accel_rms_g() * 10 ** (level_db / 20):.3f} g rms)")
    k = 10 ** ((level_db + profile.max_level_db) / 20)
    if profile.kind == "sine_sweep":
        f = np.geomspace(profile.f_lo, profile.f_hi, 500)
        what = (f"sweep {profile.f_start_hz:g} -> {profile.f_end_hz:g} Hz, "
                f"{profile.rate_oct_min:g} oct/min, {profile.sweeps} sweep(s)")
    else:
        f = np.array(profile.frequencies_hz)
        what = (f"stepped sine, {len(f)} frequencies {profile.f_lo:g}-{profile.f_hi:g} Hz x "
                f"{len(profile.levels_db)} level(s)"
                + (f", ring-down {profile.ringdown_s:g} s" if profile.ringdown_s else ""))
    return (f"{profile.name} at {level_db:+.1f} dB\n         {what}\n"
            f"         max {np.max(profile.accel_pk_g(f)) * k:.3f} g peak, "
            f"{np.max(profile.displacement_pp_mm(f)) * k:.3f} mm p-p, "
            f"about {profile.duration_estimate_s() / 60:.1f} min")


def make_controller(settings, shaker, profile, backend, **kwargs):
    cls = SineController if isinstance(profile, SineProfile) else Controller
    return cls(settings, shaker, profile, backend, **kwargs)


def _ai_range_override(args, settings):
    """``--ai-range``: None (use daq.ai_range_v), "auto" or one of the DAQ's ranges."""
    value = args.ai_range
    if value is None or value == "auto":
        return value
    lim = settings.daq.limits.ai_ranges_v
    try:
        v = float(value)
    except ValueError:
        v = None
    if v is None or not any(abs(v - r) <= 1e-6 * max(r, 1.0) for r in lim):
        raise ConfigError(f"--ai-range must be 'auto' or one of "
                          f"{', '.join(f'{r:g}' for r in lim)} ({settings.daq.model})")
    return v


def cmd_check(args, settings, shaker, profile, rates) -> int:
    ai = select_ai_range(profile, settings, args.level, args.ai_range)
    report = preflight(profile, shaker, settings, args.level, ai)
    print(f"shaker : {shaker.name}\ndaq    : {settings.daq.model}, AI range {ai.format()}\n"
          f"rates  : {rates.format()}\n"
          f"sensor : {_sensor_text(settings)}\n"
          f"profile: {_profile_text(profile, args.level)}\n")
    print(report.format())
    print("\nPASS" if report.ok else "\nFAIL")
    return 0 if report.ok else 2


def cmd_run(args, settings, shaker, profile, rates, pretest_only: bool) -> int:
    ai = select_ai_range(profile, settings, args.level, args.ai_range)
    report = preflight(profile, shaker, settings, args.level, ai)
    print(f"rates   : {rates.format()}\nAI range: {ai.format()}\n")
    print(report.format())
    if not report.ok:
        print("\npre-flight check FAILED; nothing was output")
        return 2
    if not args.sim and not args.yes:
        print(f"\nAbout to drive {shaker.name} via {settings.daq.model} "
              f"{settings.daq.ao_device or settings.daq.device}/{settings.daq.ao_channel} "
              f"(max {shaker.max_drive_v:g} V).\n"
              f"Control sensor: {_sensor_text(settings)}.\n"
              "Check the amplifier gain, the accelerometer mounting and the charge amplifier "
              "setting.")
        if input("Type 'yes' to start: ").strip().lower() != "yes":
            print("cancelled")
            return 1

    tag = f"{'pretest' if pretest_only else 'run'}_{Path(args.shaker).stem}_{Path(args.profile).stem}"
    sine = isinstance(profile, SineProfile)
    if sine and getattr(args, "duration", None) is not None:
        print("note: --duration is ignored for sine profiles")
    logger = RunLogger(args.log_dir, tag + ("_sim" if args.sim else ""),
                       SINE_STATUS_FIELDS if sine else STATUS_FIELDS)
    block_io = settings.control.frame_size // 2 * settings.daq.decimation
    backend = _make_backend(args, settings, shaker, block_io)
    last = [0.0]
    ctl = make_controller(settings, shaker, profile, backend, target_level_db=args.level,
                          duration_s=None if sine else getattr(args, "duration", None),
                          logger=logger, seed=args.seed, ai_range_v=args.ai_range,
                          on_status=lambda row: _print_status(row, last))
    logger.meta({"version": __version__, "shaker": shaker, "settings": settings,
                 "profile": profile, "level_db": args.level, "ai_range": ai, "rates": rates,
                 "sim": args.sim, "pretest_only": pretest_only,
                 "started": time.strftime("%Y-%m-%dT%H:%M:%S")})

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
              "SNR {snr_db:.1f} dB, mean coherence {coherence_mean:.3f}, "
              "drive {drive_rms_v:.3g} V rms".format(**info)
              + (f", predicted drive {info['predicted_drive_rms_v']:.3f} V rms"
                 if "predicted_drive_rms_v" in info else "")
              + (f", predicted drive {info['predicted_drive_pk_v']:.3f} V peak"
                 if "predicted_drive_pk_v" in info else ""))
    print(f"result : {result.reason}")
    if result.meas_rms_g is not None:
        print(f"final  : ref {result.ref_rms_g:.3f} g rms, meas {result.meas_rms_g:.3f} g rms "
              f"({result.rms_err_db:+.2f} dB), {result.run_time_s:.1f} s at full level")
    if sine and ctl.steps:
        print(f"steps  : {len(ctl.steps)} written to steps.csv"
              + (f", {len(ctl.events)} ring-down(s)" if ctl.events else ""))
    print(f"logs   : {logger.dir}")
    return 0 if result.completed else (1 if result.state == State.ABORTED else 3)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list-devices":
        return cmd_list_devices()
    if args.cmd == "loopback":
        return cmd_loopback(args)
    try:
        settings = load_settings(args.settings, args.daq, args.config_dir, args.sensor)
        shaker = load_shaker(args.shaker, args.config_dir)
        profile = load_profile(resolve_config_path(args.profile, "test_profiles", args.config_dir))
        validate_setup(settings, shaker)
        settings, rates = resolve_rates(profile, shaker, settings)
        args.ai_range = _ai_range_override(args, settings)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        return cmd_check(args, settings, shaker, profile, rates)
    return cmd_run(args, settings, shaker, profile, rates, pretest_only=args.cmd == "pretest")
