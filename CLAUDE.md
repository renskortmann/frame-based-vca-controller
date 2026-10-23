# CLAUDE.md

`vcactl` is a frame-based closed-loop random vibration controller (NI DAQ + shaker).
README.md covers hardware wiring, usage, the control algorithm and the code layout; read it first.
This file only records what is not obvious from the code or README.

## Working in the repo

- Setup: `python -m venv .venv && .venv/bin/pip install -e ".[plot,test]"`, then `.venv/bin/pytest`.
  All tests use the simulated backend, so no hardware is needed.
- NI-DAQmx does not work under WSL2. In WSL2 always use `--sim`. OS per DAQ (NI's OS
  compatibility table, `Controller + DAQ + accellerometers/NI/NI Hardware and Software Operating
  System Compatibility Data.xlsx` in the Nextcloud folder, and the DT9837 manual): USB-6211 and
  USB-4431 Windows only; PXIe-4468 and the cDAQ-9174 with NI 9232/9234/9260/9263 Windows and
  Linux; DT9837A Linux only (UL for Linux, `uldaq`; Data Translation's Windows driver is not
  used). No single OS runs all of them. The tests, `check` and a `--sim` run passed with Windows
  Python 3.12 (2026-10-21). Install the Windows checkout editable (`pip install -e`):
  `DEFAULT_CONFIG_DIR` is found relative to the source tree.
- Check a profile without hardware:
  `vcactl check --shaker bk4809 --daq usb6211 --profile low_g`
- Work directly in the checked-out branch; no feature branch or PR is required.
- Don't commit or push. The user reviews and commits all changes themselves.
- Datasheets and manuals are not in the repo (the old `docs/` folder is gone); they are in the
  Nextcloud project folder (see "Documents outside the repo"), which is the second folder of the
  VS Code workspace `frame-based-vca-controller.code-workspace` (git-ignored on purpose; don't add
  it). Read PDFs with `pdftotext -layout`, or `pypdf` where poppler is not installed. Shaker and
  DAQ profile and sensor values must come from them. The `docs/...` paths in the config file comments are
  the old locations; the file names are unchanged.

## Shaker, DAQ, sensor + settings configs

Four files and a test profile combine into one run, each selected separately:

- Shaker (`--shaker`, `config/shakers/*.toml`): the exciter, its amplifier gain and the drive
  limits at the amplifier input (`max_drive_v`, `max_drive_rms_v`).
- DAQ (`--daq`, `config/daq/*.toml`, default `usb6211`): device settings plus `[limits]` with the
  data-sheet capabilities that settings are validated against (ranges, allowed rates, coupling,
  IEPE and its compliance voltage, driver, AO sync method, converter filters and delay). The
  settings file can override DAQ settings in a `[daq]` table, but never `model` or `[limits]`.
  `conditioner_gain` is the gain of an external IEPE conditioner (USB-6211). CompactDAQ files
  name the chassis in `device` and the modules in `ai_device`/`ao_device`.
- Sensor (`--sensor`, `config/sensors/*.toml`, default `pcb352b`): the control accelerometer's
  calibrated sensitivity, range, frequency range, mass and IEPE current range. Settings files
  may not have a `[sensor]` table (the loader rejects it).
- Settings (`--settings`, default key `default` = `config/settings/default.toml`; `--station` is
  an old alias): control tuning, pretest and the shaker-independent safety settings (payload,
  clipping, open-loop and overload aborts).
- Test profile (`--profile`, `config/test_profiles/*.toml`, e.g. `low_g`).

Every option takes a key (`<config-dir>/<kind>/<key>.toml`, via `resolve_config_path`) or a path.
`--shaker` and `--profile` are required on purpose: no silent default for the amplifier-dependent
drive limits or for the test itself.

Anything that depends on the amplifier belongs in the shaker file, so that no settings/shaker
combination can be wrong. `validate_setup` (called by the CLI and the `Controller`) checks the
cross-file rules: shaker `max_drive_v` <= `daq.ao_range_v` (the USB-4431 AO is only +/-3.5 V) and
`pretest.drive_rms_v` <= shaker `max_drive_rms_v`. `load_shaker` checks
`max_drive_rms_v` <= `max_drive_v` and <= `amp_input_full_v`. `validate_settings` checks a
DAQ IEPE current against the sensor's IEPE range, and the sensor's highest bias
(`iepe_bias_max_v`) plus its swing at `range_g_pk` against `limits.iepe_compliance_v` (352B:
16.0 V, 356B18: 17.2 V; DT9837A 18 V, NI 9234 19 V). The pre-flight check compares the band with
the sensor's frequency range and the acceleration peak (`safety.accel_crest_factor` x rms for
random) with `range_g_pk`; the runtime monitor aborts on a measured peak above it. The payload
in all checks and in the simulator is `safety.payload_kg` + sensor `mass_kg`
(`Settings.payload_total_kg`); the chain sensitivity is `Settings.v_per_g`.

### PCB 352B (default sensor, added 2026-10-23)

- Single axis, serial 526673; calibration from the user: 972 mV/g. Datasheet `352b_c.pdf` is a
  scan without a text layer (read it as an image).
- Default "for now" (user's choice) because its band is 2-10000 Hz against the 356B18's
  0.5-3000 Hz; same +/-5 g and ~1 V/g, so it is not the less sensitive sensor below. A more
  sensitive accelerometer may become the default later.
- `iepe_bias_max_v = 11.1` is the bias the user measured, not the datasheet's 8-14 V: with 14 V,
  14 + 4.86 V fails the DT9837A's 18 V compliance check. Re-measure it for another unit.

### PCB 356B18

- Serial LW266119; calibration from the user: x 980, y 1054, z 1048 mV/g. The z axis closes the
  loop (`sensitivity_mv_per_g = 1048`); the others are only in the file's comments.
- Intended for low-frequency, low-g tests: +/-5 g, 0.5-3000 Hz (+/-5 %). The shared
  `pretest.drive_rms_v` was lowered from 0.05 to 0.01 V for it: at 0.05 V the simulated pretest
  peaks above 5 g on the BK 4809 and BK 4801/4812. The higher-level example profiles
  (`flat`, `sloped`, `wideband`) fail its range check (and the 352B's); they wait for a
  less sensitive, higher-range accelerometer, which gets its own sensor file.
- Tests mostly use `tests/data/sensor_test.toml` (10 mV/g, +/-500 g, 1-10000 Hz) so that they are
  not limited by the PCB sensors.

### DAQ device notes

- USB-6211: SAR converters without anti-aliasing, so `decimation` >= 2 and AO is clocked from
  `ai/SampleClock`.
- USB-4431 and PXIe-4468 (DSA): delta-sigma with built-in anti-aliasing; AO runs on its own clock
  and starts on `ai/StartTrigger`; AO is hardware-timed only, so `force_zero` writes a short
  finite burst of zeros. Their converter filter delay (`limits.filter_delay_at(fs_io_hz)`) is
  added to the controller's drive/response alignment and is emulated by the simulator. For the
  PXIe-4468 it depends on the rate: `filter_delay_max_rate_hz`/`filter_delay_by_rate` hold the
  datasheet table, and `filter_delay_samples` applies above its last rate.
- Rates and frame size are chosen per run by `rates.resolve_rates` (called by the CLI before the
  backend is created; the controller and both backends refuse settings that are still "auto"):
  - I/O rate: the lowest of `daq.fs_io_auto_hz` (USB-4431: `limits.fs_allowed_hz`) whose band
    covers the profile's f_hi. Band (`config.daq_band_max_hz`, `Settings.f_band_max_hz`):
    `limits.passband_fraction` x `fs_io_hz` (0.5 USB-6211, 0.453 USB-4431, 0.454 PXIe-4468)
    and, with `decimation` > 1, 0.3 x control rate (the FIR passband).
  - Decimation: 1 on the DSA devices; on the USB-6211 the largest factor from 2 to 16
    (`MAX_DECIMATION`) with a whole-number control rate. The USB-6211 keeps fs_io >= 100 kS/s
    (no anti-aliasing filter; the 352B/356B18 resonances are >= 25/20 kHz).
  - Frame size: the smallest power of two with df <= `control.line_spacing_hz` (6.25 Hz) and
    <= f_lo / 2, so the loop timing in seconds (frames per level, pretest, averaging) stays as
    tuned at every rate. This choice (lowest rate; df kept near 6 Hz) is the user's.
  - Band mismatches (profile against the shaker, the sensor, the DAQ maximum and a fixed
    frame size) are one ConfigError. Highest test frequency with the DAQ as the only limit:
    USB-6211 37.5 kHz, USB-4431 43.5 kHz, PXIe-4468 90.8 kHz.
  - Decimation 1 (now the DSA default) has run only in the simulator. Tests that need exact
    numbers pin the old fixed rates with `conftest.pin_rates`.
- The simulator discretizes the shaker model with a bilinear transform at fs_io. At low DSA
  rates, resonances above about 0.4 fs_io are pushed below Nyquist (the TV 51110's 6.5 kHz
  lands at about 2.1 kHz at 5 kS/s), whereas a real DSA filters them out.
- AI range: `ai_range_v = "auto"` (default in all DAQ files) is resolved per run by
  `safety.select_ai_range` from the expected peak at `--level` (`expected_accel_peak_g` x
  `v_per_g`, + `ai_range_margin_db`). The controller passes it to `backend.start(prefill,
  ai_range_v)` and the `RuntimeMonitor`; `daq.ai_range_v` itself may still be "auto". One range
  per run, so the pretest probes at -20 dB and lowers its drive to stay within half the range.
- The DSA profiles are untested on hardware. The AI/AO start-trigger sync, coupling and IEPE
  settings follow the datasheets and the nidaqmx API but have only run against the simulator.
- Decimation 1 needs both an AI anti-aliasing and an AO reconstruction filter
  (`limits.antialias_filter`, `ao_reconstruction_filter`); otherwise the FIR interpolates the
  drive (USB-6211, NI 9263).
- cDAQ-9174 (`cdaq9234_9260`, `cdaq9232_9263`, added 2026-10-21, simulator only):
  - NI 9234 + NI 9260: separate delta-sigma modules with their own 13.1072 MHz oscillators.
    DAQmx shares timebase/sync pulse/start trigger automatically only within one task (and AI
    and AO cannot share a task), so `ao_sync = "shared_timebase"` gives the AO task the AI
    task's `samp_clk_timebase_term` and the AI start trigger. nidaqmx 1.6 has no sync-pulse
    source property; the start trigger should do. Whether DAQmx accepts this route on the
    cDAQ-9174 is the main open point: check with `vcactl loopback` (drift must be 0 ppm, delay
    about 71 samples). Fallback: the 9232/9263 pair.
  - NI 9232 + NI 9263: AO clocked from `ai/SampleClock` (NI: the non-delta-sigma module takes the
    delta-sigma clock). The NI 9263 has no reconstruction filter, so decimation >= 2 and fs_io >=
    51.2 kS/s. NI-DAQmx picks the highest decimation for the NI 9232/9234, so its input delay is
    29/fs at 51.2 kS/s and 30/fs at 102.4 kS/s (+ about one sample for the AO).
  - Delta-sigma modules (NI 9260) do not support on-demand AO: `force_zero` writes a burst.
- DT9837A (`dt9837a`, `src/vcactl/daq/ul.py`, added 2026-10-21, tested only against a fake
  uldaq in `tests/test_ul_backend.py`):
  - **Start alignment:** uldaq has no simultaneous AI/AO start. The backend starts AI, then AO
    with a 10 mV chirp marker, finds it in the `DAQI_DAC` readback (an analog measurement of the
    AO output) and cuts the AI stream so that returned sample n belongs to AO sample n at the
    output. `filter_delay_samples` is therefore 0.
  - **AO lag:** in continuous mode uldaq keeps the 8192-sample AO FIFO and 32 USB transfers of
    10 ms full (src/usb/UsbScanTransferOut.cpp, AoUsb9837x.h). So the AO runs about 0.48 s
    behind the written data at 51.2 kS/s. `limits.ao_fifo_samples`/`ao_transfer_s` model this;
    `resolve_rates` raises `ao_queue_blocks` to cover it, and `start` checks the measured lag.
    The user chose a 51.2 kS/s floor for it (lag 0.5 s, 1.2 s at 10 kS/s).
  - **Consequences:** about 0.6 s of drive plays after an abort. The sine gain is scaled by
    4 / (queue + 2). In the sim, sweeps through resonances track less tightly (85 % within
    1 dB at 6 oct/min, 96 % at 3 oct/min).
  - **Rates:** the AI and AO PLLs are identical up to 52.734 kS/s (above it the AI uses 256 x fs),
    so `fs_max_hz = 52734`; `start` refuses differing actual rates.
  - **AO filter:** the 2-pole 10 kHz Butterworth AO filter is compensated by the loop; the band
    goes to 0.453 fs = 23.9 kHz (user's choice, option 2). The drive limits apply before the
    filter, so above 10 kHz less drive reaches the amplifier.
  - **Watchdog:** a continuous UL scan would replay old drive. A watchdog thread zeroes taken AO
    samples and stops the AO at 0 V if writes stop for `STALL_S`.
- `vcactl loopback` (`src/vcactl/loopback.py`): AO wired to a spare AI channel, amplifier
  disconnected; noise at the highest auto rate; reports the delay (start/end), the drift (ppm)
  and the gain against `filter_delay_samples`. The backends take an optional
  `loopback_channel` (second AI channel, IEPE off, DC), and `read` then returns two columns.

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
  `C:\Users\rkortmann\Nextcloud\CITG-macrolab\Projecten\CSAA04 EPICA DP (HF Shakers Alessandro)`,
  in WSL `/mnt/c/Users/rkortmann/Nextcloud/CITG-macrolab/Projecten/CSAA04 EPICA DP (HF Shakers Alessandro)`.
  - `Shakers/`: shaker and amplifier datasheets/manuals (TIRA, BK 4809, Palladium P-2000, BK 4801/4812/2707),
    the RFQ template, the shaker comparison (`shaker_comparison.pdf`, .docx) and other candidates.
  - `Controller + DAQ + accellerometers/`: `NI/` (NI DAQ and C Series specifications, the DAQmx
    user manual and the OS compatibility table), `MCC DAQ/` (the DT9837 datasheet and product
    manual), `PCB accelerometers/` (352B, 356B18, conditioners), other sensor datasheets
    (ADXL326, optoNCDT 1220, VR9700) and the presentation below.
  - `Ommatidia Lidar/`: Q1/Q2 lidar and VR9700 integration documents.
- Presentation for the researcher (the user's colleague in the same department; the user is the lab
  engineer supporting them): `Controller + DAQ + accellerometers/shaker_controller_options_draft.pptx` in that folder.
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
