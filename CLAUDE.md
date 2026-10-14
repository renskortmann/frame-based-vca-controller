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
- Datasheets and manuals are not in the repo (the old `docs/` folder is gone); they are in the
  Nextcloud project folder (see "Documents outside the repo"), which is the second folder of the
  VS Code workspace `frame-based-vca-controller.code-workspace` (git-ignored on purpose; don't add
  it). Read PDFs with `pdftotext -layout`, or `pypdf` where poppler is not installed. Shaker and
  DAQ profile and sensor values must come from them. The `docs/...` paths in the config file comments are
  the old locations; the file names are unchanged.

## Shaker, DAQ, sensor + settings configs

Four files combine into one run, each selected separately:

- Shaker (`--shaker`, `config/shakers/*.toml`): the exciter, its amplifier gain and the drive
  limits at the amplifier input (`max_drive_v`, `max_drive_rms_v`).
- DAQ (`--daq`, `config/daq/*.toml`, default `usb6211`): device settings plus `[limits]` with the
  data-sheet capabilities that settings are validated against (ranges, allowed rates, coupling,
  IEPE, AO sync method, converter filter delay). The settings file can override DAQ settings in a
  `[daq]` table, but never `model` or `[limits]`. `conditioner_gain` is the gain of an
  external IEPE conditioner (USB-6211).
- Sensor (`--sensor`, `config/sensors/*.toml`, default `pcb356b18`): the control accelerometer's
  calibrated sensitivity, range, frequency range, mass and IEPE current range. Settings files
  may not have a `[sensor]` table (the loader rejects it).
- Settings (`--settings`, default `config/settings.toml`; `--station` is an old alias): control
  tuning, pretest and the shaker-independent safety settings (payload, clipping, open-loop and
  overload aborts).

Anything that depends on the amplifier belongs in the shaker file, so that no settings/shaker
combination can be wrong. `validate_setup` (called by the CLI and the `Controller`) checks the
cross-file rules: shaker `max_drive_v` <= `daq.ao_range_v` (the USB-4431 AO is only +/-3.5 V) and
`pretest.drive_rms_v` <= shaker `max_drive_rms_v`. `load_shaker` checks
`max_drive_rms_v` <= `max_drive_v` and <= `amp_input_full_v`. `validate_settings` checks a
DAQ IEPE current against the sensor's IEPE range. The pre-flight check compares the band with
the sensor's frequency range and the acceleration peak (`safety.accel_crest_factor` x rms for
random) with `range_g_pk`; the runtime monitor aborts on a measured peak above it. The payload
in all checks and in the simulator is `safety.payload_kg` + sensor `mass_kg`
(`Settings.payload_total_kg`); the chain sensitivity is `Settings.v_per_g`.

### PCB 356B18 (default sensor)

- Serial LW266119; calibration from the user: x 980, y 1054, z 1048 mV/g. The z axis closes the
  loop (`sensitivity_mv_per_g = 1048`); the others are only in the file's comments.
- Intended for low-frequency, low-g tests: +/-5 g, 0.5-3000 Hz (+/-5 %). The shared
  `pretest.drive_rms_v` was lowered from 0.05 to 0.01 V for it: at 0.05 V the simulated pretest
  peaks above 5 g on the BK 4809 and BK 4801/4812. The higher-level example profiles
  (`example_flat`, `example_sloped`, `example_wideband`) fail its range check; they wait for the
  second, less sensitive and wider-band accelerometer, which gets its own sensor file and probably
  becomes the default.
- Tests mostly use `tests/data/sensor_test.toml` (10 mV/g, +/-500 g, 1-10000 Hz) so that they are
  not limited by the 356B18.

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

### Payload limits (all shaker files)

- `payload_static_max_kg`: the payload whose weight uses up the half-stroke when mounted vertically
  (stiffness * half-stroke / g). Derived for the TIRA shakers and the BK 4809 (their datasheets
  give no static limit); from the 4801 manual (133 N on the flexures) for the 4801/4812. The
  pre-flight check compares `safety.payload_kg` against it.
- The dynamic payload limit is not a separate setting: it is the force check
  ((moving mass + payload) * a_rms <= `force_random_rms_n`). Each shaker file has a comment
  table of payload limits for flat profiles from 20 Hz to f_max (columns: 0.001/0.01/0.05 g2/Hz,
  labelled by the 3 sigma peak-peak stroke they need), also capped by stroke for vertical mounting
  (sag + half the vibration stroke <= half-stroke). Regenerate the tables when the force rating,
  moving mass, stiffness or stroke changes, and rebuild `Shakers/shaker_comparison.pdf` in the
  Nextcloud folder with `tools/shaker_comparison.py --out ...` (same calculation;
  amplifier/cooling/mass facts that are not in the shaker files are in its `EXTRA` table).
- Static sag is not subtracted from the usable displacement in the pre-flight check.

### TIRA TV 51110 / TV 52110 + BDA 120

- The BDA 120 has voltage mode only (datasheet: "Voltage-/Current mode yes/no"). Full power is
  reached at 3.5 V peak (1 kHz sine) input, so `amp_input_full_v = 2.47` V rms.
- `[sim] amp_gain_a_per_v = 2.23` is the mid-band equivalent (5.5 A rms at 2.47 V rms) for the
  simulator's current-drive model; back-EMF damping in voltage mode is not modelled.

### BK 4809 + DAP-Audio Palladium P-2000 assumptions (not stated in the datasheets)

- The 4809 is driven by a Palladium P-2000 (an audio amplifier), no longer by the BK 2718. The
  source is the Palladium Vintage series product manual (P-400 to P-2000; use the P-2000 column).
- The P-2000 is a voltage amplifier without a current limit: 91x standard gain with the attenuator
  fully clockwise, 750 W/8 ohm or 1025 W/4 ohm per channel. The drive limits in the shaker file are
  the only protection of the 4809's 5 A rms coil rating. Use stereo mode, one channel, never
  bridged; the ~2 ohm coil is below the 4 ohm rated load, but needs only ~10 V rms / 50 W.
- Amplifier setup (the user's choice): the channel attenuator turned down to about 9.1 V/V
  (-20 dB). Into the ~2 ohm coil this gives ~4.5 A/V, so the 5 A rms rating is reached at
  1.1 V rms input (`amp_input_full_v = 1.1`, `[sim] amp_gain_a_per_v = 4.55`, a mid-band
  equivalent; back-EMF damping in voltage mode is not modelled). The knob is uncalibrated, so the
  actual gain G must be measured (1 kHz sine); then `amp_input_full_v = 10 / G`, both drive
  limits scale by the same factor, and `[sim] amp_gain_a_per_v = G / 2`. Update them together.
  With the attenuator fully clockwise the full rating would be at 0.11 V rms, and the shared
  `pretest.drive_rms_v = 0.01` would already drive ~9 % of the rating.
- Drive limits: `max_drive_v = 2.7` (peak clip, about 3 sigma of the rms limit) and
  `max_drive_rms_v = 0.88` (80 % of the 1.1 V rms rating).
- `f_min_hz = 20`: the P-2000 is specified 20 Hz - 20 kHz (+0.1/-3 dB), with a subsonic
  protection circuit at 10 Hz and a high-frequency protection that disconnects the output on
  excessive HF energy (threshold not given; untested with wideband random drive).
- The datasheets give no random-vibration rating. `force_random_rms_n = 31.5` and
  `accel_random_rms_g = 53` are derived as the 44.5 N sine peak / sqrt(2), and 31.5 N / 0.060 kg.
  Replace them if a real rating becomes available.
- Rated without forced air cooling (44.5 N, 75 g). With cooling the datasheet allows 60 N and
  about 100 g; the profile does not use those.

### BK 4801/4812 + 2707 assumptions (not stated in the manuals)

- The sources are old instruction manuals (scanned with a text layer), not datasheets; the 4812
  values are in the 4801 manual, section 9.3.2.
- Amplifier setup: OUTPUT IMPEDANCE "High" (current mode, 14 A/V), AMPLIFIER GAIN fully clockwise,
  CURRENT LIMIT at or below the 23 A rms head rating. Full output (22 A rms) is reached at
  1.57 V rms input (`amp_input_full_v`). Current mode was chosen because the simulator models a
  current-driven shaker; in "Low" (voltage) mode the 2707 is 5 V/V and the response depends on the
  coil impedance and back-EMF, so `amp_input_full_v` and `[sim]` would need revisiting.
- Drive limits: `max_drive_v = 3.5` (fits the USB-4431 +/-3.5 V AO range) and
  `max_drive_rms_v = 1.25` (80 % of 1.57 V rms).
- Random ratings derived like the BK 4809: 445 N sine peak / sqrt(2) = 315 N rms, 100 g / sqrt(2)
  = 70 g rms. `bl_n_per_a = 17.2` is 1 / head constant (58 mm/Vs).
- `f_min_hz = 5`: the 2707 gives full current only from 40 Hz (11 A at and below 5 Hz).
  `f_max_hz = 10000`: the 2707 full-output limit; the head resonance is at 7.2 kHz.

## Documents outside the repo

- Project folder (Nextcloud, synced; anything written there is shared):
  `C:\Users\rkortmann\Nextcloud\CITG-macrolab\Projecten\CSAA04 HF Shakers (Alessandro)`,
  in WSL `/mnt/c/Users/rkortmann/Nextcloud/CITG-macrolab/Projecten/CSAA04 HF Shakers (Alessandro)`.
  - `Shakers/`: shaker and amplifier datasheets/manuals (TIRA, BK 4809, Palladium P-2000, BK 4801/4812/2707),
    the RFQ template, the shaker comparison (`shaker_comparison.pdf`, .docx) and other candidates.
  - `Controller + DAQ/`: NI DAQ specifications and the DAQmx user manual, sensor datasheets
    (ADXL326, optoNCDT 1220, VR9700) and the presentation below.
  - `Ommatidia Lidar/`: Q1/Q2 lidar and VR9700 integration documents.
- Presentation for the researcher (the user's colleague in the same department; the user is the lab
  engineer supporting them): `Controller + DAQ/shaker_controller_options_draft.pptx` in that folder.
  - The user edits it by hand in PowerPoint. Always edit that file (python-pptx on a copy, then copy
    back); never regenerate it with `tools/client_options_deck.py`, which only built the first draft
    (with € prices and a client tone that were later removed) and would overwrite the edits.
  - Before writing it back, check for a `~$shaker_controller_options_draft.pptx` lock file: if it
    exists, the deck is open in PowerPoint and must be closed first.
  - Tone: colleague to colleague ("the lab", "you"). Hardware is given in € (indicative, excl.
    VAT; assumptions in the speaker notes); development work only as low / medium / high, never
    in hours or rates.
  - The 4801/4812 + 2707 runs on 230 V single-phase mains at the lab (the 4801 manual lists
    380 V three-phase for the body; the user confirmed single-phase).

## Status

- Sine sweep, stepped sine and ring-down (`src/vcactl/sine.py`) have only run in the simulator.
  They control the base (one AI channel); response-controlled and phase-resonant sine (as in
  the TRC joint-damping tests) need a second AI channel and are deferred.

- The laser displacement sensor as control sensor (commit dfadd62) was dropped on 2026-10-14 and
  its code and config removed; the loop is always closed with the accelerometer.
- Only simulated runs and `check` have been done for the BK 4809 and BK 4801/4812 setups. `pretest` and `run` on
  real hardware are still untested; start with a low `--level` (for example `-12`).
