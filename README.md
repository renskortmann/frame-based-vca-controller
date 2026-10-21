# vcactl — frame-based closed-loop vibration controller

`vcactl` is a closed-loop vibration controller for electrodynamic shakers. It sends the drive
through a DAQ (NI devices through NI-DAQmx, the MCC DT9837A through UL for Linux) and closes the
loop with an IEPE accelerometer. It can run various test profiles and can be configured for
different hardware (shaker and amplifier, DAQ, sensor). Both are explained below, under
[Configuration](#configuration): the hardware files first, then the
[test profiles](#test-profiles---profile). The simulated shaker (`--sim`) runs anywhere, for
development without hardware.

Operating system per DAQ (NI's OS compatibility table, the DT9837 manual):

| DAQ | Windows 11 | Linux |
|---|---|---|
| USB-6211, USB-4431 | yes | no (not supported by NI-DAQmx for Linux) |
| PXIe-4468; cDAQ-9174 with NI 9232, 9234, 9260, 9263 | yes | yes (Ubuntu 22.04/24.04, RHEL, openSUSE) |
| DT9837A | no (only Data Translation's own driver, which vcactl does not use) | yes (UL for Linux) |

> WSL2 cannot run NI-DAQmx, because the driver needs native kernel/USB access: under WSL2 use
> `--sim`. UL for Linux might work in WSL2 with the DT9837A attached through usbipd-win
> (untested).

Contents:

- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Safety](#safety)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Run output](#run-output)
- [Tools](#tools)
- [Development](#development)

## Installation

1. Install Python (3.9 or newer), then verify (on Linux the command may be `python3`):
   ```
   python --version
   ```
2. Create and activate a virtual environment:
   ```
   python -m venv .venv
   source .venv/bin/activate                       # Windows: .venv\Scripts\activate
   ```
3. Install `vcactl` in that environment (this also installs the Python `nidaqmx` package):
   ```
   .venv/bin/pip install -e ".[plot,test]"        # Windows: .venv\Scripts\pip ...
   ```
4. Install the **driver** of the DAQ (required for real hardware, separate from the Python
   package):
   - NI devices on Windows 11: NI-DAQmx from ni.com, or `python -m nidaqmx installdriver` after
     step 3.
   - NI devices on Linux (PXIe-4468 and CompactDAQ; not the USB-6211/4431): NI-DAQmx for Linux on
     a supported distribution (Ubuntu LTS, RHEL, openSUSE). Follow NI's repository instructions
     and reboot so the kernel modules load.
   - DT9837A on Linux: build and install libuldaq from MCC's release at
     <https://github.com/mccdaq/uldaq> (needs `libusb-1.0-0-dev`; `./configure && make && sudo
     make install` also installs the udev rules), then `.venv/bin/pip install -e ".[ul]"`.
5. Check that the device is visible. If it has a different name, set `daq.device` to that name
   (CompactDAQ: the chassis, and `ai_device`/`ao_device` to the modules; DT9837A: the unique ID,
   needed only with several modules):
   ```
   vcactl list-devices
   ```
6. Before connecting a shaker to a new DAQ, run the loopback self-test (see [Usage](#usage)).

## Configuration

### Overview

A run combines four configuration files and a test profile. Each option takes a key, which
selects `<config-dir>/<directory>/<key>.toml`, or a path to a TOML file anywhere (for example a
specimen-specific profile):

| Option | Directory | Default | Keys |
|---|---|---|---|
| `--shaker` | `config/shakers/` | required | `tv51110`, `tv52110`, `bk4809`, `bk4801_4812` |
| `--daq` | `config/daq/` | `usb6211` | `usb6211`, `usb4431`, `pxie4468`, `cdaq9234_9260`, `cdaq9232_9263`, `dt9837a` |
| `--sensor` | `config/sensors/` | `pcb356b18` | `pcb356b18` |
| `--settings` | `config/settings/` | `default` | `default` |
| `--profile` | `config/test_profiles/` | required | `low_g`, `flat`, `sloped`, `wideband`, `sine_sweep`, `stepped_sine`, `ringdown` |

`--shaker` and `--profile` have no default on purpose: the shaker file sets the
amplifier-dependent drive limits, and the profile is the test itself, so neither should be
chosen silently.

All files are validated when they are loaded, including the rules that involve more than one
file (for example the shaker's drive limit against the DAQ's AO range).

Cross-file rules:

- `--daq` selects a DAQ profile; `[daq]` in `--settings` may override DAQ settings (for example
  `device`), but not `model` or `[limits]`.
- The control sensor is selected only with `--sensor`; `[sensor]` in the settings file is rejected.
- `shaker.max_drive_v` must be within `daq.ao_range_v`.
- `pretest.drive_rms_v` must be within `shaker.max_drive_rms_v`.
- If IEPE is enabled (`daq.iepe_current_ma > 0`), `ai_coupling` must be AC and the current must
  be within the sensor's IEPE range.
- The profile band must lie within the shaker's and the sensor's frequency ranges and below the
  DAQ's highest test frequency (see [DAQ devices](#daq-devices---daq)). This is checked per
  run, before the pre-flight check: one `configuration error` lists every component that
  limits the band.

The third-party datasheets and manuals (shakers, amplifiers, DAQs, sensors) are not in this
repository. They are in the project's Nextcloud folder (`CSAA04 HF Shakers (Alessandro)`):
`Shakers/` and `Controller + DAQ/`. The source comments in `config/shakers/*.toml`,
`config/daq/*.toml` and `config/sensors/*.toml` refer to them by file name.

### Shakers (`--shaker`)

A shaker file describes the exciter and its amplifier: the data-sheet ratings, the amplifier
gain and the drive limits at the amplifier input.

| `--shaker` | Shaker | Amplifier | Band (Hz) | `max_drive_v` / `max_drive_rms_v` |
|---|---|---|---|---|
| `tv51110` | TIRA TV 51110 | BDA 120 | 2–7000 | 3.5 V peak / 1.5 V rms |
| `tv52110` | TIRA TV 52110 | BDA 120 | 2–7000 | 3.5 V peak / 1.5 V rms |
| `bk4809` | BK 4809 | DAP-Audio Palladium P-2000 | 20–20000 | 2.7 V peak / 0.88 V rms |
| `bk4801_4812` | BK 4801 body with 4812 general purpose head | BK 2707 | 5–10000 | 3.5 V peak / 1.25 V rms |

The drive limits `max_drive_v` (peak clip) and `max_drive_rms_v` are in the shaker file,
because they depend on the amplifier. The BDA 120 reaches full power at 3.5 V peak (2.47 V rms)
sine input. For the BK 4809 the limits are 80 % of the 1.1 V rms full-rating input at 9.1× gain.
Raise the rms limit only if a profile needs more drive. `max_drive_rms_v` may not exceed
`amp_input_full_v`. The header comments of each shaker file describe the assumed amplifier
setup, and a table of payload limits.

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

### DAQ devices (`--daq`)

A DAQ profile holds the device settings (device name, channels, ranges, coupling, IEPE, rates)
and, under `[limits]`, the device's data-sheet capabilities. All settings are checked against
these limits when the configuration is loaded. The settings file can override DAQ settings
in its own `[daq]` table (for example `device = "Dev2"`), but not the limits.

| `--daq` | AI ranges (V) | AO range (V) | Rates | Notes |
|---|---|---|---|---|
| `usb6211` | 0.2, 1, 5, 10 | 10 | 20 MHz / n, ≤ 250 kS/s | no anti-aliasing filter: `decimation` ≥ 2 |
| `usb4431` | 10 | 3.5 | 51.2 k, 80 k, 96 k ÷ 2ⁿ | IEPE 2.1 mA; shaker `max_drive_v` ≤ 3.5 |
| `pxie4468` | 0.316 … 42.4 | 0.316, 1, 3.16, 10 | 100 S/s – 200 kS/s | IEPE 4/10/20 mA; the AO delay depends on the rate |
| `cdaq9234_9260` | 5 | 4.24 | 51.2 kS/s ÷ n (n 1–31) | cDAQ-9174 + NI 9234 (IEPE 2 mA) + NI 9260; AO uses the AI timebase |
| `cdaq9232_9263` | 30 | 10 | 102.4, 51.2, 25.6 kS/s ÷ n | cDAQ-9174 + NI 9232 (IEPE 4 mA) + NI 9263 (no reconstruction filter: `decimation` ≥ 2) |
| `dt9837a` | 1, 10 | 10 | 10–52.734 kS/s | UL for Linux; IEPE 4 mA; AO 10 kHz output filter; AO lag about 0.5 s |

- `conditioner_gain` (default 1) is the gain of an external IEPE conditioner between the sensor
  and AI (USB-6211).
- **Rates.** `fs_io_hz` and `decimation` are `"auto"` in every DAQ file: each run chooses them
  from the profile band, and `check`, `pretest` and `run` print the choice on the `rates` line.
  The I/O rate is the lowest of `daq.fs_io_auto_hz` (USB-4431: its AO update rates) whose band
  covers the profile's f_hi. The band is the converter passband, `limits.passband_fraction` ×
  `fs_io_hz`, and, with `decimation` > 1, the passband of the rate-change FIR filter,
  0.6 × control Nyquist (0.3 × `fs_io_hz` / `decimation`). The pre-flight row
  `profile f_hi <= DAQ band` shows it. The decimation is 1 where both the AI and the AO
  converters filter (USB-4431, PXIe-4468, `cdaq9234_9260`, DT9837A). Otherwise (USB-6211,
  `cdaq9232_9263`) it is the largest factor from 2 to 16 that still covers the band and gives a
  whole-number control rate. A number in `fs_io_hz` or
  `decimation` (in the DAQ file, or in `[daq]` of the settings file) fixes that value, and the
  other one is chosen around it.

  | `--daq` | `fs_io_auto_hz` | `low_g` (20–3000 Hz) gets | Highest test frequency |
  |---|---|---|---|
  | `usb6211` | 100, 125, 200, 250 kS/s | 100 kS/s, decimation 10 (10 kS/s control) | 37.5 kHz (250 kS/s, decimation 2) |
  | `usb4431` | the AO update rates, 1.25–96 kS/s | 10 kS/s, decimation 1 | 43.5 kHz (96 kS/s) |
  | `pxie4468` | 1.6, 3.2, … 102.4, 200 kS/s | 12.8 kS/s, decimation 1 | 90.8 kHz (200 kS/s) |
  | `cdaq9234_9260` | 2.048, 2.56, … 25.6, 51.2 kS/s (51.2 k/n) | 10.24 kS/s, decimation 1 | 23.04 kHz (51.2 kS/s) |
  | `cdaq9232_9263` | 51.2, 102.4 kS/s | 51.2 kS/s, decimation 5 (10.24 kS/s control) | 15.36 kHz (102.4 kS/s, decimation 2) |
  | `dt9837a` | 51.2, 52.734 kS/s | 51.2 kS/s, decimation 1 | 23.9 kHz (52.734 kS/s) |

  - USB-6211: there is no analog anti-aliasing or reconstruction filter, so AI stays oversampled
    at ≥ 100 kS/s and only the decimation lowers the control rate. Response content up to
    50 kHz (the PCB 356B18 resonance is ≥ 20 kHz) is then removed by the decimation FIR instead
    of folding into the band. The higher rates are used only for bands above 15 kHz. Near
    37.5 kHz the AO images near `fs_io_hz` are only about 15 dB below the drive. At 250 kS/s
    the 4,095-sample AI FIFO lasts 16 ms, and the rates need a Hi-Speed USB connection; remove
    rates from `fs_io_auto_hz` if the PC cannot keep up.
  - USB-4431 and PXIe-4468: the built-in converter filters are alias-free up to 0.453 fs and
    0.454 fs. At low rates the AI sees only the test band, so response outside it is not
    monitored (as in commercial controllers).
  - PXIe-4468: the AO delay depends on the rate (65 samples at ≤ 10 kS/s up to 106 at
    200 kS/s). `limits.filter_delay_max_rate_hz` and `filter_delay_by_rate` hold the datasheet
    table.
  - CompactDAQ: `device` is the chassis (`cDAQ1`, whose terminals route the clocks);
    `ai_device` and `ao_device` are the modules (`cDAQ1Mod1`, `cDAQ1Mod2`): check the slots in
    NI MAX.
    - `cdaq9234_9260`: the two delta-sigma modules each have their own oscillator, so the AO
      task takes the AI task's sample clock timebase (`ao_sync = "shared_timebase"`) and starts
      on the AI start trigger. Delay 71 samples.
    - `cdaq9232_9263`: the NI 9263 has no reconstruction filter, so, as on the USB-6211, the
      drive is interpolated (decimation ≥ 2) and the I/O rate stays ≥ 51.2 kS/s. Its AO is
      clocked from the NI 9232's sample clock. Delay 30–31 samples.

    Both have only run in the simulator: run `vcactl loopback` before connecting a shaker.
  - DT9837A:
    - **Alignment:** UL for Linux starts AI and AO with separate commands. Every run therefore
      begins with a 10 mV, 40 ms chirp on the AO, which the backend finds in the AO readback
      channel to align the streams.
    - **AO filter:** a 2-pole 10 kHz Butterworth (−3 dB at 10 kHz, −12 dB at 20 kHz). The loop
      compensates it, but the drive limits apply before it: a flat test loses 0.8 / 3 / 6 / 9 dB
      of level for bands to 10 / 15 / 20 / 23.9 kHz, and above 10 kHz the logged drive is the
      value before the filter.
    - **AO lag:** uldaq keeps the 8192-sample AO FIFO and 32 USB transfers full, so the output
      runs about 0.5 s behind the data handed over (1.2 s at 10 kS/s). The rates therefore stay
      ≥ 51.2 kS/s, and the AO queue is raised per run; the `rates` line shows it (8 half-frames,
      0.64 s). About 0.6 s of drive still plays after an abort.
    - **Sine:** the sine correction gain is scaled down for the longer loop. Sweeps through
      resonances track less tightly: in the simulator 85 % of the half-frames are within 1 dB at
      6 oct/min, against 98 % on the NI devices, and 96 % at 3 oct/min. Sweep at about half the
      rate.
- **Frame size.** `[control] frame_size = "auto"` (the default) is the smallest power of two
  with a line spacing Δf = control rate / `frame_size` of at most `control.line_spacing_hz`
  (6.25 Hz) and at most f_lo / 2 (the profile starts at ≥ 2 lines). A frame then lasts
  160–320 ms at every rate, so the loop timing stays the same: frames per level, pretest
  frames, averaging and the abort reaction. A low f_lo needs finer lines and longer frames. A
  fixed `frame_size` is checked instead: if f_lo is below 2 lines at the lowest suitable rate,
  the run stops with a configuration error.
- **Mismatches.** Before the pre-flight check, the profile band is compared with the shaker's
  and the sensor's frequency ranges, the DAQ's highest test frequency and a fixed frame size.
  Every mismatch is listed in one `configuration error`, and nothing is output.
- Underflow errors (-200621/-200290, or `AO underrun` on the DT9837A) on a slow or busy PC:
  increase `daq.ao_queue_blocks`. This adds latency.
- **AI range.** `daq.ai_range_v = "auto"` (the default) picks, per run, the smallest range of
  the device that holds the expected AI peak at `--level` plus `safety.ai_range_margin_db`
  (3 dB) below `safety.ai_overload_fraction` (98 %) of the range. The expected peak is the
  expected acceleration peak (see [Sensors](#sensors---sensor)) × the sensitivity at the DAQ
  input. A smaller range gives a finer ADC step: on the USB-6211, 305 µV on ±10 V but 6 µV on
  ±0.2 V. `check`, `pretest` and `run` print the chosen range; the pre-flight check fails if
  even the largest range is too small. `--ai-range <V>` or a number in `daq.ai_range_v` sets
  the range by hand.
- The range stays the same for the whole run (noise floor, pretest, level ramp, test), so it is
  chosen for the target level. The pretest first probes the response at −20 dB of
  `pretest.drive_rms_v` and lowers its drive if the pretest would use more than half of the
  range (low `--level` on a small range); it never raises it.

### Sensors (`--sensor`)

The accelerometer is selected with `--sensor` (default `pcb356b18`). Its file holds the
calibrated sensitivity of the axis used for control (`sensitivity_mv_per_g`), the measurement
range (`range_g_pk`), the frequency range (`f_min_hz`, `f_max_hz`), the mass (`mass_kg`, added
to `safety.payload_kg`) and the IEPE excitation range (`iepe_min_ma`, `iepe_max_ma`). The
controller uses them as follows:

- The sensitivity at the DAQ input is `sensitivity_mv_per_g` × `daq.conditioner_gain`. The
  conditioner gain (default 1) is for an external IEPE conditioner with gain (USB-6211).
- A profile band outside the sensor's frequency range stops the run with a configuration
  error. The pre-flight check fails if the expected acceleration peak exceeds `range_g_pk`. For
  random profiles the expected peak is `safety.accel_crest_factor` (4.5) × rms; for sine
  profiles it is the sine peak.
- A test aborts when the measured peak exceeds `range_g_pk`.
- With IEPE excitation from the DAQ, `daq.iepe_current_ma` must lie within the sensor's IEPE
  range. The sensor's highest output bias (`iepe_bias_max_v`) plus its swing at `range_g_pk`
  must stay below the DAQ's IEPE compliance voltage (`limits.iepe_compliance_v`). For the
  356B18: 12 V + 5.24 V = 17.2 V, within the DT9837A's 18 V and the NI 9234's 19 V.

The AI range follows the expected peak automatically (see the DAQ notes above). Choose the
accelerometer so that the expected peak also stays inside the input range of any other
instrument that shares the signal.

**PCB 356B18** (`pcb356b18`, serial LW266119): a triaxial, high-sensitivity sensor for
low-frequency, low-level tests: 1000 mV/g nominal, ±5 g, 0.5–3000 Hz (±5 %), 25 g. Only one axis
closes the loop: z (1048 mV/g calibrated). The file also lists the x (980) and y (1054)
calibrations. Notes:

- It needs a 4-pin (1/4-28) to 3 × BNC breakout cable. Connect only the control axis.
- The output carries an 8–12 V bias. Use AC coupling: IEPE from the DAQ (USB-4431 2.1 mA,
  PXIe-4468 4/10/20 mA, NI 9234 2 mA, NI 9232 4 mA, DT9837A 4 mA), or the AC-coupled output of
  an external conditioner on the USB-6211. The NI 9234's ±5 V input limits it to about 4.7 g.
- Wait at least 12 s after switching on the IEPE excitation before starting a pretest; the
  output needs that long to settle.
- 1 V/g leaves little room: a 0.55 g rms random test (`low_g.toml`) already peaks near
  2.5 g. The default `pretest.drive_rms_v` (0.01 V) keeps the pretest within ±5 g on every
  shaker; at 0.05 V the BK shakers exceed it.

A less sensitive, wider-band accelerometer will get its own file later for the higher-level
example profiles (`flat`, `sloped`, `wideband`), which fail the
356B18's range check.

### Wiring and power-up

The control sensor is always an IEPE accelerometer, mounted on the shaker table or fixture next
to the specimen. How it is powered depends on the DAQ:

- **USB-4431 / PXIe-4468:** connect the accelerometer directly to AI 0 (BNC) and switch on the
  DAQ's IEPE excitation with `iepe_current_ma` in the DAQ profile (`config/daq/*.toml`): 2.1 mA on
  the USB-4431, 4, 10 or 20 mA on the PXIe-4468. IEPE needs `ai_coupling = "AC"`. Check that the
  accelerometer works at the chosen current (2.1 mA is below the 4 mA many sensors are specified at).
  Connect AO 0 to the amplifier input.
- **cDAQ-9174:** connect the accelerometer to AI 0 of the NI 9234 (BNC) or NI 9232, and switch
  on IEPE with `iepe_current_ma` (2 mA on the 9234, 4 mA on the 9232) and AC coupling. Connect
  AO 0 of the NI 9260 (BNC) or of the NI 9263 (terminals AO0 and COM) to the amplifier input.
  The NI 9263 drives at most 1 mA, so the amplifier input must be ≥ 10 kΩ.
- **DT9837A:** connect the accelerometer to AI 0 (BNC), IEPE 4 mA with AC coupling, and AO 0
  (BNC, ≥ 3.3 kΩ load) to the amplifier input.
- **USB-6211:** it has no IEPE excitation, so an external IEPE signal conditioner powers the
  accelerometer and its output goes to the DAQ (wiring below). Leave the conditioner's output
  AC-coupled, so that the IEPE bias voltage does not reach the DC-coupled USB-6211 input.

| USB-6211 terminal | Connect to |
|---|---|
| AO 0 (pin 12) / AO GND (pin 14) | amplifier signal input (BNC; TRS/XLR on the P-2000) |
| AI 1 (pin 17) / AI 9 (pin 18) | IEPE conditioner output: signal / reference (differential) |
| AI GND (pin 28) | if the conditioner output is **floating** (battery powered): 10–100 kΩ from AI 9 to AI GND |

Power sequence, which matters because the USB-6211 AO glitches by about ±1 V for 200 ms at power-on
(the USB-4431 AO also glitches at power-on; the NI 9263 by −1.5 V for 20 µs at power-on and up to
1.8 V at power-down; the NI 9260 by 400 mV at power-on, and it steps by 100 mV for 200 µs at the
first output of every run):

1. Connect the DAQ and let the PC enumerate it. Leave the **amplifier off**.
2. Switch on the amplifier with its gain knob in the usual position. Keep the knob in the same
   position for the whole test, because the pretest measures the system with that gain.
3. When finished, switch the amplifier **off before** unplugging USB or shutting down the PC.

### Settings (`--settings`)

`config/settings/default.toml` (key `default`) holds the settings that do not depend on the
hardware files: control
tuning (`[control]`, `[sine]`), safety (`[safety]`: payload, clipping, open-loop and overload
aborts) and the pretest (`[pretest]`). The sensor is not in it; it comes from `--sensor`.
Tuning notes:

- `control.correction_gain` above about 0.2 can overshoot. Raise `control.control_dof` for
  smoother but slower equalization.
- `sine.correction_gain` (default 0.15) above about 0.25 overshoots after a sudden change of
  the specimen; lower it for slower, smoother sine control.
- `control.frame_size` and `control.line_spacing_hz` set the frequency resolution; the frame
  size follows the DAQ rate (see [DAQ devices](#daq-devices---daq)).

### Test profiles (`--profile`)

Test profiles are in `config/test_profiles/*.toml`; the file name is the key
(`--profile low_g`). `type` selects the test: random (the
default, no `type` needed), `type = "sine_sweep"` or `type = "stepped_sine"`. The `[tolerance]`
table is optional. `--level` shifts the whole profile by that many dB.

#### Random

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

Examples: `low_g.toml` (0.55 g rms, 20–3000 Hz; fits the PCB 356B18), and
`flat.toml`, `sloped.toml` and `wideband.toml` (higher levels, for a
less sensitive sensor).

#### Sine sweep

The reference amplitude of both sine types is a breakpoint table; each breakpoint gives one of
`accel_g` (peak), `velocity_m_s` (peak) or `displacement_mm_pp` (peak-peak). Acceleration is
interpolated log-log between breakpoints and held constant beyond them, so two displacement
breakpoints give exactly constant displacement. For "constant displacement, then constant
acceleration", put a breakpoint at the crossover frequency.

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

Example: `sine_sweep.toml`. `--duration` is ignored for sine profiles.

#### Stepped sine and ring-down

The amplitude breakpoints work as for the sine sweep. Each step settles, dwells and, with
`ringdown_s` > 0, cuts the drive to record the ring-down (see
[Sine control](#sine-control)).

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

Examples: `stepped_sine.toml` and `ringdown.toml`. `--level` shifts every level;
`--duration` is ignored.

## Usage

| Command | Purpose | Notes |
|---|---|---|
| `vcactl list-devices` | List NI-DAQmx and UL for Linux devices. | Real DAQ backend only. |
| `vcactl check ...` | Run pre-flight checks only (no output drive). | Exits non-zero on failed checks. |
| `vcactl pretest ...` | Measure noise floor + FRF at low level, then stop. | Useful hardware sanity check before `run`. |
| `vcactl run ...` | Run a random or sine closed-loop test. | `--duration` applies to random profiles only. |
| `vcactl loopback ...` | Self-test of a DAQ: AO looped back into a spare AI input, amplifier disconnected. | Delay, clock drift and gain; no shaker or profile. |

Options of `check`, `pretest` and `run`
(see [Configuration](#configuration) for the keys):

- Required: `--shaker`, `--profile`.
- Defaults: `--daq usb6211`, `--sensor pcb356b18`, `--settings default` (alias: `--station`),
  `--config-dir config/`. All five take a key or a path.
- `--level` shifts the profile level in dB (default 0).
- `--ai-range` sets the AI range in V (one of the DAQ's ranges) or `auto` (default:
  `daq.ai_range_v`, normally `auto`).

Only for `pretest` and `run`:

- `--sim` uses the simulated shaker instead of the DAQ; `--sim-realtime` paces it in real time.
- `--log-dir` sets the run output root (default `runs/`).
- `--yes` skips the confirmation prompt before driving real hardware.
- `--seed` fixes the random drive (reproducible runs).

Only for `run`: `--duration` sets the test duration at full level in s (random profiles; default
from the profile).

`loopback` drives low-level noise (`--level-v`, default 0.05 V rms) at the DAQ's highest
automatic rate for `--duration` s (default 120) and reads it back on `--loopback-channel`
(IEPE off, DC coupling). It reports the AO → AI delay at the start and the end, the drift
between the AO and AI clocks, and the gain at a few frequencies. It FAILs when the drift
exceeds 1 ppm or the delay differs from `limits.filter_delay_samples` by more than 2 samples.
Disconnect the amplifier first; `--sim` runs it on the simulator.

```
vcactl loopback --daq cdaq9234_9260 --loopback-channel ai1
```

```
vcactl check   --shaker bk4809 --profile low_g [--level -6]
vcactl pretest --shaker bk4809 --profile low_g
vcactl run     --shaker bk4809 --profile low_g [--level 0] [--duration 60]
```

Without hardware, try the setup and the profile on the simulated shaker first. `check` never
drives the DAQ, so it needs no `--sim`:

```
vcactl check         --shaker bk4809 --profile low_g
vcactl pretest --sim --shaker bk4809 --profile low_g
vcactl run     --sim --shaker bk4809 --profile low_g --duration 10
```

Ctrl-C (or SIGTERM) ramps the drive down smoothly. A second Ctrl-C stops immediately and forces
AO to 0 V.

> **Status:** `pretest` and `run` have only been run against the simulated shaker so far. On
> real hardware, start at a low level (`--level -12`).

Recommended first test on hardware:

1. With a new DAQ: run `vcactl loopback ...` with the amplifier disconnected.
2. Run `vcactl check ...` and fix any `FAIL`.
3. Run `vcactl pretest ...` and check the SNR, the mean coherence and the predicted drive voltage.
4. Run `vcactl run ... --level -12 --duration 10`.
5. Run at full level.

## Safety

- **Pre-flight (before any output):** payload (`safety.payload_kg` + sensor mass) against the
  shaker's static payload limit, rms acceleration, force (moving mass + payload), 3σ velocity
  and 3σ peak-peak displacement against the shaker's data-sheet limits, plus the frequency
  range. The force check is the dynamic payload limit; each shaker file lists it for a few
  acceleration levels. Also checked: the acceleration peak (4.5 × rms for random profiles)
  against the sensor range, the band against the sensor's frequency range, and the expected
  AI peak (+ 3 dB) against the AI range. A band outside the shaker's or the sensor's frequency
  range or above the DAQ's highest test frequency stops the run earlier, with a
  `configuration error`.
- **Runtime aborts:**
  - AI overload;
  - sensor over range (measured peak above the sensor file's `range_g_pk`);
  - drive rms or clipping over the limit;
  - response over the shaker's random rating;
  - open loop (sensor or amplifier lost);
  - rms error or too many lines outside the abort band;
  - any DAQmx or UL error or warning (e.g. underflow, potential glitch).

  Every abort ends with ramp-down, 0 V and the tasks cleared. The drive already queued
  (`ao_queue_blocks` + 1 half-frames, about 250–500 ms; about 0.7 s on the DT9837A) still plays
  out before the ramp-down. A DAQ fault stops immediately and writes 0 V. On the DT9837A a
  watchdog also stops the AO at 0 V if the controller stops writing, because a continuous UL
  scan would otherwise replay old drive.
- **Sine aborts:** response peak over the shaker's sine rating, open loop, amplitude error
  beyond `abort_db` for 3 half-frames (sweep and dwell), a step that does not settle within
  `max_settle_s`, a drive amplitude over `max_drive_v`, plus the random I/O aborts.

## Troubleshooting

- `configuration error: ...` at startup:
  - Check for unknown or misspelled TOML keys.
  - Check the cross-file rules (see [Overview](#overview)): drive limits, IEPE current and
    coupling, no `[sensor]` table in the settings file.
  - `the profile (...) does not fit the selected hardware and settings`: each line names the
    component that limits the band (shaker, sensor, DAQ, or a fixed `control.frame_size`).
    Choose another shaker, sensor or DAQ, narrow the profile band, or use
    `frame_size = "auto"`.
- `cannot query NI-DAQmx: Could not find an installation of NI-DAQmx ...`:
  - The NI-DAQmx driver is missing: install it (Installation, step 4) and reboot.
  - Under WSL2 the driver cannot work; use `--sim`.
- `cannot query UL for Linux: No module named 'uldaq'`: only needed for the DT9837A; install
  libuldaq and the `ul` extra (Installation, step 4).
- `no DAQ devices found`:
  - The driver works, but sees no device: check the USB/PXI connection and that the device
    appears in NI MAX, then rerun `vcactl list-devices`.
- DT9837A:
  - `no AO marker in the readback channel`: the AO did not start; check the USB connection.
  - `AO underrun` or `the AO output lags ... behind`: the PC did not keep up, or the AO lag is
    longer than expected; raise `limits.ao_transfer_s` (DAQ file) or `daq.ao_queue_blocks`.
- `vcactl loopback` FAIL:
  - delay error: correct `filter_delay_samples` in the DAQ file to the measured delay;
  - drift: AO and AI are not on one clock (on `cdaq9234_9260`, the timebase sharing did not
    work);
  - no loopback signal: check the cable and `--loopback-channel`.
- Pre-flight `FAIL` on profile checks:
  - Start with `--profile low_g` and `--level -12`.
  - Re-check the sensor range and the shaker's force and displacement limits.
- Runtime aborts (`AI overload`, `sensor over range`, `drive clipping`, `open loop`):
  - Lower `--level`, verify amplifier gain position/cabling, verify accelerometer mounting and
    IEPE setup, then rerun `pretest`.
  - `AI overload` on an automatically chosen range: the response is larger than the profile
    predicts (for example a resonance outside the band, or a wrong sensitivity in the sensor
    file). Check the sensor file, or choose a larger range with `--ai-range`.

## How it works

### Random control

- **Rates:** AI and AO run on one shared sample clock at the I/O rate chosen for the run (see
  [DAQ devices](#daq-devices---daq)). The USB-6211 has no anti-aliasing filter: AO is clocked
  from `ai/SampleClock`, AI is oversampled at ≥ 100 kS/s, and FIR decimation and interpolation
  (≥ 80 dB stop band) convert to the control rate, for example 10 kS/s (decimation 10) for a
  3 kHz band. The USB-4431 and PXIe-4468 have delta-sigma converters with built-in
  anti-aliasing. They run without decimation at the lowest rate whose passband covers the band.
  Their AO has its own sample clock from the same timebase and starts on `ai/StartTrigger`.
  On the cDAQ-9174, the NI 9260 takes the NI 9234's timebase, and the NI 9263 is clocked from
  the NI 9232's sample clock; the DT9837A streams are aligned on its AO readback. The converter
  filters delay the response by up to 106 samples (`limits.filter_delay_samples`; per
  rate for the PXIe-4468 and the NI 9232), and the controller shifts the drive by that amount
  before estimating the FRF. The frame size follows the control rate, so the line spacing is
  3.1–6.25 Hz and one loop step (half a frame) takes 80–160 ms at every rate, unless a low
  f_lo needs finer lines.
- **Drive:** each frame gets new random phases. Frames are sqrt-Hann windowed and overlap-added
  at 50 %, giving a continuous, Gaussian, non-periodic signal. AO regeneration is off, so the
  output always comes from the controller.
- **Sequence:**
  1. noise floor with zero drive;
  2. pretest with flat low-level noise (drive lowered if needed to fit the AI range): H1 FRF
     and coherence, SNR and coherence checks, predicted drive at the target level;
  3. closed loop: drive PSD = C·S_ref/|H|², where the per-line correction C is updated every
     frame from the averaged control PSD. H continues to update during the run;
  4. level ramp from −12 dB in 3 dB steps, each held until the rms error is ≤ 1 dB, then the
     test duration at full level;
  5. ramp-down to 0 V.

### Sine control

- The same noise-floor and flat random pretest run first, over the test band widened by
  1/3 octave. The pretest H1 gives the feed-forward drive amplitude
  D = c · A_ref(f) · L / |H(f)|, which also checks before any sine output that the drive
  stays within `max_drive_v` and √2 · `max_drive_rms_v`.
- The amplitude is measured every half-frame with a tracking filter: the last frame of the
  control signal is demodulated with the drive phase (delayed by the same I/O delay as the
  FRF) and Hann-weighted. The scalar correction c (dB) is updated from the error with
  `sine.correction_gain`. The loop delay is about four half-frames, so gains above about 0.25
  overshoot. With a longer AO queue (DT9837A: 8 half-frames) the gain is scaled by
  4 / (queue + 2).
- Frequency and amplitude change smoothly within each half-frame (log frequency, continuous
  phase); a level starts with a ramp of `sine.ramp_s` from `control.start_level_db` below it.
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

## Run output

Each run directory (under `--log-dir`, default `runs/`) contains:

- `meta.json`: all configs (with the rates and frame size of the run) and the `rates` choice.
- `status.csv`: one row per half-frame.
- `pretest_frf.csv`: FRF, coherence and noise floor.
- `psd_<t>s.csv` snapshots and `psd_final.csv`: reference, control PSD, tolerance bands,
  drive PSD, FRF and correction.

Sine runs have their own `status.csv` columns (frequency, reference/measured peak amplitude,
error, drive amplitude, phase) and, for stepped sine:

- `steps.csv`: one row per step with the dwell mean of the measured amplitude, drive, H
  magnitude and phase, displacement and the (approximate) wall-clock start time.
- `events.csv` and `ringdown_<step>.csv`: drive cuts and the recorded decays (see
  [Sine control](#sine-control)).

## Tools

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

`tools/client_options_deck.py` built the first draft of the shaker and controller options
presentation from the same shaker data. The deck has since been edited by hand in the Nextcloud
folder (`Controller + DAQ/shaker_controller_options_draft.pptx`); do not rerun the script over
it, because that would overwrite the edits.

## Development

```
.venv/bin/pytest            # all tests use the simulated shaker and, mostly, the wide-range
                            # test sensor tests/data/sensor_test.toml
vcactl run --sim --shaker bk4809 --profile low_g --duration 30
```

Layout:

- `src/vcactl/controller.py`: I/O loop, pretest and the random control loop.
- `sine.py`: sine sweep, stepped sine and ring-down.
- `drive.py`: drive synthesis.
- `dsp/`: resampling and spectra.
- `safety.py`: pre-flight (random and sine) and runtime monitors.
- `rates.py`: DAQ rate and frame size per run, and the band compatibility check.
- `daq/nidaq.py` (NI-DAQmx), `daq/ul.py` (UL for Linux, DT9837A), `daq/sim.py`: backends.
- `loopback.py`: the loopback self-test.
- `profile.py`, `config.py`, `logger.py`, `cli.py`.
