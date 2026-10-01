"""Frame-based closed-loop random vibration controller.

Each loop iteration (one half-frame of N/2 control-rate samples):

1. synthesize the next drive half-frame from the current drive PSD (random phase, overlap-add),
2. interpolate to the I/O rate, clip, and queue it on AO,
3. read the matching amount of AI, decimate, and scale to g,
4. pair the latest N response samples with the drive samples that caused them and update the
   averaged spectra (H1 FRF, coherence, response PSD),
5. correct the drive PSD per line, run the safety checks and advance the level schedule.

Level handling: every change of drive definition starts a new "segment". Response spectra are
only averaged from frames lying entirely inside one run segment and are normalized by that
segment's level, so averages remain valid across level steps.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import numpy as np
from scipy.ndimage import uniform_filter1d

from .config import ShakerConfig, Settings, validate_setup
from .daq.base import DaqBackend, DaqFault
from .drive import RandomDriveGenerator, flat_psd
from .dsp.resample import Decimator, Interpolator, design_lowpass, total_delay_control_samples
from .dsp.spectral import AutoSpectrumAverager, CrossSpectrumAverager, Spectrum
from .logger import RunLogger
from .profile import Profile
from .safety import AbortError, RuntimeMonitor, preflight


class State(str, Enum):
    IDLE = "idle"
    NOISE = "noise"
    PRETEST = "pretest"
    RAMP = "ramp"
    RUN = "run"
    RAMPDOWN = "rampdown"
    DONE = "done"
    ABORTED = "aborted"


class OperatorStop(AbortError):
    """Stop requested by the operator (Ctrl-C / SIGTERM): normal ramp-down."""


class History:
    """Ring buffer addressed by absolute sample index."""

    def __init__(self, capacity: int, dtype=float):
        self.buf = np.zeros(capacity, dtype=dtype)
        self.cap = capacity
        self.count = 0

    def append(self, x: np.ndarray) -> None:
        n = len(x)
        if n > self.cap:
            raise ValueError("block larger than history")
        self.buf[(self.count + np.arange(n)) % self.cap] = x
        self.count += n

    def get(self, start: int, stop: int) -> np.ndarray:
        if start < 0 or stop > self.count or start < self.count - self.cap:
            raise IndexError(f"history range [{start}, {stop}) not available")
        return self.buf[np.arange(start, stop) % self.cap]


@dataclass
class StepResult:
    drive_rms_v: float
    drive_peak_v: float
    clip_fraction: float
    ai_peak_v: float
    block_rms_g: float


@dataclass
class Evaluation:
    ref_rms_g: float
    meas_rms_g: float
    rms_err_db: float
    lines_alarm: int
    lines_abort: int
    n_lines: int


@dataclass
class RunResult:
    completed: bool
    state: State
    reason: str
    run_time_s: float
    ref_rms_g: float | None
    meas_rms_g: float | None
    rms_err_db: float | None
    log_dir: str | None


class Controller:
    def __init__(self, settings: Settings, shaker: ShakerConfig, profile: Profile,
                 backend: DaqBackend, *, target_level_db: float = 0.0,
                 duration_s: float | None = None, logger: RunLogger | None = None,
                 seed: int | None = None,
                 on_status: Callable[[dict], None] | None = None):
        validate_setup(settings, shaker)
        self.settings, self.shaker, self.profile = settings, shaker, profile
        self.backend, self.logger, self.on_status = backend, logger, on_status
        c, d = settings.control, settings.daq
        self.c, self.s = c, settings.safety
        self.target_level_db = target_level_db
        self.duration_s = profile.duration_s if duration_s is None else duration_s

        self.R = d.decimation
        self.fs = settings.fs_control_hz
        self.N = c.frame_size
        self.half = self.N // 2
        self.block_io = self.half * self.R
        self.P = d.ao_queue_blocks

        self.spec = Spectrum(self.N, self.fs)
        self.freqs, self.df = self.spec.freqs, self.spec.df
        self.band = (self.freqs >= profile.f_lo) & (self.freqs <= profile.f_hi)
        self.s_ref = profile.psd(self.freqs)          # at 0 dB
        self.v_per_g = settings.sensor.sensitivity_mv_per_g / 1000

        taps = design_lowpass(d.fs_io_hz, self.R)
        self.interp = Interpolator(taps, self.R)
        self.decim = Decimator(taps, self.R)
        # drive -> response delay: FIR resampling plus the DAQ's converter filters (DSA devices)
        self.delay = (total_delay_control_samples(taps, self.R)
                      + round(d.limits.filter_delay_samples / self.R))

        self.gen = RandomDriveGenerator(self.N, self.fs, np.random.default_rng(seed))
        cap = 4 * self.N + self.delay + (self.P + 2) * self.half
        self.drive_hist = History(cap)
        self.seg_hist = History(cap, dtype=np.int64)
        self.resp_hist = History(cap)

        self.frf = CrossSpectrumAverager(c.frf_averages)
        self.resp_ctrl = AutoSpectrumAverager(c.control_dof / 2)
        self.resp_disp = AutoSpectrumAverager(c.dof / 2)
        self.noise = AutoSpectrumAverager(1e9)       # linear average

        self.seg = 0
        self.seg_level = {0: 0.0}
        self.seg_kind = {0: "silence"}
        self.seg_frames: dict[int, int] = defaultdict(int)
        self.corr_db = np.zeros(len(self.freqs))
        self.level_db = target_level_db
        self.state = State.IDLE
        self.samples = 0
        self.run_time_s = 0.0
        self.monitor = RuntimeMonitor(settings, shaker)
        self.stop_requested = False
        self._drive_psd = np.zeros(len(self.freqs))
        self._abort_lines_blocks = 0
        self.last_eval: Evaluation | None = None

    # ------------------------------------------------------------------ helpers
    @property
    def t_s(self) -> float:
        return self.samples / self.fs

    def _new_segment(self, kind: str, level_lin: float) -> int:
        self.seg += 1
        self.seg_kind[self.seg] = kind
        self.seg_level[self.seg] = level_lin
        return self.seg

    def _set_level(self, level_db: float) -> None:
        self.level_db = level_db
        self._new_segment("run", 10 ** (level_db / 10))

    def _step(self, psd_norm: np.ndarray, envelope: np.ndarray | None = None) -> StepResult:
        """Generate, write and read one half-frame; update spectra."""
        seg = self.seg
        x = self.gen.next_block(psd_norm * self.seg_level[seg])
        if envelope is not None:
            x = x * envelope
        self.drive_hist.append(x)
        self.seg_hist.append(np.full(self.half, seg))

        y = self.interp.process(x)
        lim = self.shaker.max_drive_v
        clipped = np.abs(y) > lim
        y = np.clip(y, -lim, lim)
        self.backend.write(y)

        ai = self.backend.read(self.block_io)
        r = self.decim.process(ai) / self.v_per_g
        self.resp_hist.append(r)
        self.samples += self.half
        self._update_spectra()
        return StepResult(drive_rms_v=float(np.sqrt(np.mean(y**2))),
                          drive_peak_v=float(np.max(np.abs(y))),
                          clip_fraction=float(np.mean(clipped)),
                          ai_peak_v=float(np.max(np.abs(ai))),
                          block_rms_g=float(np.std(r)))

    def _update_spectra(self) -> None:
        m = self.resp_hist.count
        start = m - self.N - self.delay
        if m < self.N or start < 0:
            return
        y = self.resp_hist.get(m - self.N, m)
        x = self.drive_hist.get(start, start + self.N)
        segs = self.seg_hist.get(start, start + self.N)
        X, Y = self.spec.fft(x), self.spec.fft(y)
        if np.any(x):
            self.frf.update(X, Y, self.spec.scale)
        seg = int(segs[0])
        if not np.all(segs == seg):
            return
        self.seg_frames[seg] += 1
        kind = self.seg_kind[seg]
        gyy = np.abs(Y) ** 2 * self.spec.scale
        if kind == "silence":
            self.noise.update(gyy)
        elif kind == "run":
            gyy /= self.seg_level[seg]
            self.resp_ctrl.update(gyy)
            self.resp_disp.update(gyy)

    def _band_rms(self, psd: np.ndarray) -> float:
        return float(np.sqrt(np.sum(psd[self.band]) * self.df))

    def _run_drive_psd(self) -> np.ndarray:
        """Drive PSD (V^2/Hz at 0 dB) = C * S_ref / |H|^2, with a regularized |H|."""
        b = self.band
        h2 = np.abs(self.frf.h1[b]) ** 2
        ok = (self.frf.coherence[b] >= self.c.coherence_min) & (h2 > 0)
        if ok.sum() < 2:
            raise AbortError("FRF estimate invalid: coherence too low on nearly all lines")
        log_h2 = np.log(np.where(h2 > 0, h2, 1.0))
        if not ok.all():
            fb = self.freqs[b]
            log_h2[~ok] = np.interp(fb[~ok], fb[ok], log_h2[ok])
        h2 = np.exp(log_h2)
        h2 = np.maximum(h2, h2.max() * 10 ** (-self.c.h_dynamic_range_db / 10))
        psd = np.zeros(len(self.freqs))
        psd[b] = self.s_ref[b] * 10 ** (self.corr_db[b] / 10) / h2
        return psd

    def _correct(self) -> None:
        b = self.band
        meas = self.resp_ctrl.value[b]
        ref = self.s_ref[b]
        with np.errstate(divide="ignore"):
            err = np.where(meas > 0, 10 * np.log10(ref / np.where(meas > 0, meas, 1.0)), 0.0)
        # A chi-square PSD estimate with v DOF is biased low in dB by about 10/ln(10)/v;
        # without this term the loop settles slightly above the reference.
        err -= 10 / math.log(10) / self.c.control_dof
        step = np.clip(self.c.correction_gain * err, -self.c.max_step_db, self.c.max_step_db)
        step = uniform_filter1d(step, 3, mode="nearest")
        self.corr_db[b] = np.clip(self.corr_db[b] + step,
                                  -self.c.max_correction_db, self.c.max_correction_db)

    def _evaluate(self) -> Evaluation | None:
        if self.resp_disp.value is None:
            return None
        lvl = 10 ** (self.level_db / 10)
        b = self.band
        ref = self.s_ref[b] * lvl
        meas = self.resp_disp.value[b] * lvl
        ref_rms = math.sqrt(np.sum(ref) * self.df)
        meas_rms = math.sqrt(np.sum(meas) * self.df)
        err = 20 * math.log10(meas_rms / ref_rms) if meas_rms > 0 else -math.inf
        with np.errstate(divide="ignore"):
            ratio = 10 * np.log10(np.where(meas > 0, meas, 1e-300) / ref)
        tol = self.profile.tolerance
        ev = Evaluation(ref_rms, meas_rms, err, int(np.sum(np.abs(ratio) > tol.alarm_db)),
                        int(np.sum(np.abs(ratio) > tol.abort_db)), int(b.sum()))
        self.last_eval = ev
        return ev

    def _report(self, sd: StepResult, ev: Evaluation | None) -> None:
        row = {"t_s": round(self.t_s, 4), "state": self.state.value,
               "level_db": round(self.level_db, 2),
               "drive_rms_v": sd.drive_rms_v, "drive_peak_v": sd.drive_peak_v,
               "clip_fraction": sd.clip_fraction, "ai_peak_v": sd.ai_peak_v,
               "block_rms_g": sd.block_rms_g, "run_time_s": self.run_time_s,
               "duration_s": self.duration_s}
        if ev is not None and self.state in (State.RAMP, State.RUN):
            row.update(ref_rms_g=ev.ref_rms_g, meas_rms_g=ev.meas_rms_g,
                       rms_err_db=ev.rms_err_db, lines_alarm=ev.lines_alarm,
                       lines_abort=ev.lines_abort)
        if self.logger:
            self.logger.status(row)
        if self.on_status:
            self.on_status(row)

    def _snapshot(self, name: str) -> None:
        if not self.logger:
            return
        lvl = 10 ** (self.level_db / 10)
        tol = self.profile.tolerance
        ref = self.s_ref * lvl
        meas = self.resp_disp.value * lvl if self.resp_disp.value is not None \
            else np.zeros(len(self.freqs))
        h = self.frf.h1 if self.frf.gxx is not None else np.zeros(len(self.freqs))
        coh = self.frf.coherence if self.frf.gxx is not None else np.zeros(len(self.freqs))
        self.logger.spectra(name, self.freqs, {
            "ref_g2_hz": ref, "meas_g2_hz": meas,
            "alarm_lo": ref * 10 ** (-tol.alarm_db / 10), "alarm_hi": ref * 10 ** (tol.alarm_db / 10),
            "abort_lo": ref * 10 ** (-tol.abort_db / 10), "abort_hi": ref * 10 ** (tol.abort_db / 10),
            "drive_v2_hz": self._drive_psd * lvl, "h_mag_g_per_v": np.abs(h),
            "h_phase_deg": np.degrees(np.angle(h)), "coherence": coh,
            "correction_db": self.corr_db})

    def _check_stop(self) -> None:
        if self.stop_requested:
            raise OperatorStop("stopped by operator")

    # ------------------------------------------------------------------ phases
    def _noise_phase(self) -> None:
        self.state = State.NOISE
        zeros = np.zeros(len(self.freqs))
        self.seg = 0
        while self.noise.count < self.settings.pretest.noise_blocks:
            self._check_stop()
            sd = self._step(zeros)
            self.monitor.check_io(sd.ai_peak_v, sd.drive_rms_v, sd.clip_fraction)

    def _pretest_phase(self) -> None:
        self.state = State.PRETEST
        pt = self.settings.pretest
        seg = self._new_segment("pretest", 1.0)
        psd = flat_psd(self.freqs, self.profile.f_lo, self.profile.f_hi, pt.drive_rms_v)
        self._drive_psd = psd
        while self.seg_frames[seg] < pt.frames:
            self._check_stop()
            sd = self._step(psd)
            self.monitor.check_io(sd.ai_peak_v, sd.drive_rms_v, sd.clip_fraction)
            self.monitor.check_response(sd.block_rms_g)
            self._report(sd, None)

        b = self.band
        resp_rms = self._band_rms(self.frf.gyy)
        noise_rms = self._band_rms(self.noise.value)
        snr_db = 20 * math.log10(resp_rms / noise_rms) if noise_rms > 0 else math.inf
        coh_mean = float(np.mean(self.frf.coherence[b]))
        self.pretest_info = {"response_rms_g": resp_rms, "noise_rms_g": noise_rms,
                             "snr_db": snr_db, "coherence_mean": coh_mean}
        if self.logger:
            self.logger.spectra("pretest_frf", self.freqs, {
                "h_mag_g_per_v": np.abs(self.frf.h1), "h_phase_deg": np.degrees(np.angle(self.frf.h1)),
                "coherence": self.frf.coherence, "drive_v2_hz": self.frf.gxx,
                "response_g2_hz": self.frf.gyy, "noise_g2_hz": self.noise.value})
        if snr_db < pt.min_snr_db:
            raise AbortError(f"pretest: response {resp_rms:.3g} g rms is only {snr_db:.1f} dB above "
                             f"the noise floor (check accelerometer, charge amplifier, amplifier)")
        if coh_mean < self.c.coherence_min:
            raise AbortError(f"pretest: mean coherence {coh_mean:.2f} < {self.c.coherence_min}")

        full = self._run_drive_psd() * 10 ** (self.target_level_db / 10)
        drive_rms = float(np.sqrt(np.sum(full) * self.df))
        self.pretest_info["predicted_drive_rms_v"] = drive_rms
        limit = min(self.shaker.max_drive_rms_v, self.shaker.max_drive_v / 3)
        if drive_rms > limit:
            raise AbortError(f"pretest: profile needs about {drive_rms:.2f} V rms drive at the target "
                             f"level; limit is {limit:.2f} V rms (max_drive_rms_v, max_drive_v/3)")

    def _closed_loop(self) -> None:
        c, tol = self.c, self.profile.tolerance
        target = self.target_level_db
        self._set_level(min(target + c.start_level_db, target))
        self.state = State.RAMP
        next_snapshot = c.snapshot_interval_s
        while True:
            self._check_stop()
            self._drive_psd = self._run_drive_psd()
            count_before = self.resp_ctrl.count
            sd = self._step(self._drive_psd)
            ev = self._evaluate()
            frames = self.seg_frames[self.seg]

            self.monitor.check_io(sd.ai_peak_v, sd.drive_rms_v, sd.clip_fraction)
            self.monitor.check_response(sd.block_rms_g)
            if ev is not None and frames >= 2:
                self.monitor.check_open_loop(sd.block_rms_g, ev.ref_rms_g)
            settled = ev is not None and (self.state == State.RUN or frames >= c.min_frames_per_level)
            if settled and abs(ev.rms_err_db) > tol.rms_abort_db:
                raise AbortError(f"rms error {ev.rms_err_db:+.2f} dB exceeds +/-{tol.rms_abort_db} dB")

            if self.resp_ctrl.count > count_before:
                self._correct()

            if self.state == State.RAMP:
                if frames >= c.min_frames_per_level and ev is not None \
                        and abs(ev.rms_err_db) <= c.level_tolerance_db:
                    if self.level_db >= target - 1e-9:
                        self.state = State.RUN
                    else:
                        self._set_level(min(self.level_db + c.level_step_db, target))
                elif frames > c.max_frames_per_level:
                    raise AbortError(f"could not reach {self.level_db:+.1f} dB within "
                                     f"{c.max_frames_per_level} frames")
            elif self.state == State.RUN:
                pct = 100 * ev.lines_abort / ev.n_lines
                self._abort_lines_blocks = self._abort_lines_blocks + 1 \
                    if pct > tol.max_abort_lines_pct else 0
                if self._abort_lines_blocks >= 3:
                    raise AbortError(f"{pct:.1f}% of lines outside +/-{tol.abort_db} dB "
                                     f"(limit {tol.max_abort_lines_pct}%)")
                self.run_time_s += self.half / self.fs
                if self.run_time_s >= next_snapshot:
                    self._snapshot(f"psd_{int(round(self.run_time_s)):06d}s")
                    next_snapshot += c.snapshot_interval_s
            self._report(sd, ev)
            if self.state == State.RUN and self.run_time_s >= self.duration_s:
                return

    def _rampdown(self, seconds: float, psd_norm: np.ndarray) -> None:
        """Fade the drive to zero, then flush the AO queue with zeros."""
        self.state = State.RAMPDOWN
        self._new_segment("rampdown", self.seg_level[self.seg])
        n_blocks = max(1, math.ceil(seconds * self.fs / self.half))
        env = np.linspace(1.0, 0.0, n_blocks * self.half)
        for i in range(n_blocks):
            sd = self._step(psd_norm, env[i * self.half:(i + 1) * self.half])
            self.monitor.check_io(sd.ai_peak_v, 0.0, 0.0)
        self.gen.reset()
        self._new_segment("silence", 0.0)
        zeros = np.zeros(len(self.freqs))
        for _ in range(self.P + 2):
            self._step(zeros)

    # ------------------------------------------------------------------ entry
    def run(self, pretest_only: bool = False) -> RunResult:
        report = preflight(self.profile, self.shaker, self.settings, self.target_level_db)
        if not report.ok:
            return self._result(False, State.ABORTED, "pre-flight check failed:\n" + report.format())

        reason, completed = "completed", False
        self.backend.start(np.zeros(self.P * self.block_io))
        self.drive_hist.append(np.zeros(self.P * self.half))
        self.seg_hist.append(np.zeros(self.P * self.half, dtype=np.int64))
        try:
            try:
                self._noise_phase()
                self._pretest_phase()
                if pretest_only:
                    reason = "pretest completed"
                else:
                    self._closed_loop()
                self._snapshot("psd_final")
                self._rampdown(self.c.rampdown_s, self._drive_psd)
                completed = True
                self.state = State.DONE
            except OperatorStop as exc:
                reason = str(exc)
                self._snapshot("psd_final")
                self._rampdown(self.c.rampdown_s, self._drive_psd)
                self.state = State.ABORTED
            except AbortError as exc:
                reason = f"ABORT: {exc}"
                self._snapshot("psd_final")
                self._rampdown(self.s.abort_rampdown_s, self._drive_psd)
                self.state = State.ABORTED
        except DaqFault as exc:
            reason = f"DAQ fault: {exc}" if reason == "completed" else \
                f"{reason}; DAQ fault during ramp-down: {exc}"
            completed = False
            self.state = State.ABORTED
        except AbortError as exc:   # raised again during ramp-down
            reason = f"{reason}; {exc} during ramp-down"
            completed = False
            self.state = State.ABORTED
        finally:
            self.backend.stop()
        return self._result(completed, self.state, reason)

    def _result(self, completed: bool, state: State, reason: str) -> RunResult:
        ev = self.last_eval
        return RunResult(completed=completed, state=state, reason=reason,
                         run_time_s=self.run_time_s,
                         ref_rms_g=ev.ref_rms_g if ev else None,
                         meas_rms_g=ev.meas_rms_g if ev else None,
                         rms_err_db=ev.rms_err_db if ev else None,
                         log_dir=str(self.logger.dir) if self.logger else None)
