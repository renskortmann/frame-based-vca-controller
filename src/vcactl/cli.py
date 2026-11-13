"""Command-line interface: ``vcactl {list-devices,check,pretest,run,loopback,record,tap}``."""

from __future__ import annotations

import argparse
import importlib
import math
import signal
import sys
import time
from pathlib import Path

import numpy as np

from . import __version__
from .config import (DEFAULT_CONFIG_DIR, ConfigError, load_channels, load_shaker, load_settings,
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
                   help="test profile key (low_g, low_g_10k, flat, sloped, wideband, sine_sweep, "
                        "stepped_sine, ringdown) or path to a profile TOML file (random, sine_sweep or stepped_sine)")
    p.add_argument("--daq", default="usb6211",
                   help="DAQ device key (usb6211, usb4431, pxie4468, cdaq9234_9260, cdaq9232_9263, "
                        "dt9837a) or path to a DAQ TOML file (default usb6211)")
    p.add_argument("--sensor", default=None,
                   help="control accelerometer key (pcb352b, pcb356b18) or path to a sensor TOML "
                        "file (default pcb352b; not with a channels file that has a control "
                        "channel)")
    p.add_argument("--channels", default=None,
                   help="channels key (cantilever_driven, ...) or path to a channels TOML file: "
                        "the control channel and the channels recorded alongside it "
                        "(default: the DAQ file's control channel only)")
    p.add_argument("--settings", "--station", dest="settings", default="default",
                   help="settings key (default) or path to a settings TOML file (default: default)")
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
                   help="directory containing shakers/, daq/, sensors/, channels/, settings/ and "
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
    p.add_argument("--record", action="store_true",
                   help="also save the raw drive and response (raw.f32, about 0.4 MB/s at "
                        "51.2 kS/s)")
    _add_sim_aux(p)


def _add_sim_aux(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sim-aux-ppm", type=float, default=0.0, help=argparse.SUPPRESS)
    p.add_argument("--sim-aux-offset-ms", type=float, default=0.0, help=argparse.SUPPRESS)


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
    p.add_argument("--open-loop", type=float, default=None, metavar="HZ",
                   help="sine sweep without control, for comparison: the drive is calibrated "
                        "once at HZ from the pretest and never corrected (no amplitude "
                        "tolerance; the safety checks stay)")

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

    p = sub.add_parser("record", help="record every channel without drive (background: "
                                      "amplifier off and on)")
    _add_input_opts(p)
    p.add_argument("--seconds", type=float, default=60.0, help="recording length (default 60 s)")

    p = sub.add_parser("tap", help="impact-hammer tap test: triggered records, H1 and coherence")
    _add_input_opts(p)
    p.add_argument("--profile", required=True,
                   help="tap profile key (tap_beam) or path to a tap profile TOML file")
    p.add_argument("--confirm", action="store_true",
                   help="ask to accept each hit that passes the checks (default: accept)")
    return ap


def _add_input_opts(p: argparse.ArgumentParser) -> None:
    """Options of the input-only commands (record, tap)."""
    p.add_argument("--daq", default="usb6211",
                   help="DAQ device key or path to a DAQ TOML file (default usb6211)")
    p.add_argument("--channels", default=None,
                   help="channels key or path: the channels to record (default: the DAQ file's "
                        "control channel with --sensor)")
    p.add_argument("--sensor", default=None,
                   help="sensor of the DAQ file's control channel (default pcb352b)")
    p.add_argument("--settings", "--station", dest="settings", default="default",
                   help="settings key or path (default: default)")
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    p.add_argument("--sim", action="store_true", help="use the simulated DAQ")
    p.add_argument("--shaker", default="tv51110", help="shaker for --sim (default tv51110)")
    p.add_argument("--log-dir", type=Path, default=Path("runs"), help="root directory for run logs")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--seed", type=int, default=None, help="random seed")
    _add_sim_aux(p)


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


def _make_backend(args, settings, shaker, block_io, loopback_channel=None, tap=None):
    if args.sim:
        from .daq.sim import SimulatedDaq
        return SimulatedDaq(settings, shaker, realtime=getattr(args, "sim_realtime", False),
                            seed=args.seed, loopback_channel=loopback_channel, tap=tap,
                            aux_ppm=getattr(args, "sim_aux_ppm", 0.0),
                            aux_offset_s=getattr(args, "sim_aux_offset_ms", 0.0) / 1000)
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


def cmd_record(args) -> int:
    from .record import record_settings, run_record

    try:
        settings = record_settings(load_run_settings(args))
        shaker = load_shaker(args.shaker, args.config_dir) if args.sim else None
        if args.seconds <= 0:
            raise ConfigError("--seconds must be > 0")
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    d = settings.daq
    print(f"daq    : {d.model}, {d.fs_io_hz:g} S/s, {args.seconds:g} s, no drive"
          + (" (AO: clock markers only; keep the amplifier OFF)" if settings.aux else
             " (AO not used)"))
    print(_channels_text(settings) or f"sensor : {_sensor_text(settings)}")
    print(_aux_text(settings), end="")
    if not args.sim and not args.yes:
        if input("Type 'yes' to start: ").strip().lower() != "yes":
            print("cancelled")
            return 1
    from .record import InputStream, block_samples
    names = InputStream(settings, None, 1).names
    logger = RunLogger(args.log_dir, "record" + ("_sim" if args.sim else ""),
                       ["t_s"] + [f"{n}_rms" for n in names])
    backend = _make_backend(args, settings, shaker, block_samples(settings))
    meta = {"version": __version__, "command": "record", "settings": settings,
            "seconds": args.seconds, "sim": args.sim,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "raw": {**raw_meta(settings, d.fs_io_hz, drive=False), "columns": names}}
    _attach_aux(settings, backend, logger, meta)
    logger.meta(meta)
    stop = [False]

    def request_stop(signum, frame):
        stop[0] = True

    old_int = signal.signal(signal.SIGINT, request_stop)
    try:
        result = run_record(settings, backend, logger, args.seconds, lambda: stop[0],
                            lambda t: (sys.stdout.write(f"\rrecording {t:6.1f} s"),
                                       sys.stdout.flush()))
    except DaqFault as exc:
        print(f"\nDAQ fault: {exc}")
        return 1
    finally:
        signal.signal(signal.SIGINT, old_int)
        logger.close()
    print()
    settle_s = getattr(backend, "settle_s", None)
    if settle_s:
        meta["iepe_settle_s"] = round(settle_s, 2)
    meta["recorded_s"] = result.seconds
    _finish_aux(settings, backend, meta)
    logger.meta(meta)
    print(result.format())
    print(f"result : {result.reason}\nlogs   : {logger.dir}")
    return 0 if result.completed else 1


def cmd_tap(args) -> int:
    from .daq.sim import TapSim
    from .record import block_samples, record_settings
    from .tap import TapTest, load_tap_profile

    try:
        settings = record_settings(load_run_settings(args))
        profile = load_tap_profile(resolve_config_path(args.profile, "test_profiles",
                                                       args.config_dir))
        shaker = load_shaker(args.shaker, args.config_dir) if args.sim else None
        tap_sim = TapSim(interval_s=profile.record_s + 0.5) if args.sim else None
        backend = _make_backend(args, settings, shaker, block_samples(settings), tap=tap_sim)

        def ask(hit):
            answer = input(f"\nhit {hit.index}: {hit.peak_n:.1f} N, accept? [Y/n] ")
            return answer.strip().lower() not in ("n", "no")

        logger = None
        tap = TapTest(settings, backend, profile, confirm=ask if args.confirm else None,
                      confirm_async=args.confirm,
                      on_hit=lambda h: print(f"hit {h.index:3d}: {h.peak_n:7.1f} N "
                                             + ("accepted" if h.accepted else "rejected")
                                             + (" (double hit)" if h.double_hit else "")
                                             + (f" (overload: {', '.join(h.overload)})"
                                                if h.overload else "")))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    d = settings.daq
    print(f"daq    : {d.model}, {d.fs_io_hz:g} S/s, no drive"
          + (" (AO: clock markers only; keep the amplifier OFF)" if settings.aux else
             " (AO not used)"))
    print(_channels_text(settings) or f"sensor : {_sensor_text(settings)}")
    print(_aux_text(settings), end="")
    print(f"profile: {profile.name or Path(args.profile).stem}: {profile.hits} hits, "
          f"{profile.record_s:g} s records, trigger {profile.trigger_n:g} N\n")
    if not args.sim and not args.yes:
        if input("Type 'yes' to start, then tap: ").strip().lower() != "yes":
            print("cancelled")
            return 1
    logger = RunLogger(args.log_dir, "tap_" + Path(args.profile).stem
                       + ("_sim" if args.sim else ""), ["t_s"])
    tap.logger = logger
    meta = {"version": __version__, "command": "tap", "settings": settings, "profile": profile,
            "sim": args.sim, "tau_w_s": tap.tau_w if math.isfinite(tap.tau_w) else None,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hits": {"file": "hits.f32", "dtype": "float32", "fs_hz": d.fs_io_hz,
                     "record_samples": tap.n_rec, "pretrigger_samples": tap.n_pre,
                     "columns": tap.stream.names}}
    _attach_aux(settings, backend, logger, meta)
    logger.meta(meta)
    stop = [False]
    old_int = signal.signal(signal.SIGINT, lambda signum, frame: stop.__setitem__(0, True))
    try:
        reason = tap.run(lambda: stop[0])
    except DaqFault as exc:
        print(f"DAQ fault: {exc}")
        return 1
    finally:
        signal.signal(signal.SIGINT, old_int)
        logger.close()
    meta["accepted_hits"] = tap.accepted
    meta["total_hits"] = len(tap.hits)
    _finish_aux(settings, backend, meta)
    logger.meta(meta)
    print(f"result : {reason}\nlogs   : {logger.dir}")
    return 0 if tap.accepted >= profile.hits else 1


def _sensor_text(settings) -> str:
    sn = settings.sensor
    return (f"{sn.name} ({settings.v_per_g * 1000:g} mV/g at the DAQ input, "
            f"+/-{sn.range_g_pk:g} g, {sn.f_min_hz:g}-{sn.f_max_hz:g} Hz)")


def _channels_text(settings) -> str:
    """One line per AI channel (only when a channels file adds channels)."""
    if not settings.extra_channels:
        return ""
    lines = [f"channels: {settings.channels_name}"]
    for c in settings.channels:
        rng = "per run" if c.range_v == 0 else f"+/-{c.range_v:g} V"
        iepe = f"IEPE {c.iepe_current_ma:g} mA" if c.iepe_current_ma > 0 else "IEPE off"
        what = "AO loopback" if c.role == "loopback" else \
            f"{c.sensor.name}, {c.mv_per_unit:g} mV/{c.unit}"
        lines.append(f"  {c.name:<12} {c.physical:<5} {c.role:<8} {what}; {c.coupling}, {iepe}, "
                     f"range {rng}")
    return "\n".join(lines) + "\n"


def raw_meta(settings, fs_hz: float, file: str = "raw.f32", drive: bool = True) -> dict:
    """``meta.json["raw"]``: the columns of a raw recording, their units and sensitivities."""
    cols, units, sens = [], [], []
    if drive:
        cols, units, sens = ["drive_v"], ["V"], [None]
    for c in settings.channels:
        if c.role == "control":
            cols.append("response_g")
        else:
            cols.append(f"{c.name}_{c.unit}")
        units.append(c.unit)
        sens.append(None if c.role == "loopback" else c.mv_per_unit)
    return {"file": file, "dtype": "float32", "fs_hz": fs_hz, "columns": cols, "units": units,
            "sensitivity_mv_per_unit": sens}


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
          f"{_channels_text(settings)}{_aux_text(settings)}"
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
              f"{_channels_text(settings)}"
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
    f_cal = getattr(args, "open_loop", None)
    extra = {"open_loop_cal_hz": f_cal} if f_cal is not None else {}
    if f_cal is not None:
        print(f"open loop: drive calibrated at {f_cal:g} Hz and not corrected (comparison run)")
    ctl = make_controller(settings, shaker, profile, backend, target_level_db=args.level,
                          duration_s=None if sine else getattr(args, "duration", None),
                          logger=logger, seed=args.seed, ai_range_v=args.ai_range,
                          record=args.record, on_status=lambda row: _print_status(row, last),
                          **extra)
    meta = {"version": __version__, "shaker": shaker, "settings": settings,
            "profile": profile, "level_db": args.level, "ai_range": ai, "rates": rates,
            "sim": args.sim, "pretest_only": pretest_only, "open_loop_cal_hz": f_cal,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _attach_aux(settings, backend, logger, meta)
    if args.record:
        meta["raw"] = raw_meta(settings, settings.fs_control_hz)
        meta["raw"]["response_lag_samples"] = ctl.response_lag
    if getattr(ctl, "periodic", False):
        meta["periodic"] = {"period_samples": ctl.period_n,
                            "period_s": ctl.period_n / settings.fs_control_hz}
    logger.meta(meta)

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
    settle_s = getattr(backend, "settle_s", None)   # NiDaq with IEPE on: wait for the sensor
    if settle_s:
        print(f"IEPE   : sensor output settled after {settle_s:.1f} s")
        meta["iepe_settle_s"] = round(settle_s, 2)
    _finish_aux(settings, backend, meta)
    logger.meta(meta)
    info = getattr(ctl, "pretest_info", None)
    if info:
        print("pretest: response {response_rms_g:.3g} g rms, noise {noise_rms_g:.3g} g rms, "
              "SNR {snr_db:.1f} dB, mean coherence {coherence_mean:.3f}, "
              "drive {drive_rms_v:.3g} V rms".format(**info)
              + (f", predicted drive {info['predicted_drive_rms_v']:.3f} V rms"
                 if "predicted_drive_rms_v" in info else "")
              + (f", predicted drive {info['predicted_drive_pk_v']:.3f} V peak"
                 if "predicted_drive_pk_v" in info else "")
              + (f", predicted open-loop peak {info['predicted_open_loop_pk_g']:.2f} g"
                 if "predicted_open_loop_pk_g" in info else ""))
    print(f"result : {result.reason}")
    if result.meas_rms_g is not None:
        print(f"final  : ref {result.ref_rms_g:.3f} g rms, meas {result.meas_rms_g:.3f} g rms "
              f"({result.rms_err_db:+.2f} dB), {result.run_time_s:.1f} s at full level")
    if sine and ctl.steps:
        print(f"steps  : {len(ctl.steps)} written to steps.csv"
              + (f", {len(ctl.events)} ring-down(s)" if ctl.events else ""))
    print(f"logs   : {logger.dir}")
    return 0 if result.completed else (1 if result.state == State.ABORTED else 3)


def _attach_aux(settings, backend, logger, meta: dict) -> None:
    """With an aux DAQ: its samples and the AO go to aux.f32 and ao.f32 (vcactl.analysis.sync)."""
    if settings.aux is None:
        return
    from .daq.aux_daq import aux_meta
    backend.sink = logger.raw_block
    d = settings.daq
    meta["aux"] = aux_meta(settings.aux, d.fs_io_hz, d.limits.filter_delay_at(d.fs_io_hz))


def _finish_aux(settings, backend, meta: dict) -> None:
    aux = getattr(backend, "aux", None)          # NiDaq: the convert rate DAQmx chose
    if settings.aux is not None and getattr(aux, "conv_rate", None):
        meta["aux"]["convert_rate_hz"] = aux.conv_rate


def _aux_text(settings) -> str:
    aux = settings.aux
    if aux is None:
        return ""
    lines = [f"aux    : {aux.daq.model} ({aux.daq.device}), {aux.rate_hz:g} S/s, start trigger "
             f"{aux.daq.device}/{aux.trigger_out} -> {settings.daq.device}/{aux.trigger_in}"]
    for c in aux.channels:
        what = "AO drive (BNC tee at the amplifier input)" if c.role == "drive_ref" else \
            f"{c.sensor.name}, {c.mv_per_unit:g} mV/{c.unit}"
        lines.append(f"  {c.name:<12} {c.physical:<5} {c.role:<9} {what}; range +/-{c.range_v:g} V")
    return "\n".join(lines) + "\n"


def load_run_settings(args):
    """Settings, DAQ, sensor and, with ``--channels``, the channels file."""
    settings = load_settings(args.settings, args.daq, args.config_dir, args.sensor or "pcb352b")
    if getattr(args, "channels", None):
        settings = load_channels(args.channels, settings, args.config_dir,
                                 sensor_given=args.sensor is not None)
    return settings


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list-devices":
        return cmd_list_devices()
    if args.cmd == "loopback":
        return cmd_loopback(args)
    if args.cmd == "record":
        return cmd_record(args)
    if args.cmd == "tap":
        return cmd_tap(args)
    try:
        settings = load_run_settings(args)
        if not settings.has_control:
            raise ConfigError(f"the channels file '{settings.channels_name}' has no control "
                              f"channel; {args.cmd} needs one")
        shaker = load_shaker(args.shaker, args.config_dir)
        profile = load_profile(resolve_config_path(args.profile, "test_profiles", args.config_dir))
        validate_setup(settings, shaker)
        settings, rates = resolve_rates(profile, shaker, settings)
        if settings.aux is not None and settings.daq.decimation != 1:
            raise ConfigError("an aux DAQ needs decimation 1 (a delta-sigma control DAQ)")
        args.ai_range = _ai_range_override(args, settings)
        f_cal = getattr(args, "open_loop", None)
        if f_cal is not None and not (isinstance(profile, SineProfile)
                                      and profile.kind == "sine_sweep"
                                      and profile.f_lo <= f_cal <= profile.f_hi):
            raise ConfigError("--open-loop needs a sine sweep profile and a frequency inside "
                              "its band")
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        return cmd_check(args, settings, shaker, profile, rates)
    return cmd_run(args, settings, shaker, profile, rates, pretest_only=args.cmd == "pretest")
