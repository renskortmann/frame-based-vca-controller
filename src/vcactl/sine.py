"""Closed-loop sine control: logarithmic sine sweep and stepped sine with optional ring-down.

The loop runs on the same half-frame I/O as the random controller (``ControllerBase``), after
the same noise-floor measurement and flat random pretest. The pretest H1 gives the feed-forward
drive amplitude

    D = c * A_ref(f) * L / |H(f)|

where the scalar correction c (dB) is updated every half-frame from the measured amplitude.
The amplitude is measured with a tracking filter: the last frame of the response is demodulated
with the drive phase, delayed by the same I/O delay as the FRF, and Hann-weighted.

Frequency and amplitude change smoothly inside each half-frame (phase-continuous log
frequency, linear amplitude), so the drive has no steps. A ring-down cuts the drive at a zero
crossing, records the control signal with zero drive and logs the cut time (stream time and
approximate wall-clock time) for alignment with an external response measurement.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from .config import Settings, ShakerConfig
from .controller import ControllerBase, History, State, StepResult
from .daq.base import DaqBackend
from .profile import G, SineProfile
from .safety import AbortError

SINE_STATUS_FIELDS = ["t_s", "state", "step", "level_db", "f_hz", "ref_pk_g", "meas_pk_g",
                      "err_db", "drive_pk_v", "phase_deg", "drive_rms_v", "ai_peak_v",
                      "block_rms_g"]
SWEEP_SETTLE_LIMIT_S = 30.0    # time allowed to reach the start amplitude of a sweep
ABORT_BLOCKS = 3               # consecutive half-frames beyond abort_db before aborting
RINGDOWN_PRE_S = 0.1           # response recorded before the cut


@dataclass
class Estimate:
    f_hz: float
    ref_pk_g: float
    meas_pk_g: float
    drive_pk_v: float
    phase_deg: float           # response relative to drive (incl. the sign of H)
    err_db: float              # ref / meas


class SineController(ControllerBase):
    def __init__(self, settings: Settings, shaker: ShakerConfig, profile: SineProfile,
                 backend: DaqBackend, **kwargs):
        super().__init__(settings, shaker, profile, backend, **kwargs)
        self.sc = settings.sine
        self.tol = profile.tolerance
        cap = self.hist_cap
        # per drive sample, aligned with drive_hist (incl. the AO prefill added in run())
        self.phase_hist = History(cap)           # drive phase (rad)
        self.f_hist = History(cap)               # drive frequency (Hz)
        self.ref_hist = History(cap)             # reference amplitude (g peak); 0 = no sine
        self.amp_hist = History(cap)             # drive amplitude (V peak)
        self.step_hist = History(cap, dtype=np.int64)
        prefill = self.P * self.half
        for h in (self.phase_hist, self.f_hist, self.ref_hist, self.amp_hist, self.step_hist):
            h.append(np.zeros(prefill, dtype=h.buf.dtype))

        self.phase = 0.0                         # phase of the last drive sample
        self.f = profile.f_lo
        self.ref_g = 0.0                         # reference amplitude of the last sample
        self.amp_v = 0.0                         # drive amplitude of the last sample
        self.corr_db = 0.0
        self.sine_on = False
        self.step_id = 0
        self.step_label = ""
        self.phase_name = "idle"
        self.est: Estimate | None = None
        self._bad_blocks = 0
        self._capture: tuple[int, int, np.ndarray] | None = None
        self.steps: list[dict] = []
        self.events: list[dict] = []
        self.wsum = float(np.sum(self.spec.window))

    # ------------------------------------------------------------------ setup hooks
    def _pretest_band(self) -> tuple[float, float]:
        """Test band widened by 1/3 octave and 3 lines each side, inside what the shaker,
        the control rate and the sensor allow."""
        p = self.profile
        lo = min(p.f_lo * 2 ** (-1 / 3), p.f_lo - 3 * self.df)
        hi = max(p.f_hi * 2 ** (1 / 3), p.f_hi + 3 * self.df)
        lo = max(lo, 2 * self.df, self.shaker.f_min_hz)
        hi = min(hi, 0.3 * self.fs, self.shaker.f_max_hz)
        if self.settings.sensor.type == "displacement":
            hi = min(hi, self.settings.sensor.f_max_hz)
        return lo, max(hi, p.f_hi)

    def _check_predicted_drive(self) -> None:
        """Feed-forward from the pretest FRF; abort if the drive would exceed the shaker limits."""
        h2 = self._h2_band()
        self._h_logf = np.log(self.freqs[self.band])
        self._h_logmag = 0.5 * np.log(h2)
        p = self.profile
        f = (np.array(p.frequencies_hz) if p.kind == "stepped_sine"
             else np.geomspace(p.f_lo, p.f_hi, 500))
        k = 10 ** ((p.max_level_db + self.target_level_db) / 20)
        drive = p.accel_pk_g(f) * k / np.array([self._h_at(fi) for fi in f])
        d_max = float(np.max(drive))
        self.pretest_info["predicted_drive_pk_v"] = d_max
        limit = min(self.shaker.max_drive_v, math.sqrt(2) * self.shaker.max_drive_rms_v)
        if d_max > limit:
            f_worst = float(f[int(np.argmax(drive))])
            raise AbortError(f"pretest: profile needs about {d_max:.2f} V peak drive at "
                             f"{f_worst:.1f} Hz; limit is {limit:.2f} V peak "
                             "(max_drive_v, sqrt(2) * max_drive_rms_v)")

    # ------------------------------------------------------------------ signal generation
    def _h_at(self, f: float) -> float:
        """|H| (g/V) from the pretest, log-log interpolated over the band lines."""
        return float(np.exp(np.interp(math.log(f), self._h_logf, self._h_logmag)))

    def _feed_forward(self, f: float, ref_g: float) -> float:
        return ref_g / self._h_at(f) * 10 ** (self.corr_db / 20)

    def _ref(self, f: float, level_db: float) -> float:
        return float(self.profile.accel_pk_g(f)) * 10 ** (level_db / 20)

    def _io(self, x: np.ndarray, phase=None, f=None, ref=None, amp=None) -> StepResult:
        n = len(x)
        zeros = np.zeros(n)
        self.phase_hist.append(zeros if phase is None else phase)
        self.f_hist.append(zeros if f is None else f)
        self.ref_hist.append(zeros if ref is None else ref)
        self.amp_hist.append(zeros if amp is None else amp)
        self.step_hist.append(np.full(n, self.step_id))
        sd = super()._io(x)
        if self._capture is not None:
            start, stop, buf = self._capture
            m = self.resp_hist.count
            lo, hi = max(start, m - n), min(stop, m)
            if lo < hi:
                buf[lo - start:hi - start] = self.resp_hist.get(lo, hi)
        return sd

    def _generate(self, f1: float, ref1: float, amp1: float, cut: bool = False):
        """One half-frame from the current state to (f1, ref1, amp1): log frequency, linear
        reference and drive amplitude, continuous phase. ``cut`` stops the sine at its first
        zero crossing; returns the step result and the drive index of the cut (or None)."""
        frac = (np.arange(self.half) + 1) / self.half
        f = self.f * (f1 / self.f) ** frac
        ref = self.ref_g + (ref1 - self.ref_g) * frac
        amp = self.amp_v + (amp1 - self.amp_v) * frac
        phase = self.phase + 2 * np.pi * np.cumsum(f) / self.fs
        x = amp * np.sin(phase)
        cut_index = None
        if cut:
            half_turns = np.floor(phase / np.pi)
            k = int(np.argmax(half_turns != half_turns[0]))   # first sample past a zero crossing
            if k == 0:
                k = self.half
            x[k:] = 0.0
            ref[k:] = 0.0
            amp[k:] = 0.0
            cut_index = self.drive_hist.count + k
            ref1 = amp1 = 0.0
        self.phase = float(phase[-1] % (2 * np.pi))
        self.f, self.ref_g, self.amp_v = f1, ref1, amp1
        sd = self._io(x, np.mod(phase, 2 * np.pi), f, ref, amp)
        return sd, cut_index

    # ------------------------------------------------------------------ measurement
    def _estimate(self) -> Estimate | None:
        """Amplitude and phase at the drive frequency over the last frame (tracking filter).

        None until the frame lies entirely inside one step with the sine on.
        """
        m = self.resp_hist.count
        a, b = m - self.N - self.delay, m - self.delay
        if a < 0:
            return None
        ref = self.ref_hist.get(a, b)
        steps = self.step_hist.get(a, b)
        if np.any(ref <= 0) or np.any(steps != steps[0]):
            return None
        w = self.spec.window
        y = self.resp_hist.get(m - self.N, m)
        y = y - np.sum(w * y) / self.wsum              # remove the (weighted) mean
        Y = np.sum(w * y * np.exp(-1j * self.phase_hist.get(a, b)))
        meas = 2 * abs(Y) / self.wsum
        phase = math.degrees(np.angle(1j * Y))         # y = A sin(phi + theta)
        f = float(np.sum(w * self.f_hist.get(a, b)) / self.wsum)
        if self.displacement:                          # mm -> g; a = -(2 pi f)^2 x
            meas *= (2 * np.pi * f) ** 2 * 1e-3 / G
            phase += 180.0
        ref_f = float(np.sum(w * ref) / self.wsum)
        drive = float(np.sum(w * self.amp_hist.get(a, b)) / self.wsum)
        err = 20 * math.log10(ref_f / meas) if meas > 0 else math.inf
        return Estimate(f, ref_f, float(meas), drive, (phase + 180.0) % 360.0 - 180.0, err)

    # ------------------------------------------------------------------ one half-frame
    def _block(self, f1: float, ref1: float, *, strict: bool = False,
               cut: bool = False) -> tuple[StepResult, Estimate | None, int | None]:
        """Generate, write and read one sine half-frame, then check, correct and report.

        ``strict``: abort when the amplitude error stays beyond the abort tolerance.
        """
        self._check_stop()
        amp1 = self._feed_forward(f1, ref1)
        if amp1 > self.shaker.max_drive_v:
            raise AbortError(f"drive {amp1:.2f} V peak at {f1:.1f} Hz exceeds max_drive_v "
                             f"{self.shaker.max_drive_v:g} V (insufficient drive capability)")
        self.sine_on = True
        sd, cut_index = self._generate(f1, ref1, amp1, cut)
        est = self.est = self._estimate()
        self.monitor.check_io(sd.ai_peak_v, sd.drive_rms_v, sd.clip_fraction)
        self.monitor.check_sensor_range(sd.disp_min_mm, sd.disp_max_mm)
        if est is not None:
            self.monitor.check_response_peak(est.meas_pk_g)
            self.monitor.check_open_loop(est.meas_pk_g, est.ref_pk_g)
            if strict and abs(est.err_db) > self.tol.abort_db:
                self._bad_blocks += 1
                if self._bad_blocks >= ABORT_BLOCKS:
                    raise AbortError(f"amplitude error {est.err_db:+.2f} dB at {est.f_hz:.1f} Hz "
                                     f"exceeds +/-{self.tol.abort_db} dB")
            else:
                self._bad_blocks = 0
            step = float(np.clip(self.sc.correction_gain * est.err_db,
                                 -self.sc.max_step_db, self.sc.max_step_db))
            self.corr_db = float(np.clip(self.corr_db + step, -self.sc.max_correction_db,
                                         self.sc.max_correction_db))
        self.run_time_s += self.half / self.fs
        self._report(sd, est)
        return sd, est, cut_index

    def _zero_block(self) -> StepResult:
        """Zero drive (ring-down), with the I/O checks."""
        self._check_stop()
        sd = self._io(np.zeros(self.half))
        self.monitor.check_io(sd.ai_peak_v, sd.drive_rms_v, sd.clip_fraction)
        self.monitor.check_sensor_range(sd.disp_min_mm, sd.disp_max_mm)
        self.run_time_s += self.half / self.fs
        self._report(sd, None)
        return sd

    def _report(self, sd: StepResult, est) -> None:
        in_test = self.state in (State.RUN, State.RAMPDOWN)
        row = {"t_s": round(self.t_s, 4),
               "state": self.phase_name if in_test else self.state.value,
               "step": self.step_label, "drive_rms_v": sd.drive_rms_v,
               "ai_peak_v": sd.ai_peak_v, "block_rms_g": sd.block_rms_g}
        if isinstance(est, Estimate):
            row.update(level_db=round(self.level_db, 2), f_hz=est.f_hz, ref_pk_g=est.ref_pk_g,
                       meas_pk_g=est.meas_pk_g, err_db=est.err_db, drive_pk_v=est.drive_pk_v,
                       phase_deg=est.phase_deg)
        if self.logger:
            self.logger.status(row)
        if self.on_status:
            self.on_status(row)

    # ------------------------------------------------------------------ building blocks
    def _ramp_in(self, f: float, level_db: float) -> None:
        """From silence: ramp the reference from start_level_db below ``level_db`` up to it."""
        self.phase_name = "ramp"
        n = max(1, math.ceil(self.sc.ramp_s * self.fs / self.half))
        start = level_db + self.c.start_level_db
        for i in range(1, n + 1):
            self._block(f, self._ref(f, start + (level_db - start) * i / n))

    def _settle(self, f: float, level_db: float, min_s: float, max_s: float) -> None:
        """Hold (f, level) until ``settled_blocks`` estimates are within alarm_db."""
        self.phase_name = "settle"
        t0, good = self.t_s, 0
        while True:
            _, est, _ = self._block(f, self._ref(f, level_db))
            good = good + 1 if est is not None and abs(est.err_db) <= self.tol.alarm_db else 0
            if self.t_s - t0 >= min_s and good >= self.sc.settled_blocks:
                return
            if self.t_s - t0 > max_s:
                raise AbortError(f"could not reach {self._ref(f, level_db):.3g} g peak at "
                                 f"{f:.1f} Hz within {max_s:g} s")

    def _new_step(self, label: str, level_db: float) -> None:
        self.step_id += 1
        self.step_label = label
        self.level_db = level_db

    # ------------------------------------------------------------------ test types
    def _closed_loop(self) -> None:
        self.state = State.RUN
        # fade out the random pretest noise, then switch to sine
        if np.any(self._drive_psd):
            self._step(self._drive_psd, np.linspace(1.0, 0.0, self.half))
            self.gen.reset()
            self._drive_psd = np.zeros(len(self.freqs))
        self.track_spectra = False
        if self.profile.kind == "sine_sweep":
            self._sweep()
        else:
            self._stepped()

    def _sweep(self) -> None:
        p = self.profile
        level = self.target_level_db
        self._new_step("sweep", level)
        self.f = p.f_start_hz
        self._ramp_in(p.f_start_hz, level)
        self._settle(p.f_start_hz, level, 0.0, SWEEP_SETTLE_LIMIT_S)
        d_oct = p.rate_oct_min / 60 * self.half / self.fs        # octaves per half-frame
        for i in range(p.sweeps):
            f_to = p.f_end_hz if i % 2 == 0 else p.f_start_hz
            sign = 1.0 if f_to > self.f else -1.0
            self.phase_name = f"sweep {i + 1}/{p.sweeps}"
            while self.f != f_to:
                f1 = self.f * 2 ** (sign * d_oct)
                if (f1 - f_to) * sign >= 0:
                    f1 = f_to
                self._block(f1, self._ref(f1, level), strict=True)

    def _stepped(self) -> None:
        p = self.profile
        silent = True
        n_dwell = max(1, math.ceil(p.dwell_s * self.fs / self.half))
        for li, lvl in enumerate(p.levels_db):
            level = lvl + self.target_level_db
            for fi, f in enumerate(p.frequencies_hz):
                self._new_step(f"L{li + 1}F{fi + 1}", level)
                if silent:
                    self.f = f
                    self._ramp_in(f, level)
                self._settle(f, level, p.settle_s, p.max_settle_s)

                self.phase_name = "dwell"
                t_start, wall_start = self.t_s, self._wall(self.drive_hist.count)
                ests = []
                for _ in range(n_dwell):
                    _, est, _ = self._block(f, self._ref(f, level), strict=True)
                    if est is not None:
                        ests.append(est)
                if not ests:
                    raise AbortError(f"no valid amplitude estimate during the dwell at {f:.1f} Hz")
                row = self._step_row(li, fi, level, f, ests, t_start, wall_start)
                self.steps.append(row)
                if self.logger:
                    self.logger.append_row("steps", row)

                silent = p.ringdown_s > 0
                if silent:
                    self._ringdown(f, level, p.ringdown_s)

    def _step_row(self, li, fi, level, f, ests, t_start, wall_start) -> dict:
        meas = float(np.mean([e.meas_pk_g for e in ests]))
        ref = float(np.mean([e.ref_pk_g for e in ests]))
        drive = float(np.mean([e.drive_pk_v for e in ests]))
        ph = float(np.degrees(np.angle(np.mean(np.exp(1j * np.radians(
            [e.phase_deg for e in ests]))))))
        return {"step": self.step_label, "level_index": li + 1, "freq_index": fi + 1,
                "level_db": level, "f_hz": f, "ref_pk_g": ref, "meas_pk_g": meas,
                "err_db": 20 * math.log10(ref / meas), "drive_pk_v": drive,
                "h_mag_g_per_v": meas / drive, "h_phase_deg": ph,
                "displacement_pp_mm": 2 * meas * G / (2 * math.pi * f) ** 2 * 1e3,
                "t_start_s": round(t_start, 4), "t_end_s": round(self.t_s, 4),
                "wall_start": wall_start, "estimates": len(ests)}

    def _wall(self, drive_index: int) -> str:
        """Approximate wall-clock time at which drive sample ``drive_index`` leaves the DAQ."""
        t = self.t0_wall + drive_index / self.fs
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"

    def _ringdown(self, f: float, level: float, seconds: float) -> None:
        """Cut the drive at a zero crossing and record the decay with zero drive."""
        self.phase_name = "ringdown"
        n_ring = round(seconds * self.fs)
        # pre-cut samples must not have been read yet: AI runs P half-frames behind AO
        pre = min(round(RINGDOWN_PRE_S * self.fs), self.P * self.half + self.delay)
        # the cut drive sample is index c; the response sees it at c + delay
        start_guess = self.drive_hist.count + self.delay - pre
        buf = np.full(pre + self.half + n_ring, np.nan)
        self._capture = (start_guess, start_guess + len(buf), buf)
        _, _, cut = self._block(f, self._ref(f, level), cut=True)
        self.sine_on = False
        event = {"event": "drive_cut", "step": self.step_label, "level_db": level, "f_hz": f,
                 "t_stream_s": cut / self.fs, "wall_clock": self._wall(cut),
                 "ringdown_s": seconds}
        self.events.append(event)
        if self.logger:
            self.logger.append_row("events", event)
        stop = cut + self.delay + n_ring
        while self.resp_hist.count < stop:
            self._zero_block()
        self._capture = None
        a = cut + self.delay - pre - start_guess
        data = buf[a:a + pre + n_ring]
        t = (np.arange(len(data)) - pre) / self.fs
        if self.displacement:
            cols = {"t_s": t, "response_mm": data + self.sensor.offset_mm}
        else:
            cols = {"t_s": t, "response_g": data}
        if self.logger:
            self.logger.timeseries(f"ringdown_{self.step_label}", cols)
        self.last_ringdown = cols

    # ------------------------------------------------------------------ stop
    def _rampdown(self, seconds: float) -> None:
        """Fade the sine to zero at constant frequency, then flush the AO queue with zeros."""
        if not self.sine_on:
            super()._rampdown(seconds)
            return
        self.state = State.RAMPDOWN
        self.phase_name = "rampdown"
        n = max(1, math.ceil(seconds * self.fs / self.half))
        a0, r0 = self.amp_v, self.ref_g
        for i in range(1, n + 1):
            sd, _ = self._generate(self.f, r0 * (1 - i / n), a0 * (1 - i / n))
            self.monitor.check_io(sd.ai_peak_v, 0.0, 0.0)
        self.sine_on = False
        for _ in range(self.P + 2):
            self._io(np.zeros(self.half))
