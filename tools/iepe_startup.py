"""Record the IEPE start-up of the control accelerometer (AI only, no AO task).

Switches the DAQ's IEPE excitation off for ``--off`` seconds so the sensor discharges, then on,
and records the control channel for ``--seconds``. Runs at the DAQ file's fixed rate (USB-4431:
51.2 kS/s), so the AO is not disturbed and the amplifier may stay on. Writes
``<log-dir>/<stamp>_iepe_startup/iepe_startup.csv`` (t_s, ai_v) and ``meta.json`` with the time
after which ``NiDaq`` would start the run (``wait_until_settled``, same threshold).

usage: python tools/iepe_startup.py [--daq usb4431] [--sensor pcb352b] [--log-dir runs]
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np

from vcactl.config import load_settings
from vcactl.daq.nidaq import SETTLE_FRACTION, SETTLE_HOLD_S, wait_until_settled


def record(d, fs: float, seconds: float, iepe: bool, range_v: float) -> np.ndarray:
    import nidaqmx
    from nidaqmx.constants import (AcquisitionType, Coupling, ExcitationSource,
                                   ExcitationVoltageOrCurrent, TerminalConfiguration)

    n = int(round(fs * seconds))
    with nidaqmx.Task() as task:
        ai = task.ai_channels.add_ai_voltage_chan(
            f"{d.ai_device or d.device}/{d.ai_channel}",
            terminal_config=TerminalConfiguration[d.ai_terminal_config.upper()],
            min_val=-range_v, max_val=range_v)
        ai.ai_coupling = Coupling[d.ai_coupling.upper()]
        if iepe:
            ai.ai_excit_src = ExcitationSource.INTERNAL
            ai.ai_excit_voltage_or_current = ExcitationVoltageOrCurrent.USE_CURRENT
            ai.ai_excit_val = d.iepe_current_ma / 1000
        else:
            ai.ai_excit_src = ExcitationSource.NONE
        task.timing.cfg_samp_clk_timing(fs, sample_mode=AcquisitionType.FINITE,
                                        samps_per_chan=n)
        return np.asarray(task.read(number_of_samples_per_channel=n, timeout=seconds + 10))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daq", default="usb4431")
    ap.add_argument("--sensor", default="pcb352b")
    ap.add_argument("--off", type=float, default=5.0, help="IEPE off before the start (s)")
    ap.add_argument("--seconds", type=float, default=4.0, help="recording after switch-on (s)")
    ap.add_argument("--log-dir", type=Path, default=Path("runs"))
    args = ap.parse_args()

    st = load_settings(daq=args.daq, sensor=args.sensor)
    d = st.daq
    if d.iepe_current_ma <= 0:
        raise SystemExit(f"{args.daq}: iepe_current_ma is 0, nothing to switch on")
    fs = float(d.fs_io_auto_hz[0]) if isinstance(d.fs_io_hz, str) else float(d.fs_io_hz)
    range_v = max(d.limits.ai_ranges_v)
    record(d, fs, args.off, iepe=False, range_v=range_v)
    y = record(d, fs, args.seconds, iepe=True, range_v=range_v)

    chunk = round(0.1 * fs)
    chunks = iter(range(0, len(y), chunk))
    try:
        settle_s = wait_until_settled(lambda: y[next(chunks):][:chunk], fs,
                                      SETTLE_FRACTION * range_v)
    except StopIteration:
        settle_s = None                       # not settled within the recording
    out = args.log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_iepe_startup"
    out.mkdir(parents=True)
    t = np.arange(len(y)) / fs
    np.savetxt(out / "iepe_startup.csv", np.column_stack([t, y]), delimiter=",",
               header="t_s,ai_v", comments="", fmt="%.6e")
    meta = {"daq": d.model, "sensor": st.sensor.name, "fs_hz": fs, "range_v": range_v,
            "iepe_ma": d.iepe_current_ma, "off_s": args.off, "settle_limit_v":
            SETTLE_FRACTION * range_v, "settle_hold_s": SETTLE_HOLD_S, "settle_s": settle_s}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"peak {np.max(np.abs(y)):.2f} V, settled after "
          f"{'-' if settle_s is None else f'{settle_s:.2f}'} s; written to {out}")


if __name__ == "__main__":
    main()
