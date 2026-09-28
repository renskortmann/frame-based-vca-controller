import numpy as np
import pytest
from scipy import signal, stats

from vcactl.drive import RandomDriveGenerator, flat_psd
from vcactl.dsp.resample import (Decimator, Interpolator, design_lowpass,
                                 total_delay_control_samples)
from vcactl.dsp.spectral import AutoSpectrumAverager, CrossSpectrumAverager, Spectrum

FS_IO, R = 100000.0, 4


def test_lowpass_passband_and_stopband():
    taps = design_lowpass(FS_IO, R)
    assert (len(taps) - 1) % (2 * R) == 0
    w, h = signal.freqz(taps, worN=2**16, fs=FS_IO)
    mag = 20 * np.log10(np.abs(h) + 1e-300)
    assert np.max(np.abs(mag[w <= 7500])) < 0.01
    assert np.max(mag[w >= FS_IO / R / 2]) < -79


def test_interp_decim_roundtrip_is_pure_delay():
    taps = design_lowpass(FS_IO, R)
    d = total_delay_control_samples(taps, R)
    fs = FS_IO / R
    t = np.arange(8192) / fs
    x = np.sin(2 * np.pi * 3000 * t) + 0.5 * np.sin(2 * np.pi * 6900 * t)
    interp, decim = Interpolator(taps, R), Decimator(taps, R)
    # process in blocks to exercise the filter state
    y = np.concatenate([decim.process(interp.process(b)) for b in np.split(x, 8)])
    np.testing.assert_allclose(y[d + 500:], x[500:len(x) - d], atol=2e-3)


def test_spectrum_scaling_white_noise():
    fs, n = 25000.0, 4096
    sp = Spectrum(n, fs)
    rng = np.random.default_rng(0)
    avg = AutoSpectrumAverager(1e9)
    sigma = 0.3
    for _ in range(400):
        avg.update(np.abs(sp.fft(rng.normal(0, sigma, n))) ** 2 * sp.scale)
    expected = sigma**2 / (fs / 2)
    assert np.median(avg.value[10:-10]) == pytest.approx(expected, rel=0.02)


def test_drive_generator_matches_target_psd():
    fs, n = 25000.0, 4096
    sp = Spectrum(n, fs)
    gen = RandomDriveGenerator(n, fs, np.random.default_rng(1))
    target = flat_psd(sp.freqs, 50, 5000, rms=0.5)
    x = np.concatenate([gen.next_block(target) for _ in range(800)])[n:]
    assert np.std(x) == pytest.approx(0.5, rel=0.01)
    # Welch estimate, 50 % overlap
    f, pxx = signal.welch(x, fs=fs, window="hann", nperseg=n)
    band = (f >= 100) & (f <= 4900)
    err_db = 10 * np.log10(np.mean(pxx[band]) / np.mean(target[band]))
    assert abs(err_db) < 0.1
    per_line_db = 10 * np.log10(pxx[band] / target[band])
    assert np.max(np.abs(per_line_db)) < 1.5
    # Gaussian amplitude distribution
    assert abs(stats.kurtosis(x)) < 0.1
    assert abs(stats.skew(x)) < 0.05


def test_drive_generator_is_not_periodic():
    fs, n = 25000.0, 1024
    gen = RandomDriveGenerator(n, fs, np.random.default_rng(2))
    psd = flat_psd(np.fft.rfftfreq(n, 1 / fs), 100, 5000, 1.0)
    a = np.concatenate([gen.next_block(psd) for _ in range(2)])
    b = np.concatenate([gen.next_block(psd) for _ in range(2)])
    assert abs(np.corrcoef(a, b)[0, 1]) < 0.2


def test_h1_recovers_filter():
    fs, n = 25000.0, 2048
    sp = Spectrum(n, fs)
    rng = np.random.default_rng(3)
    b, a = signal.butter(2, 3000, fs=fs)
    x = rng.normal(0, 1, n * 200)
    y = signal.lfilter(b, a, x) + rng.normal(0, 0.01, len(x))
    avg = CrossSpectrumAverager(1e9)
    for i in range(0, len(x) - n, n // 2):
        avg.update(sp.fft(x[i:i + n]), sp.fft(y[i:i + n]), sp.scale)
    _, h = signal.freqz(b, a, worN=sp.freqs, fs=fs)
    sel = (sp.freqs > 100) & (sp.freqs < 6000)
    np.testing.assert_allclose(np.abs(avg.h1[sel]), np.abs(h[sel]), rtol=0.03)
    assert np.min(avg.coherence[sel]) > 0.95
