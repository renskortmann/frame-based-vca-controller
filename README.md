# vcactl — frame-based closed-loop random vibration controller

`vcactl` runs closed-loop **random vibration** tests on a TIRA **TV 51110** or **TV 52110**
(each with a BDA 120 power amplifier), a **BK 4809** with a BK 2718 amplifier, or a **BK 4801**
body with **4812** general purpose head and a BK 2707 amplifier. The drive goes out through an **NI USB-6211**, **NI USB-4431** or **NI PXIe-4468**,
and the loop is closed with an accelerometer on a charge amplifier (or an IEPE accelerometer
on the USB-4431/PXIe-4468). The
control band reaches **7 kHz**. It runs on **Windows 11** and **native Linux**, and includes a
simulated shaker for development without hardware.

> WSL2 cannot run NI-DAQmx because the driver needs native kernel/USB access. Under WSL2, use
> `--sim` only.

## Hardware setup

| USB-6211 terminal | Connect to |
|---|---|
| AO 0 (pin 12) / AO GND (pin 14) | BDA 120 signal input (BNC) |
| AI 1 (pin 17) / AI 9 (pin 18) | charge amplifier output: signal / reference (differential) |
| AI GND (pin 28) | if the charge amplifier output is **floating** (battery powered): 10–100 kΩ from AI 9 to AI GND |

The table is for the USB-6211. On the USB-4431 and PXIe-4468 (BNC), connect AO 0 to the amplifier
input and AI 0 to the charge amplifier output, or connect an IEPE accelerometer directly to AI 0
and set `iepe_current_ma` in the DAQ profile (`config/daq/*.toml`).

Power sequence, which matters because the USB-6211 AO glitches by about ±1 V for 200 ms at power-on
(the USB-4431 AO also glitches at power-on):

1. Connect the DAQ and let the PC enumerate it. Leave the **amplifier off**.
2. Switch on the amplifier with its gain knob in the usual position. Keep the knob in the same
   position for the whole test, because the pretest measures the system with that gain.
3. When finished, switch the amplifier **off before** unplugging USB or shutting down the PC.

Set `sensor.sensitivity_mv_per_g` in `config/settings.toml` to the sensitivity of the measurement
chain at the DAQ input, in mV/g. With a charge amplifier, this is its output setting in mV/g (not
its mV/pC gain). With IEPE, it is the accelerometer's calibrated sensitivity. With a charge
amplifier, choose the output setting so the expected peak response (about 4–5 × rms) stays inside
`daq.ai_range_v`. With IEPE, choose the AI range to suit the accelerometer.
For example, 10 mV/g at 30 g rms gives peaks around 1.5 V on the 10 V range.

## Installation

1. Install the **NI-DAQmx driver** (not only the Python package):
   - Windows 11: NI-DAQmx from ni.com, or `python -m nidaqmx installdriver` after step 2.
   - Linux: NI-DAQmx for Linux on a supported distribution (Ubuntu LTS, RHEL, openSUSE). Follow
     NI's repository instructions and reboot so the kernel modules load.
2. Install the Python package (Python ≥ 3.9):
   ```
   python -m venv .venv
   .venv/bin/pip install -e ".[plot,test]"        # Windows: .venv\Scripts\pip ...
   ```
3. Check that the device is visible. If it has a different name, set `daq.device` to that name:
   ```
   vcactl list-devices
   ```

## Usage

```
vcactl check   --shaker tv51110 --profile config/test_profiles/example_flat.toml [--level -6]
vcactl pretest --shaker tv51110 --profile config/test_profiles/example_flat.toml
vcactl run     --shaker tv51110 --profile config/test_profiles/example_flat.toml [--level 0] [--duration 60]
```

- `--shaker` takes `tv51110`, `tv52110`, `bk4809`, `bk4801_4812`, or a path to a shaker TOML file.
- `--daq` takes `usb6211` (default), `usb4431`, `pxie4468`, or a path to a DAQ TOML file.
- `--sim` uses the simulated shaker instead of the DAQ. `--sim-realtime` paces it in real time.
- `--settings` points to a different settings file (default `config/settings.toml`; `--station` still works). `--log-dir` sets the log root (default `runs/`).
- Ctrl-C (or SIGTERM) ramps the drive down smoothly. A second Ctrl-C stops immediately and
  forces AO to 0 V.

Recommended first test on hardware:

1. Run `vcactl pretest ...` and check the SNR, the mean coherence and the predicted drive voltage.
2. Run `vcactl run ... --level -12 --duration 10`.
3. Run at full level.

Plot a run afterwards:

```
python tools/plot_run.py runs/<run-dir>                  # add --save out.png for a file
```

`docs/shaker_comparison.pdf` compares the configured shakers (ratings, amplifier and drive limits,
payload limits). Rebuild it after changing a shaker file:

```
pip install -e ".[report]"
python tools/shaker_comparison.py                        # writes docs/shaker_comparison.pdf
```

`tools/client_options_deck.py` builds a draft client presentation (.pptx) on shaker and
controller options from the same shaker data. Its prices, hours and labour rate are assumptions
in the script; check them before using the deck.

Each run directory contains:

- `meta.json`: all configs.
- `status.csv`: one row per half-frame.
- `pretest_frf.csv`: FRF, coherence and noise floor.
- `psd_<t>s.csv` snapshots and `psd_final.csv`: reference, control PSD, tolerance bands,
  drive PSD, FRF and correction.

## Test profiles (`config/test_profiles/*.toml`)

```toml
name = "Sloped 20-2000 Hz"
duration_s = 60.0
breakpoints = [
    { f_hz = 20.0,   psd_g2_hz = 0.002 },
    { f_hz = 80.0,   slope_db_oct = 6.0 },   # slope from the previous breakpoint
    { f_hz = 1000.0, slope_db_oct = 0.0 },
    { f_hz = 2000.0, slope_db_oct = -6.0 },
]
[tolerance]            # all optional
alarm_db = 3.0         # per-line alarm band
abort_db = 6.0         # per-line abort band
rms_alarm_db = 1.5
rms_abort_db = 3.0
max_abort_lines_pct = 5.0
```

The PSD is interpolated log-log between breakpoints and is zero outside the first and last
breakpoint.

## How it works

- **Rates:** AI and AO run at 100 kS/s on one shared sample clock (AO is clocked from
  `ai/SampleClock`). The control rate is 25 kS/s after 4× FIR decimation and interpolation
  with ≥ 80 dB stop band. That filtering is needed because the USB-6211 has no anti-aliasing
  filter. With frames of N = 4096, the line spacing is Δf = 6.1 Hz and one loop step is 82 ms.
  The USB-4431 and PXIe-4468 have delta-sigma converters with built-in anti-aliasing. They run
  at 51.2 kS/s with 2× decimation (25.6 kS/s control rate, Δf = 6.25 Hz). Their AO has its own
  sample clock from the same timebase and starts on `ai/StartTrigger`. The converter filters
  delay the response by about 100 samples (`limits.filter_delay_samples` in the DAQ profile),
  and the controller shifts the drive by that amount before estimating the FRF.
- **Drive:** each frame gets new random phases. Frames are sqrt-Hann windowed and overlap-added
  at 50 %, giving a continuous, Gaussian, non-periodic signal. AO regeneration is off, so the
  output always comes from the controller.
- **Sequence:**
  1. noise floor with zero drive;
  2. pretest with flat low-level noise: H1 FRF and coherence, SNR and coherence checks,
     predicted drive at the target level;
  3. closed loop: drive PSD = C·S_ref/|H|², where the per-line correction C is updated every
     frame from the averaged control PSD. H continues to update during the run;
  4. level ramp from −12 dB in 3 dB steps, each held until the rms error is ≤ 1 dB, then the
     test duration at full level;
  5. ramp-down to 0 V.
- **Pre-flight (before any output):** payload (`safety.payload_kg`) against the shaker's static
  payload limit, rms acceleration, force (moving mass + payload), 3σ velocity and 3σ peak-peak
  displacement against the shaker's data-sheet limits, plus the frequency range. The force check
  is the dynamic payload limit; each shaker file lists it for a few acceleration levels.
- **Runtime aborts:**
  - AI overload;
  - drive rms or clipping over the limit;
  - response over the shaker's random rating;
  - open loop (sensor or amplifier lost);
  - rms error or too many lines outside the abort band;
  - any DAQmx error or warning (e.g. underflow, potential glitch).

  Every abort ends with ramp-down, 0 V and the tasks cleared. The drive already queued
  (`ao_queue_blocks` + 1 half-frames, about 250 ms) still plays out before the ramp-down.
  A DAQ fault stops immediately and writes 0 V.

## DAQ profiles (`config/daq/*.toml`)

A DAQ profile holds the device settings (device name, channels, ranges, coupling, IEPE, rates)
and, under `[limits]`, the device's data-sheet capabilities. All settings are checked against
these limits when the configuration is loaded. The settings file can override DAQ settings
in its own `[daq]` table (for example `device = "Dev2"`), but not the limits.

| `--daq` | AI ranges (V) | AO range (V) | Rates | Notes |
|---|---|---|---|---|
| `usb6211` | 0.2, 1, 5, 10 | 10 | 20 MHz / n, ≤ 250 kS/s | no anti-aliasing filter: `decimation` ≥ 2 |
| `usb4431` | 10 | 3.5 | 51.2 k, 80 k, 96 k ÷ 2ⁿ | IEPE 2.1 mA; shaker `max_drive_v` ≤ 3.5 |
| `pxie4468` | 0.316 … 42.4 | 0.316, 1, 3.16, 10 | 100 S/s – 200 kS/s | IEPE 4/10/20 mA; `filter_delay_samples` depends on the rate |

The shaker's `max_drive_v` must not exceed `daq.ao_range_v`. IEPE excitation requires AC coupling.

## Tuning notes (`config/settings.toml`)

- Underflow errors (-200621/-200290) on a slow or busy PC: increase `daq.ao_queue_blocks`.
  This adds latency.
- `daq.fs_io_hz` must be a rate the device supports (see the DAQ profiles above). The control
  band is limited to 0.6 × control Nyquist (7.5 kHz with the USB-6211 defaults, 7.68 kHz with
  the USB-4431/PXIe-4468 defaults).
- `control.correction_gain` above about 0.2 can overshoot. Raise `control.control_dof` for
  smoother but slower equalization.
- The drive limits `max_drive_v` (peak clip) and `max_drive_rms_v` are in the shaker file,
  because they depend on the amplifier. For the TIRA shakers they are 3.5 V peak and 1.5 V rms;
  the BDA 120 reaches full power at 3.5 V peak (2.47 V rms) sine input. Raise the rms limit only
  if a profile needs more drive. `max_drive_rms_v` may not exceed `amp_input_full_v`.

## Development

```
.venv/bin/pytest            # all tests use the simulated shaker
vcactl run --sim --shaker tv52110 --profile config/test_profiles/example_wideband.toml --duration 30
```

Layout:

- `src/vcactl/controller.py`: control loop and state machine.
- `drive.py`: drive synthesis.
- `dsp/`: resampling and spectra.
- `safety.py`: pre-flight and runtime monitors.
- `daq/nidaq.py`, `daq/sim.py`: backends.
- `profile.py`, `config.py`, `logger.py`, `cli.py`.
