# vcactl — frame-based closed-loop vibration controller

`vcactl` runs closed-loop **random vibration**, **sine sweep** and **stepped sine** (with
optional **ring-down**) tests on a TIRA **TV 51110** or **TV 52110**
(each with a BDA 120 power amplifier), a **BK 4809** with a DAP-Audio Palladium P-2000 amplifier,
or a **BK 4801** body with **4812** general purpose head and a BK 2707 amplifier. The drive goes
out through an **NI USB-4431**, **NI PXIe-4468** or **NI USB-6211**, and the loop is always closed
with an **IEPE accelerometer**, whichever DAQ is used. The control band reaches **7 kHz**. It runs
on **Windows 11** and **native Linux**, and includes a simulated shaker for development without
hardware.

> WSL2 cannot run NI-DAQmx because the driver needs native kernel/USB access. Under WSL2, use
> `--sim` only.

The third-party datasheets and manuals (shakers, amplifiers, DAQs, sensors) are not in this
repository. They are in the project's Nextcloud folder (`CSAA04 HF Shakers (Alessandro)`):
`Shakers/` and `Controller + DAQ/`. The source comments in `config/shakers/*.toml`,
`config/daq/*.toml` and `config/sensors/*.toml` refer to them by file name.

## Hardware setup

The control sensor is always an IEPE accelerometer, mounted on the shaker table or fixture next
to the specimen. How it is powered depends on the DAQ:

- **USB-4431 / PXIe-4468:** connect the accelerometer directly to AI 0 (BNC) and switch on the
  DAQ's IEPE excitation with `iepe_current_ma` in the DAQ profile (`config/daq/*.toml`): 2.1 mA on
  the USB-4431, 4, 10 or 20 mA on the PXIe-4468. IEPE needs `ai_coupling = "AC"`. Check that the
  accelerometer works at the chosen current (2.1 mA is below the 4 mA many sensors are specified at).
  Connect AO 0 to the amplifier input.
- **USB-6211:** it has no IEPE excitation, so an external IEPE signal conditioner powers the
  accelerometer and its output goes to the DAQ (wiring below). Leave the conditioner's output
  AC-coupled, so that the IEPE bias voltage does not reach the DC-coupled USB-6211 input.

| USB-6211 terminal | Connect to |
|---|---|
| AO 0 (pin 12) / AO GND (pin 14) | amplifier signal input (BNC; TRS/XLR on the P-2000) |
| AI 1 (pin 17) / AI 9 (pin 18) | IEPE conditioner output: signal / reference (differential) |
| AI GND (pin 28) | if the conditioner output is **floating** (battery powered): 10–100 kΩ from AI 9 to AI GND |

Power sequence, which matters because the USB-6211 AO glitches by about ±1 V for 200 ms at power-on
(the USB-4431 AO also glitches at power-on):

1. Connect the DAQ and let the PC enumerate it. Leave the **amplifier off**.
2. Switch on the amplifier with its gain knob in the usual position. Keep the knob in the same
   position for the whole test, because the pretest measures the system with that gain.
3. When finished, switch the amplifier **off before** unplugging USB or shutting down the PC.

### Sensor files (`config/sensors/*.toml`)

The accelerometer is selected with `--sensor` (default `pcb356b18`). Its file holds the
calibrated sensitivity of the axis used for control (`sensitivity_mv_per_g`), the measurement
range (`range_g_pk`), the frequency range (`f_min_hz`, `f_max_hz`), the mass (`mass_kg`, added
to `safety.payload_kg`) and the IEPE excitation range (`iepe_min_ma`, `iepe_max_ma`). The
controller uses them as follows:

- The sensitivity at the DAQ input is `sensitivity_mv_per_g` × `daq.conditioner_gain`. The
  conditioner gain (default 1) is for an external IEPE conditioner with gain (USB-6211).
- The pre-flight check fails if the profile's band is outside the sensor's frequency range, or
  if the expected acceleration peak exceeds `range_g_pk`. For random profiles the expected peak
  is `safety.accel_crest_factor` (4.5) × rms; for sine profiles it is the sine peak.
- A test aborts when the measured peak exceeds `range_g_pk`.
- With IEPE excitation from the DAQ, `daq.iepe_current_ma` must lie within the sensor's IEPE
  range.

Choose the accelerometer (and the conditioner gain) so that the expected peak response also
stays inside `daq.ai_range_v`, and inside the input range of any other instrument that shares
the signal.

**PCB 356B18** (`pcb356b18`, serial LW266119): a triaxial, high-sensitivity sensor for
low-frequency, low-level tests: 1000 mV/g nominal, ±5 g, 0.5–3000 Hz (±5 %), 25 g. Only one axis
closes the loop: z (1048 mV/g calibrated). The file also lists the x (980) and y (1054)
calibrations. Notes:

- It needs a 4-pin (1/4-28) to 3 × BNC breakout cable. Connect only the control axis.
- The output carries an 8–12 V bias. Use AC coupling: IEPE from the USB-4431 (2.1 mA) or the
  PXIe-4468 (4, 10 or 20 mA), or the AC-coupled output of an external conditioner on the
  USB-6211.
- Wait at least 12 s after switching on the IEPE excitation before starting a pretest; the
  output needs that long to settle.
- 1 V/g leaves little room: a 0.55 g rms random test (`example_low_g.toml`) already peaks near
  2.5 g. The default `pretest.drive_rms_v` (0.01 V) keeps the pretest within ±5 g on every
  shaker; at 0.05 V the BK shakers exceed it.

A less sensitive, wider-band accelerometer will get its own file later for the higher-level
example profiles (`example_flat`, `example_sloped`, `example_wideband`), which fail the
356B18's range check.

**BK 4809 with the Palladium P-2000.** The P-2000 is an audio amplifier: voltage mode, no current
limit, and 91× gain with the attenuator fully clockwise, which would reach the 4809's 5 A rms
rating at only 0.11 V rms input. Use stereo mode (never bridged) with the shaker on one channel,
and turn that channel's attenuator down to about 9.1× (1 kHz sine: 0.1 V rms in, 0.91 V rms out).
`config/shakers/bk4809.toml` assumes 9.1× (full rating at 1.1 V rms input); if the measured gain
differs, rescale the values as described at the top of that file. The software drive limits are
the only protection of the coil, so leave the attenuator untouched between pretest and run. The
amplifier is specified from 20 Hz (subsonic protection at 10 Hz), so profiles start at 20 Hz or
higher. Connect the DAQ AO with a mono (TS) jack as the manual's unbalanced input: tip to AO 0,
sleeve to AO GND. Measure the gain with the same cable.

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
vcactl check   --shaker bk4809 --profile config/test_profiles/example_low_g.toml [--level -6]
vcactl pretest --shaker bk4809 --profile config/test_profiles/example_low_g.toml
vcactl run     --shaker bk4809 --profile config/test_profiles/example_low_g.toml [--level 0] [--duration 60]
```

- `--shaker` takes `tv51110`, `tv52110`, `bk4809`, `bk4801_4812`, or a path to a shaker TOML file.
- `--daq` takes `usb6211` (default), `usb4431`, `pxie4468`, or a path to a DAQ TOML file.
- `--sensor` takes `pcb356b18` (default) or a path to a sensor TOML file.
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

`tools/shaker_comparison.py` builds `shaker_comparison.pdf`, which compares the configured shakers
(ratings, amplifier and drive limits, payload limits). The PDF is kept in the Nextcloud folder
(`Shakers/`), not in the repository. Rebuild it after changing a shaker file:

```
pip install -e ".[report]"
python tools/shaker_comparison.py --out "<Nextcloud folder>/Shakers/shaker_comparison.pdf"
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

Sine runs have their own `status.csv` columns (frequency, reference/measured peak amplitude,
error, drive amplitude, phase) and, for stepped sine:

- `steps.csv`: one row per step with the dwell mean of the measured amplitude, drive, H
  magnitude and phase, displacement and the (approximate) wall-clock start time.
- `events.csv` and `ringdown_<step>.csv`: drive cuts and the recorded decays (below).

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

### Sine profiles

`type = "sine_sweep"` or `type = "stepped_sine"` (random is the default). The reference
amplitude is a breakpoint table; each breakpoint gives one of `accel_g` (peak),
`velocity_m_s` (peak) or `displacement_mm_pp` (peak-peak). Acceleration is interpolated log-log
between breakpoints and held constant beyond them, so two displacement breakpoints give
exactly constant displacement. For "constant displacement, then constant acceleration", put a
breakpoint at the crossover frequency.

```toml
type = "sine_sweep"
f_start_hz = 20.0          # default: first breakpoint; f_start > f_end sweeps down
f_end_hz = 2000.0
rate_oct_min = 1.0         # logarithmic sweep
sweeps = 1                 # single sweeps, alternating direction
breakpoints = [{ f_hz = 20.0, accel_g = 0.5 }, { f_hz = 2000.0, accel_g = 0.5 }]
[tolerance]                # amplitude error; defaults for sine: 1 / 3 dB
alarm_db = 1.0
abort_db = 3.0
```

```toml
type = "stepped_sine"
frequencies_hz = [120.0]   # or f_start_hz / f_end_hz / points_per_octave
direction = "up"
levels_db = [-12.0, -6.0, 0.0]   # every level runs all frequencies
settle_s = 2.0             # minimum time before the dwell (and until within alarm_db)
max_settle_s = 30.0
dwell_s = 3.0
ringdown_s = 2.0           # 0 = off
breakpoints = [{ f_hz = 120.0, accel_g = 0.5 }]
```

Examples: `example_sine_sweep.toml`, `example_stepped_sine.toml` and `example_ringdown.toml`.
`--level` shifts every level; `--duration` is ignored for sine profiles.

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
- **Pre-flight (before any output):** payload (`safety.payload_kg` + sensor mass) against the
  shaker's static payload limit, rms acceleration, force (moving mass + payload), 3σ velocity
  and 3σ peak-peak displacement against the shaker's data-sheet limits, plus the frequency
  range. The force check is the dynamic payload limit; each shaker file lists it for a few
  acceleration levels. Also checked: the acceleration peak (4.5 × rms for random profiles)
  against the sensor range, and the band against the sensor's frequency range.
- **Runtime aborts:**
  - AI overload;
  - sensor over range (measured peak above the sensor file's `range_g_pk`);
  - drive rms or clipping over the limit;
  - response over the shaker's random rating;
  - open loop (sensor or amplifier lost);
  - rms error or too many lines outside the abort band;
  - any DAQmx error or warning (e.g. underflow, potential glitch).

  Every abort ends with ramp-down, 0 V and the tasks cleared. The drive already queued
  (`ao_queue_blocks` + 1 half-frames, about 250 ms) still plays out before the ramp-down.
  A DAQ fault stops immediately and writes 0 V.

### Sine control

- The same noise-floor and flat random pretest run first, over the test band widened by
  1/3 octave. The pretest H1 gives the feed-forward drive amplitude
  D = c · A_ref(f) · L / |H(f)|, which also checks before any sine output that the drive
  stays within `max_drive_v` and √2 · `max_drive_rms_v`.
- The amplitude is measured every half-frame with a tracking filter: the last frame of the
  control signal is demodulated with the drive phase (delayed by the same I/O delay as the
  FRF) and Hann-weighted. The scalar correction c (dB) is updated from the error with
  `sine.correction_gain`. The loop delay is about four half-frames, so gains above about 0.25
  overshoot.
- Frequency and amplitude change smoothly within each half-frame (log frequency, continuous
  phase); a level starts with a ramp of `sine.ramp_s` from `control.start_level_db` below it.
- **Aborts:** response peak over the shaker's sine rating, open loop, amplitude error beyond
  `abort_db` for 3 half-frames (sweep and dwell), a step that does not settle within
  `max_settle_s`, a drive amplitude over `max_drive_v`, plus the random I/O aborts.
- **Ring-down:** after the dwell the sine is cut at a zero crossing and the drive stays at
  0 V for `ringdown_s`. The control signal (g) is written from
  0.1 s before the cut, with t = 0 at the cut. `events.csv` records the cut as stream time
  (sample-exact) and wall-clock time (stream start + stream time, accurate to about the USB
  latency, tens of ms). Use it to find the decay in an external recording (Q2); precise
  alignment needs a trigger output (not implemented). With the amplifier in voltage mode
  (BDA 120) the amplifier damps the armature at 0 V. In current mode (BK 2707 "High") the
  table keeps moving with the specimen, so measure the specimen's motion relative to the table.
- Response-controlled and phase-resonant sine need the response of the specimen in `vcactl`,
  that is a second AI channel; they are not implemented.

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

The shaker's `max_drive_v` must not exceed `daq.ao_range_v`. IEPE excitation requires AC coupling,
and its current must lie within the sensor's IEPE range. `conditioner_gain` (default 1) is the
gain of an external IEPE conditioner between the sensor and AI (USB-6211).

## Tuning notes (`config/settings.toml`)

- Underflow errors (-200621/-200290) on a slow or busy PC: increase `daq.ao_queue_blocks`.
  This adds latency.
- `daq.fs_io_hz` must be a rate the device supports (see the DAQ profiles above). The control
  band is limited to 0.6 × control Nyquist (7.5 kHz with the USB-6211 defaults, 7.68 kHz with
  the USB-4431/PXIe-4468 defaults).
- `sine.correction_gain` (default 0.15) above about 0.25 overshoots after a sudden change of
  the specimen; lower it for slower, smoother sine control.
- `control.correction_gain` above about 0.2 can overshoot. Raise `control.control_dof` for
  smoother but slower equalization.
- The drive limits `max_drive_v` (peak clip) and `max_drive_rms_v` are in the shaker file,
  because they depend on the amplifier. For the TIRA shakers they are 3.5 V peak and 1.5 V rms;
  the BDA 120 reaches full power at 3.5 V peak (2.47 V rms) sine input. For the BK 4809 they are
  2.7 V peak and 0.88 V rms (80 % of the 1.1 V rms full-rating input at 9.1× gain). Raise the
  rms limit only if a profile needs more drive. `max_drive_rms_v` may not exceed `amp_input_full_v`.

## Development

```
.venv/bin/pytest            # all tests use the simulated shaker and, mostly, the wide-range
                            # test sensor tests/data/sensor_test.toml
vcactl run --sim --shaker bk4809 --profile config/test_profiles/example_low_g.toml --duration 30
```

Layout:

- `src/vcactl/controller.py`: I/O loop, pretest and the random control loop.
- `sine.py`: sine sweep, stepped sine and ring-down.
- `drive.py`: drive synthesis.
- `dsp/`: resampling and spectra.
- `safety.py`: pre-flight (random and sine) and runtime monitors.
- `daq/nidaq.py`, `daq/sim.py`: backends.
- `profile.py`, `config.py`, `logger.py`, `cli.py`.
