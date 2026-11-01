# Firmware Compatibility and Calibration Contract

## Runtime compatibility

Use Python 3.9–3.12 with the scientific versions declared in `setup.py`.
Newer Python versions need a separately reviewed dependency update; the plugin
does not advertise them as supported merely because its source parses there.

## Review validation

The local review passed 122 pytest checks on Python 3.11 with OctoPrint 1.11.8,
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

## Reference firmware

This branch was checked against the local `Marlin_c13_octoprint` checkout at
`4829b53768` of `navaismo/Marlin_bugfix_2.1_E3V3SE`. Firmware files were read,
not modified or flashed. The reviewed build identifies itself as Belaya 7.

The reference configuration enables FTM v2, all eight XY shapers and the
emergency parser. `FTM_FS=1000`, `FTM_MIN_SHAPE_FREQ=20` and maximum damping is
0.25. The bed is 220 × 220 × 250 mm, XY steps/mm are 80, XY acceleration limits
are 5000 mm/s² and serial baud rate is 115200. These are reference values:
calibration uses the active printer profile and live reports rather than
substituting factory defaults for missing runtime settings.

`RESONANCE_TEST` is disabled. The plugin therefore does not send `M495` or
`M496`. FTM's native resonance generator would need a separately reviewed
firmware change and its own completion/abort protocol before adoption.

## Runtime protocol

The plugin automatically schedules a read-only detection session at startup
when connected and idle, on connection, or when returning to idle. Connection
scripts and external commands take precedence over detection; interrupted
automatic probes retry with a short delay, up to three attempts per connection.
The tab exposes detection state, protocol and permitted shaper candidates, with
a manual retry button. Disconnects clear the cached protocol and result.
No detection probe homes, moves, changes coordinate modes or writes settings.

Before modifying settings, the worker reads `M503`, `M220` and `M114`, then
checks `M493 V` and `M494` when FTM is present. Reports must contain complete
axis settings and motion limits. FTM must be active, use static shaping and
have a trapezoidal trajectory without additional smoothing.

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

The analyzer matches `src/module/ft_motion/shaping.cpp`, including c13's MZV
coefficients and rounding the first tap interval to 1 ms before multiplying
it. It does not substitute Klipper's MZV coefficients. A C++ harness executes
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

The experimental host sweep uses step-aligned triangular travel. For stroke
length `L`, acceleration `a` and frequency `f`, the model is `a = 16 L f²`.
Each stroke spans at least two microsteps. With 80 steps/mm and a configured
2500 mm/s² ceiling, the upper model limit is approximately 79 Hz. Requests
above that limit fail before homing. Raising the ceiling requires reviewing
the actual machine; the plugin never raises firmware acceleration limits.

The planner also checks excursion margins, speed, serial bandwidth and total
test duration. It compares each band's observed completion time with the
model, rejecting deviations above 5% with a 20 ms timing floor. Temperature
traffic, firmware planning and USB scheduling can still distort excitation.
Timing validation checks duration, not the complete physical acceleration
waveform. `excitation.json` preserves the planned and observed band timing.
This host sweep still requires physical validation and repeatability tests.

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
change state are blocked during calibration; status queries and `M112` remain
available. This is host-side ownership, not a physical machine lock: LCD or
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

Cancellation drains the current queued band and restores settings. Lost
confirmation after mutation, an unverifiable restoration or a competing print
can trigger `M112`; reset/reconnect and explicit recovery are then required.
A disconnect or process termination preserves the journal. Recovery verifies
the profile and protocol, but the operator must confirm that the same physical
printer is connected. Results cannot be applied across a reconnect or after
firmware settings have changed.

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

The pinned LIS2DW wrapper stamps host receipt times rounded to milliseconds.
Its sensor-value check remains usable, but high-rate resonance captures fail
timestamp validation before homing. A precise acquisition-clock protocol is
needed in the external sensor firmware/wrapper; fabricated uniform timestamps
would conceal dropped samples and are not a substitute. The plugin cannot
repair that external timing contract by itself.

Run artifacts live in OctoPrint's plugin data folder under `runs/<session>/`.
Authenticated downloads require status permission. Runtime artifacts are not
written into the installed package. PNG plots work offline; optional Plotly
charts can be unavailable without affecting application of a complete result.
Runs are retained for investigation; monitor storage and remove old runs while
the plugin is idle.
