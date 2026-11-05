# Firmware Compatibility and Calibration Contract

## Runtime compatibility

Use Python 3.9–3.12 with the scientific versions declared in `setup.py`.
Newer Python versions need a separately reviewed dependency update; the plugin
does not advertise them as supported merely because its source parses there.

## Review validation

The November upstream review updates MZV and the stored C++ contract locally,
without a commit or push. All 144 combinations were captured from the current
c13 firmware; 18 MZV cases differ from the previous fixture. The remaining
seven shapers retain their previous numerical results. The host harness now
provides upstream's `_MAX` helper for the 2HEI zero-tolerance guard. The plugin
already limits tolerance to at least 0.001, matching that guard.

The updated local review passed all 122 pytest checks against c13 and all three
contract checks against c14, including the live 144-case C++ comparisons for
each firmware. The frontend checks passed too. This run used an isolated
OctoPrint 1.11.2 container with Python 3.10.18, NumPy 2.2.6 and the declared
SciPy/Matplotlib/Pandas versions. Only dependencies inside the temporary
container were installed; no printer, installer or firmware flash was used.
These results do not replace physical calibration/printing validation.


The previous review passed 122 pytest checks on Python 3.11 with OctoPrint 1.11.8,
the declared scientific dependency versions and the live c13 checkout. This
includes executing the firmware's C++ shaping functions for 144 parameter
combinations, plugin registration and command-tag integration, simulated
transport faults, recovery and numerical quality gates. Frontend state checks,
installer syntax and source-distribution packaging were also checked.
Frontend protocol gating was exercised with OctoPrint's real Knockout library
as well as the offline test interfaces.
Matplotlib emitted dependency deprecation warnings; the checks passed.

The Python 3.9/3.11/3.12 CI matrix is configured but has not run remotely for
these uncommitted changes. No installer, physical printer test, firmware flash,
commit or push was performed during this review.

The PIS:RT v2 host-arming follow-up passed 307 pytest checks, the live c13
144-case shaping comparison, all six native lifecycle C++ harness variants,
and frontend checks with both offline interfaces and OctoPrint Knockout.
The STM32F103RET6_creality build compiled successfully. Against an isolated
baseline with identical current configuration/version, v2 uses 28,468 bytes
static RAM (+8) and 254,652 bytes flash (+1,384). It leaves native equations and
software excitation unchanged. No physical printer or firmware flash was used.

## Reference firmware

This branch was checked against the local `Marlin_c13_octoprint` checkout at
`135757aea0` of `navaismo/Marlin_bugfix_2.1_E3V3SE`. The updated upstream integration also uses the same numerical contract on
`c14_octoprint` at `b012a4ace3`. Local pause/runout follow-ups do not modify
the shaping functions. The reviewed build identifies itself as Belaya 7.

The reference configuration enables FTM v2, all eight XY shapers and the
emergency parser. `FTM_FS=1000`, `FTM_MIN_SHAPE_FREQ=20` and maximum damping is
0.25. The bed is 220 × 220 × 250 mm, XY steps/mm are 80, XY acceleration limits
are 5000 mm/s² and serial baud rate is 115200. These are reference values:
calibration uses the active printer profile and live reports rather than
substituting factory defaults for missing runtime settings.

That original coefficient review had `RESONANCE_TEST` disabled. The current
c13 checkout implements instrumented M495 with backward-compatible PIS:RT v1
events and an explicitly negotiated v2 host-arming barrier. Native calibration
requires v2; v1 detection and recovery remain supported.
The optional [native M495 backend](native-m495.md) uses that explicit runtime
contract only after manual capability detection. Software excitation remains
the default; M495/M496 are not sent on connection or by software calibration.

## Runtime protocol

Firmware detection runs only when the operator requests **Detect firmware**
with the printer connected and idle. Startup, connection and idle events never
start probes, and failed or interrupted detection never schedules a retry.
Connection scripts and external commands take precedence over detection;
the operator can request another check when the printer is idle again.
The tab exposes detection state, protocol and permitted shaper candidates.
Disconnects clear the cached protocol and result, requiring a new request.
No detection probe homes, moves, changes coordinate modes or writes settings.
Explicit calibration, application and recovery operations still read fresh
settings as part of their validation and restoration; cached detection cannot
replace those checks.

Before modifying settings, the worker reads `M503`, `M220` and `M114`, then
checks `M493 V` and `M494` when FTM is present. Reports must contain complete
axis settings and motion limits. Software calibration requires FTM to be
active, with static shaping and a trapezoidal trajectory without additional
smoothing. Native calibration also permits FTM's standard-motion mode (S0),
while preserving the reviewed static shaping/trajectory requirements. It does
not activate FTM when applying an axis configuration measured in S0.

| Shaper | FTM `C` ID | Impulses |
| --- | --- | --- |
| ZV | 1 | 2 |
| ZVD | 2 | 3 |
| ZVDD | 3 | 4 |
| ZVDDD | 4 | 5 |
| EI | 5 | 3 |
| 2HUMP_EI | 6 | 4 |
| 3HUMP_EI | 7 | 5 |
| MZV | 8 | 3 |

Examples: `M493 X C1 A50 I0.05` and `M493 Y C7 A72 I0.05 Q0.05`.
Frequency is `A`, damping is `I` and EI vibration tolerance is `Q`.
Legacy `M593` is supported only for ZV, for example `M593 X F50 D0.05`.
No recommendation sends an unscoped frequency command or writes EEPROM.
Legacy detection permits only ZV. FTM allows manual selection among shapers
fitted in the current saved measurement, defaulting to the recommendation.
The application API accepts a session identifier and an optional shaper name;
omitting the name applies the recommendation. The backend retrieves that
candidate's saved frequency and the measurement's damping and tolerance,
rebuilds the axis-specific command and rejects arbitrary commands or parameter
overrides. Selecting a shaper without a saved fit is rejected. Every application obtains
a fresh snapshot, so a cached UI detection cannot bypass firmware validation.
Unknown, incomplete or incompatible reports leave calibration and application
disabled. Firmware names and Belaya release numbers are not detection inputs.

The analyzer matches `src/module/ft_motion/shaping.cpp`, including upstream's
corrected MZV damping factor K^0.75 for impulses spaced 0.375 periods apart,
and rounding the first tap interval to 1 ms before multiplying it. The former
c13 K-based MZV coefficients are no longer the reference. A C++ harness executes
the actual firmware functions for 144 parameter combinations; the stored
fixture records their source SHA256. Run the live comparison with:

```bash
PINPUT_FIRMWARE_ROOT=/path/to/Marlin_c13_octoprint python -m pytest -q tests/test_c13_contract.py
```

FTM v2 does not report its sampling clock or every compiled shaper capability.
The numerical contract assumes the reviewed c13 configuration. A different
build requires checking its configuration and rerunning the contract test;
an FTM version response alone cannot establish binary equivalence.

## Motion and measurement limits

The following excitation/PSD details describe the default software backend.
Native M495 uses firmware-owned motion, COMPLETE/M496 lifecycle events and a
separate frequency-aware response adapter; see [its contract](native-m495.md).

Excitation and the analysis frequency range are separate. The controller uses
an acceleration sweep derived from the original plugin: center/high/center/low
motion, excursions decreasing from 5 to 1 mm and acceleration increasing within
the configured and reported limits. Eight stages cover the ramp, with at least
four cycles per stage. Excursions
are quantized to the step grid and remain within the selected position's travel
margins. Speed is capped by the reported firmware limit. No machine limit is raised.

`freqStart` and `freqEnd` select measured PSD frequencies to inspect; they do not
command a motor oscillation frequency. New installations default to 5–132 Hz
and a 20-second target excitation (`sweepDuration`). Whole cycles can extend the
capture. Existing frequency settings are preserved. The former `freqStep` and
`bandDuration` controls no longer describe this method and are not used.

The previous tiny-segment frequency sweep assumed zero junction speed and a
precise host-side excitation clock. Real captures still completed about 0.43 s
for a nominal 1 s band after increasing the minimum segment from two to four
steps. The reviewed Marlin planner also imposes a nonzero minimum entry/exit
speed. The four-step correction did not establish a physically timed sweep;
the resulting 43–56 Hz cap was a limit of that generator, not the accelerometer
or the valid analysis range. That generator and its 5% timing gate are removed.

The complete bounded ramp is queued as one stream, with a session-matched start
marker and one final `M400`/completion marker. Acceleration stages have no
intermediate completion barriers, host waits or redundant center moves.
The worker checks the sensor while waiting for firmware completion.
Estimated motion time sizes command deadlines; the controller rejects completion faster
than the programmed distance/feedrate permits, allowing a 20 ms host timing
margin. It does not infer frequency accuracy from those completion times.
`excitation.json` records the complete sweep's estimated/minimum/observed
duration and validity, with planned acceleration, amplitude, feedrate, cycles
and estimated/minimum duration for each stage under `blocks`. Per-stage dispatch
times cannot establish physical completion and are not reported as such. Transport
failures still invalidate the session and preserve recovery behavior.

Millimeter-scale reversals provide broad excitation, as in the original plugin;
they do not prove equal input energy across the entire selected analysis range.
Physical response, useful frequency coverage and repeatability require validation.

Analysis rejects invalid clocks, gaps, insufficient sampling, obvious clipping,
flat captures, weak spectral peaks and boundary peaks. Combined XYZ PSDs
reduce sensitivity to sensor orientation. All candidates share the same raw
reference spectrum. Fitting uses the complete measured band, damping
variation and a displacement-based smoothing penalty. Reported acceleration
limits estimate smoothing and are capped by the test ceiling; they do not
measure motor torque or prove print quality.

## Session ownership and recovery

Only one operation owns calibration at a time. Hooks enqueue serial lines;
process startup, waits, analysis and file operations run in a worker. Each
confirmation includes a unique session marker. Foreign commands that could
change motion or settings are blocked during calibration. Status queries, automatic
reports (`M154`, `M155`, `M113`), display/progress updates (`M117`, `M73`), completion
barriers (`M400`) and `M112` remain available. A blocked command is logged with
its session, stage and tags, and its G-code appears in the cancellation message. This is host-side ownership, not a physical machine lock: LCD or
independent host actions must not change the printer during a test.

Prepare coordinate modes explicitly before motion. The preparation action sets
millimeters, absolute positioning, absolute extrusion and a 1500 mm/min feed
rate without commanding an axis displacement. Later tests restore the modes
and feed rate observed before their session. Unknown modes after reconnect
require preparation again.

A durable `recovery_required.json` is written before changing firmware state.
Shaping, acceleration, advanced motion settings and speed percentage are
restored and reread before analysis. Normal restoration only rewrites fields
changed by the test, preserving the internal precision of untouched parameters.
Rollback after a failed application and explicit recovery restore latent FTM
parameters with EI enabled before reselecting the saved compensator, including
NONE. This avoids sending frequency, damping or tolerance to a firmware mode
that rejects those parameters. Full recovery also restores the reported steps
and motion limits. Backups are limited to Marlin's report precision; finer
internal values cannot be recovered after a reset or a parameter overwrite.

The `sending` hook rejects owned motion and settings commands after cancellation
or when their session is stale, even if OctoPrint queued them earlier. Recovery
commands and completion barriers remain available. Cancellation drains motion
already accepted by the firmware before restoring settings. Lost
confirmation after mutation, an unverifiable restoration or a competing print
can trigger `M112`; reset/reconnect and explicit recovery are then required.
A disconnect or process termination preserves the journal. Recovery verifies
the profile and protocol, but the operator must confirm that the same physical
printer is connected. Results cannot be applied across a reconnect or after
firmware settings have changed.

These rollback/emergency rules describe software measurements and setting
application. Native excitation does not modify shaping/motion settings, uses
M496 for ordinary cancellation, and verifies stop before settings reads. Lost
communication does not prove stopped motion. Its durable journal requires
explicit recovery; no M112 is emitted automatically for a native stop timeout.
Recovery verifies untouched settings rather than blindly restoring them.

## Sensor installation and remaining limitations

Copy the entire `bash/` directory before using an installer. On Raspberry Pi
OS / Debian, select the OctoPrint service account explicitly when necessary:

```bash
sudo OCTOPRINT_USER=pi bash bash/Octo-deps-install-ADXLUSB.sh
```

Installers fetch pinned upstream revisions. USB ADXL installs as `adxl345usb`,
SPI as `adxl345spi`; they no longer overwrite each other. If an older USB
installer replaced the SPI executable, reinstall the SPI wrapper before using
that sensor type. USB wrappers run as the OctoPrint user with serial-group
access; only the SPI GPIO tool uses noninteractive sudo. Installers modify the
host and were not executed during the offline test suite.

Configure a stable USB sensor device path in plugin settings, preferably
`/dev/serial/by-id/...`. It must differ from the printer serial port. A blank
path retains the upstream defaults: ADXL USB `/dev/ttyACM1`, LIS2DW
`/dev/ttyACM0`.

LIS2DW sampling is selected in plugin settings with `lis2dwRate`: 800 Hz by
default, or 1600 Hz explicitly. Both the sensor precheck and full capture use
the session's frozen selection. ADXL sampling remains 3200 Hz. No automatic
rate fallback occurs after a failed capture. The analyzer uses measured sample
times rather than the requested rate and retains its 2.5-times-end-frequency
gate; the reported effective rate near 797 Hz covers analysis up to 200 Hz.

The legacy LIS2DW wrapper pinned by the installer stamps host receipt times rounded to milliseconds.
Its sensor-value check remains usable, but high-rate resonance captures fail
timestamp validation before homing. A precise acquisition-clock protocol is
needed in the external sensor firmware/wrapper; fabricated uniform timestamps
would conceal dropped samples and are not a substitute. The plugin cannot
repair that external timing contract by itself.
The separately installed protocol 2 wrapper announces device FIFO-edge timing
and preserves the same CSV and Q/EOF interface. It remains subject to the
plugin's timestamp validation and must stay alive until the requested stop;
the readiness banner alone does not establish capture validity. This change
does not update the installer's pinned revision or flash the sensor firmware.

Run artifacts live in OctoPrint's plugin data folder under `runs/<session>/`.
Authenticated downloads require status permission. Runtime artifacts are not
written into the installed package. PNG plots work offline; optional Plotly
charts can be unavailable without affecting application of a complete result.
Runs are retained for investigation; monitor storage and remove old runs while
the plugin is idle.

The plugin writes `Pinput_Shaping.log` in OctoPrint's configured log directory
(normally `~/.octoprint/logs/`), with daily rotation and three backups. It also
keeps OctoPrint's normal logger propagation. This restores the dedicated file
that the earlier optimization refactor stopped configuring. Sensor subprocess
output remains in each run's `capture.csv.log` or `sensor_check.csv.log`.
The sensor PTY is drained in a background reader throughout capture, including
while the calibration worker waits for printer motion. Driver errors are kept
in that log; unexpected exits report the driver, exit code or signal and the
log path. A passed sensor precheck does not guarantee that the later capture
will survive motion or a connection failure.
