"""Impact-hammer tap test (``vcactl tap``): input only, triggered on the hammer channel.

The channel set has one channel with role ``hammer`` (force, N) and one or more response
channels (accelerometers, g). For every hit:

1. a record of ``record_s`` starts ``pretrigger_s`` before the hammer force first reaches
   ``trigger_n``;
2. the hit is rejected on an AI overload or sensor over range in any channel, or a double hit (a
   second force peak above ``double_hit_fraction`` of the first after the first pulse has ended);
3. otherwise it is accepted (``--confirm``: after a y/n answer; the stream keeps running);
4. the hammer signal gets a force window (offset removed, zero after ``force_window_s``, short
   cosine taper) and the responses an exponential window that falls to ``exp_window_end`` at the
   end of the record (time constant ``tau_w``).

The accepted hits are averaged into H1 = Sum(Y X*) / Sum(|X|^2) and the coherence per response
(``tap_frf.csv``). The exponential window adds damping: a mode at f_r measured with damping ratio
zeta_m has zeta = zeta_m - 1 / (2 pi f_r tau_w) (``tau_w`` is in meta.json).

Files: ``hits.f32`` (the unwindowed records of every hit, float32, record x channels, hit after
hit), ``hits.csv`` and ``tap_frf.csv``.
"""

from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass

import numpy as np

from .config import ConfigError, Settings, build_dataclass, read_toml
from .record import InputStream, block_samples


@dataclass(frozen=True)
class TapProfile:
    type: str = "tap"
    name: str = ""
    record_s: float = 4.0             # record length per hit
    pretrigger_s: float = 0.02        # part of the record before the trigger
    trigger_n: float = 10.0           # hammer force that starts a record (N)
    hits: int = 5                     # accepted hits to average
    max_hits: int = 50                # stop after this many hits in total (accepted or not)
    double_hit_fraction: float = 0.1  # second peak above this fraction of the first: rejected
    force_window_s: float = 0.02      # force window after the trigger
    exp_window_end: float = 0.01      # exponential window at the end of the record; 1: none
    f_max_hz: float = 4000.0          # tap_frf.csv up to this frequency


def load_tap_profile(path) -> TapProfile:
    data = read_toml(path)
    p = build_dataclass(TapProfile, data, str(path))
    where = str(path)
    if p.type != "tap":
        raise ConfigError(f"{where}: type must be \"tap\" for vcactl tap")
    for key in ("record_s", "trigger_n", "force_window_s", "f_max_hz"):
        if getattr(p, key) <= 0:
            raise ConfigError(f"{where}: {key} must be > 0")
    if not 0 <= p.pretrigger_s < p.record_s:
        raise ConfigError(f"{where}: pretrigger_s must be >= 0 and < record_s")
    if p.force_window_s >= p.record_s - p.pretrigger_s:
        raise ConfigError(f"{where}: force_window_s must be shorter than the record after the "
                          "trigger")
    if p.hits < 1 or p.max_hits < p.hits:
        raise ConfigError(f"{where}: hits must be >= 1 and max_hits >= hits")
    if not 0 < p.double_hit_fraction < 1:
        raise ConfigError(f"{where}: double_hit_fraction must be in (0, 1)")
    if not 0 < p.exp_window_end <= 1:
        raise ConfigError(f"{where}: exp_window_end must be in (0, 1] (1: no window)")
    return p


@dataclass
class Hit:
    index: int
    t_s: float                # stream time of the trigger
    peak_n: float
    double_hit: bool
    overload: list
    accepted: bool = False

    @property
    def ok(self) -> bool:
        return not self.double_hit and not self.overload


class TapTest:
    """Triggered records from an ``InputStream``; ``confirm(hit) -> bool`` decides on hits that
    pass the checks (default: accept). ``confirm_async``: ask in a thread while the stream keeps
    being read (interactive use)."""

    def __init__(self, settings: Settings, backend, profile: TapProfile, logger=None,
                 confirm=None, confirm_async: bool = False, on_hit=None):
        hammers = [i for i, c in enumerate(settings.channels) if c.role == "hammer"]
        if len(hammers) != 1:
            raise ConfigError(f"the channels file '{settings.channels_name}' needs exactly one "
                              "channel with role hammer")
        self.h = hammers[0]
        self.resp = [i for i, c in enumerate(settings.channels)
                     if i != self.h and c.role != "loopback"]
        if not self.resp:
            raise ConfigError("a tap test needs at least one response channel")
        self.p, self.logger = profile, logger
        self.stream = InputStream(settings, backend, block_samples(settings))
        fs = self.fs = self.stream.fs
        if profile.f_max_hz > settings.daq.limits.passband_fraction * fs:
            raise ConfigError(f"f_max_hz {profile.f_max_hz:g} Hz is above the DAQ band at "
                              f"{fs:g} S/s")
        self.n_rec = round(profile.record_s * fs)
        self.n_pre = round(profile.pretrigger_s * fs)
        after = (self.n_rec - self.n_pre) / fs
        self.tau_w = math.inf if profile.exp_window_end >= 1 else \
            after / math.log(1 / profile.exp_window_end)
        self.confirm = confirm or (lambda hit: True)
        self.confirm_async = confirm_async
        self.on_hit = on_hit
        self.hits: list[Hit] = []
        self._sxx = None
        self._sxy = None
        self._syy = None
        self.freqs = np.fft.rfftfreq(self.n_rec, 1 / fs)
        self.windows = self._windows()

    # ------------------------------------------------------------------ windows and spectra
    def _windows(self) -> tuple[np.ndarray, np.ndarray]:
        n, pre = self.n_rec, self.n_pre
        t = (np.arange(n) - pre) / self.fs
        exp = np.ones(n) if math.isinf(self.tau_w) else np.where(t < 0, 1.0, np.exp(-t / self.tau_w))
        force = (t < self.p.force_window_s).astype(float)
        taper = max(1, round(0.1 * self.p.force_window_s * self.fs))
        end = pre + round(self.p.force_window_s * self.fs)
        force[end - taper:end] = 0.5 * (1 + np.cos(np.linspace(0, np.pi, taper)))
        return force, exp

    @property
    def accepted(self) -> int:
        return sum(h.accepted for h in self.hits)

    def _accumulate(self, rec: np.ndarray) -> None:
        wf, we = self.windows
        x = rec[:, self.h] - np.mean(rec[:self.n_pre, self.h]) if self.n_pre else rec[:, self.h]
        X = np.fft.rfft(x * wf)
        Y = np.fft.rfft(rec[:, self.resp] * we[:, None], axis=0)
        if self._sxx is None:
            self._sxx = np.zeros(len(X))
            self._sxy = np.zeros(Y.shape, complex)
            self._syy = np.zeros(Y.shape)
        self._sxx += np.abs(X) ** 2
        self._sxy += Y * np.conj(X)[:, None]
        self._syy += np.abs(Y) ** 2

    def frf(self) -> tuple[np.ndarray, np.ndarray]:
        """H1 (response unit per N, columns per response channel) and coherence."""
        sxx = np.maximum(self._sxx, 1e-300)[:, None]
        h1 = self._sxy / sxx
        coh = np.abs(self._sxy) ** 2 / (sxx * np.maximum(self._syy, 1e-300))
        return h1, coh

    # ------------------------------------------------------------------ hit checks
    def _check(self, rec: np.ndarray, peaks_v: np.ndarray, t_s: float) -> Hit:
        f = rec[:, self.h] - (np.mean(rec[:self.n_pre, self.h]) if self.n_pre else 0.0)
        k = int(np.argmax(f))
        peak = float(f[k])
        below = np.nonzero(f[k:] < 0.05 * peak)[0]
        end = k + (int(below[0]) if len(below) else len(f) - k)
        double = bool(np.any(f[end:] > self.p.double_hit_fraction * peak))
        return Hit(len(self.hits) + 1, t_s, peak, double, self.stream.overloaded(peaks_v))

    # ------------------------------------------------------------------ run
    def run(self, stop_requested=lambda: False) -> str:
        s = self.stream
        names = s.names
        buf = np.zeros((0, len(names)))
        capture = None                             # (record rows so far, peaks so far, t_s)
        pending: queue.Queue | None = None
        waiting = None
        reason = "completed"
        s.start()
        try:
            while self.accepted < self.p.hits:
                if stop_requested():
                    reason = "stopped by operator"
                    break
                if len(self.hits) >= self.p.max_hits and waiting is None:
                    reason = f"stopped after {self.p.max_hits} hits"
                    break
                y = s.read(check=False)
                peaks = s.last_peaks_v
                if waiting is not None:            # interactive answer pending: discard input
                    if pending.empty():
                        continue
                    waiting.accepted = bool(pending.get())
                    self._decided(waiting, rec_waiting)
                    waiting = None
                    continue
                if capture is None:
                    data = np.vstack([buf, y])
                    trig = np.nonzero(data[len(buf):, self.h] >= self.p.trigger_n)[0]
                    if len(trig):
                        k = len(buf) + int(trig[0])
                        start = max(0, k - self.n_pre)
                        rows = data[start:]
                        if k - start < self.n_pre:       # too early: pad the pretrigger
                            rows = np.vstack([np.zeros((self.n_pre - (k - start), len(names))),
                                              rows])
                        t_s = (s.samples - len(data) + k) / self.fs
                        capture = [rows, peaks.copy(), t_s]
                        buf = np.zeros((0, len(names)))
                    else:
                        buf = data[-self.n_pre:] if self.n_pre else np.zeros((0, len(names)))
                else:
                    capture[0] = np.vstack([capture[0], y])
                    capture[1] = np.maximum(capture[1], peaks)
                if capture is not None and len(capture[0]) >= self.n_rec:
                    rec = capture[0][:self.n_rec]
                    rest = capture[0][self.n_rec:]
                    hit = self._check(rec, capture[1], capture[2])
                    self.hits.append(hit)
                    capture = None
                    buf = rest[-self.n_pre:] if self.n_pre else np.zeros((0, len(names)))
                    if self.logger is not None:
                        self.logger.raw_block(rec, "hits.f32")
                    if not hit.ok:
                        self._decided(hit, rec)
                    elif self.confirm_async:
                        pending, waiting, rec_waiting = queue.Queue(), hit, rec
                        threading.Thread(target=lambda h=hit, q=pending: q.put(self.confirm(h)),
                                         daemon=True).start()
                    else:
                        hit.accepted = bool(self.confirm(hit))
                        self._decided(hit, rec)
        finally:
            s.stop()
        if self.accepted < self.p.hits and reason == "completed":
            reason = f"only {self.accepted} of {self.p.hits} hits accepted"
        self._save()
        return reason

    def _decided(self, hit: Hit, rec: np.ndarray) -> None:
        if hit.accepted:
            self._accumulate(rec)
        if self.logger is not None:
            self.logger.append_row("hits", {
                "hit": hit.index, "t_s": round(hit.t_s, 4), "peak_n": round(hit.peak_n, 4),
                "double_hit": hit.double_hit, "overload": " ".join(hit.overload),
                "accepted": hit.accepted})
        if self.on_hit:
            self.on_hit(hit)

    def _save(self) -> None:
        if self.logger is None or self._sxx is None:
            return
        keep = self.freqs <= self.p.f_max_hz
        h1, coh = self.frf()
        chans = self.stream.channels
        cols = {"force_n2": self._sxx[keep] / max(self.accepted, 1)}
        for j, i in enumerate(self.resp):
            ch = chans[i]
            unit = f"{ch.unit}_per_N"
            cols[f"{ch.name}_mag_{unit}"] = np.abs(h1[keep, j])
            cols[f"{ch.name}_phase_deg"] = np.degrees(np.angle(h1[keep, j]))
            cols[f"{ch.name}_coherence"] = coh[keep, j]
        self.logger.spectra("tap_frf", self.freqs[keep], cols)
