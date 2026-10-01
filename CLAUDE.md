# CLAUDE.md

`vcactl` is a frame-based closed-loop random vibration controller (NI DAQ + shaker).
README.md covers hardware wiring, usage, the control algorithm and the code layout; read it first.
This file only records what is not obvious from the code or README.

## Working in the repo

- Setup: `python -m venv .venv && .venv/bin/pip install -e ".[plot,test]"`, then `.venv/bin/pytest`.
  All tests use the simulated backend, so no hardware is needed.
- NI-DAQmx does not work under WSL2. In WSL2 always use `--sim`; real hardware needs native
  Windows or Linux.
- Check a profile without hardware:
  `vcactl check --shaker bk4809 --daq usb6211 --profile config/test_profiles/example_flat.toml`
- Work directly in the checked-out branch; no feature branch or PR is required.
- Don't commit or push. The user reviews and commits all changes themselves.
- Datasheets are in `docs/` (read PDFs with `pdftotext -layout`, or `pypdf` where poppler is not
  installed). Shaker and DAQ profile values must come from them.

## Shaker, DAQ + settings configs

Three files combine into one run, each selected separately:

- Shaker (`--shaker`, `config/shakers/*.toml`): the exciter, its amplifier gain and the drive
  limits at the amplifier input (`max_drive_v`, `max_drive_rms_v`).
- DAQ (`--daq`, `config/daq/*.toml`, default `usb6211`): device settings plus `[limits]` with the
  data-sheet capabilities that settings are validated against (ranges, allowed rates, coupling,
  IEPE, AO sync method, converter filter delay). The settings file can override DAQ settings in a
  `[daq]` table, but never `model` or `[limits]`.
- Settings (`--settings`, default `config/settings.toml`; `--station` is an old alias): sensor,
  control tuning, pretest and the shaker-independent safety settings (payload, clipping,
  open-loop and overload aborts).

Anything that depends on the amplifier belongs in the shaker file, so that no settings/shaker
combination can be wrong. `validate_setup` (called by the CLI and the `Controller`) checks the
cross-file rules: shaker `max_drive_v` <= `daq.ao_range_v` (the USB-4431 AO is only +/-3.5 V) and
`pretest.drive_rms_v` <= shaker `max_drive_rms_v`. `load_shaker` checks
`max_drive_rms_v` <= `max_drive_v` and <= `amp_input_full_v`.

### DAQ device notes

- USB-6211: SAR converters without anti-aliasing, so `decimation` >= 2 and AO is clocked from
  `ai/SampleClock`.
- USB-4431 and PXIe-4468 (DSA): delta-sigma with built-in anti-aliasing; AO runs on its own clock
  and starts on `ai/StartTrigger`; AO is hardware-timed only, so `force_zero` writes a short
  finite burst of zeros. Their converter filter delay (`limits.filter_delay_samples`) is added to
  the controller's drive/response alignment and is emulated by the simulator. For the PXIe-4468
  this delay depends on the rate (datasheet table); update it if `fs_io_hz` changes.
- The DSA profiles are untested on hardware. The AI/AO start-trigger sync, coupling and IEPE
  settings follow the datasheets and the nidaqmx API but have only run against the simulator.

### BK 4809 + 2718 assumptions (not stated in the datasheets)

- Amplifier setup: gain switch at 20 dB, input attenuator fully open, current limit knob at 5 A
  or lower. Into the ~2 ohm coil this gives ~5 A/V, so the 5 A rms rating is reached at 1 V rms
  input (`amp_input_full_v = 1.0`, `[sim] amp_gain_a_per_v = 5.0`). At 40 dB it would be 0.1 V rms.
  If the amplifier gain changes, update `amp_input_full_v`, the drive limits and `[sim]` in the
  shaker file together.
- Drive limits: `max_drive_v = 2.5` (peak clip, about 3 sigma of the rms limit) and
  `max_drive_rms_v = 0.8` (80 % of the 1 V rms rating).
- The datasheets give no random-vibration rating. `force_random_rms_n = 31.5` and
  `accel_random_rms_g = 53` are derived as the 44.5 N sine peak / sqrt(2), and 31.5 N / 0.060 kg.
  Replace them if a real rating becomes available.
- Rated without forced air cooling (44.5 N, 75 g). With cooling the datasheet allows 60 N and
  about 100 g; the profile does not use those.

## Status

- Only simulated runs and `check` have been done for the BK 4809 setup. `pretest` and `run` on
  real hardware are still untested; start with a low `--level` (for example `-12`).
