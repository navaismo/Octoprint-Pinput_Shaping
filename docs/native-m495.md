# Native Marlin M495 calibration

This implementation adds an optional excitation backend to `plugin_optimization`.
It changes only this OctoPrint plugin. It neither builds nor modifies Marlin,
the LIS2DW firmware, or the installed sensor wrappers. Software excitation is
still the default, with its existing motion equations, drivers and application
API. Selecting a backend never sends printer commands and never causes fallback.

## Architecture and changed files

| File | Responsibility |
| --- | --- |
| `native_protocol.py` | Persistent defaults, bounded parameters, numeric IDs, anchored PIS:RT parsing and lifecycle transitions |
| `native.py` | Manual capability query, native capture/motion coordination, cancellation, recovery and diagnostics |
| `native_analysis.py` | Protocol-2 metadata checks, signal-based sweep isolation, fundamental response and optimizer adapter |
| `calibration.py` | Backend dispatch, shared session ownership, cache invalidation, saved-setting checks and result/download metadata |
| `capture.py` | Existing process interface plus host timestamps and acquisition health monitoring during cancellation |
| `firmware.py` | Permit standard-motion FTM snapshots explicitly for native measurement; retain software-mode requirements |
| `inputshaping_analyzer.py` | Shared shaper fitting extracted without changing software scoring or firmware coefficients; diagnostic plotting |
| `__init__.py` | Settings, manual API dispatch and authenticated diagnostic downloads |
| `static/js/pinput_shaping.js`, both Jinja templates | Backend settings, capability gating, progress, warnings and diagnostic results |
| `tests/test_native_*.py`, API/frontend tests | Simulated protocol, failures, recovery, synthetic analysis and UI checks |

The serial callback timestamps and enqueues lines. Process management, waits,
filesystem operations, signal analysis and graph generation run in the existing
single calibration worker. No private OctoPrint transport is used.

## Settings and API

All settings use the existing OctoPrint persistence mechanism. Existing saved
settings and explicitly selected LIS2DW rates are retained.

| Setting | Default | Meaning and limits |
| --- | --- | --- |
| `excitationBackend` | `software` | `software` or `marlin_m495` |
| `nativeStart` | 20 | Start frequency F, 5–199.99 Hz |
| `nativeEnd` | 60 | End frequency T, greater than F, at most 200 Hz |
| `nativeOctave` | 20 | O, 20–60 seconds per octave |
| `nativeAccel` | 40 | Positive acceleration per Hz A, conservative Q16 bound |
| `nativePrepareTimeout` | 180 | Homing/positioning ARMED_WAIT timeout, 30–240 seconds |
| `nativeMaxCapture` | 300 | Capture budget, 30–480 seconds |
| `nativeCancelTimeout` | 200 | M496/terminal/acknowledgment budget, 10–300 seconds |

F, T and O support two decimal places, matching event precision. A is sent with
four decimal places. Its conservative maximum is `32767 * 4*pi² / 8 - 1`, which
fits the reviewed fixed-point amplitude calculation for any valid existing
Marlin correction C from 1 to 8. The plugin does not assume C's factory value,
set C, or claim that this numeric bound is a mechanical acceleration limit.
Marlin's rejection remains authoritative. Use parameters validated for the
actual machine.

The nominal sweep duration is `D = O * log2(T/F)`. For the defaults it is
31.699 seconds. Capture budgeting requires
`25 + 1.15*D + 5 <= max_capture`. Disk space is checked before
capture; processing retains the existing 256 MiB / 2,000,000-sample limits.
Longer settings require verifying the complete sensor/USB setup under load.

`lis2dwRate` remains 800 Hz by default, with 1600 Hz an explicit choice. Each
capture uses the frozen session setting. ADXL USB and SPI use `adxlRate`,
selectable at 800, 1600 or 3200 Hz with the previous 3200 Hz default. No wrapper duration is imposed: acquisition is
stopped through the existing Q/EOF interface, subject to the capture deadline.
`lis2dwRange` selects 2, 4, 8 or 16 g in the same accelerometer settings group.
New configurations use 16 g. Settings migration retains the former 2 g range
for existing saved configurations without this field; explicit selections
are preserved. Direct legacy session settings without the field use 2 g.
The wrapper is checked through `-h` without opening the sensor. A range-aware
wrapper receives `-r <range>` and must report the matching verified READY
range/scale before capture readiness; an older wrapper is accepted only at 2 g
without `-r`. Nondefault ranges fail with upgrade instructions before M495
when the wrapper lacks range support. No automatic acquisition retries occur.
This feature does not install or update the wrapper or sensor firmware.

The new Simple API command is `detect_m495`, mapped to `detect_native`. It
requires authentication and printer CONTROL permission. Status exposes
`native_capability` and `diagnostics`. Existing resonance/application commands
remain available. The frontend additionally sends the displayed `backend` and
`native_settings` with a resonance request; mismatches with saved settings are
rejected before commands. These fields do not override persistent settings.
Explicit native recovery accepts `native_idle_confirmed: true` only when the
operator has verified the same printer is stopped or reset.

## Exact protocol and detection

**Detect firmware** remains manual and separate from **Detect Native Marlin M495**.
Startup, connection, status refresh and backend selection do not probe. The
native detector sends read-only `M495 Q`, then `M495 Q2` after a v1 response,
each bracketed by separate unique `M118` query fences. Responses before the
opening fence cannot establish support. `M495 Q` retains READY:1 for old clients;
only an explicit READY:2 reply to Q2 enables the host-armed backend. A v1 Q2
rejection records `legacy` with an explanation and disables native calibration;
software excitation and explicit recovery of old native journals remain
available. Detection reports unknown, detecting, supported, legacy, unsupported,
incompatible version, timeout or error. Each query is bounded to ten seconds
and the communication hold is released on interruption. Session events cannot
establish capability.

The first query is literally `M495 Q`, not `M495 Q1`. `Q1` is rejected by the
reviewed firmware. See the [UML sequence in the README](../README.md#m495-communication)
and the [portable upstream patch](patches/README.md) when using another fork.

Both detection buttons are available in the tab and Settings. They first save
only this plugin's displayed editable settings, and do not probe if that save
fails. A completed manual detection persists `detectedFirmware` or
`detectedNative`; confirmed v2 also selects and saves `excitationBackend` as
`marlin_m495`, notifying all connected frontends. Persisted reports are
informational and cannot authorize calibration after restarting/reconnecting.

Reconnect, changed port/baud and changed firmware identification invalidate
the capability. There are no automatic retries.

Instrumented example:

```gcode
M495 X F20.00 T60.00 O20.00 A40.0000 I12345678 S W
# After matching ARMED_WAIT and verified acquisition:
M495 I12345678 R
```

X and Y are supported separately. A cryptographically generated nonzero uint32
firmware ID is independent of the plugin UUID. IDs are canonical decimal,
without signs, padding or alternate numeric forms. Matching uses full anchored
lines, retaining the `echo:` prefix:

```text
echo:PIS:RT:READY:1
echo:PIS:RT:READY:2
echo:PIS:RT:HOMED:12345678:120000
echo:PIS:RT:ARMED_WAIT:12345678
echo:PIS:RT:ARM_ACK:12345678
echo:PIS:RT:ARM_REJECTED:12345678:NOT_WAITING
echo:PIS:RT:START:12345678:X:20.00:60.00
echo:PIS:RT:PROGRESS:12345678:35.00
echo:PIS:RT:COMPLETE:12345678
echo:PIS:RT:ABORTED:12345678
echo:PIS:RT:ERROR:12345678:MOTION_FAILED
```

HOMED precedes ARMED_WAIT. ARM_ACK must follow the host's ARM request; a
repeated ACK is diagnostic and never reauthorizes motion. ARM_REJECTED is a
nonterminal command response, but aborts the host measurement. START requires
accepted ARM_ACK, must match the requested axis/range and occur once. PROGRESS is optional,
must follow START, and must remain monotonic and within the requested range.
COMPLETE requires START. Duplicate or out-of-order matching events invalidate
measurement. ABORTED and ERROR may precede START. ERROR ID 0 is accepted as a
request rejection, never as successful motion. Other IDs, ordinary M118, `ok`
and `busy` do not satisfy lifecycle transitions.

## Native state machine and capture

The operation reserves communication, validates saved settings/profile, reads
a live snapshot, checks distinct printer/sensor ports and storage, and validates
the wrapper launch configuration without opening acquisition. Native mode has
no automatic acquisition precheck. The separate explicit Sensor values action
remains available. Both FTM and standard-motion snapshots are permitted;
software measurements retain their existing FTM-active requirement.

The sequence is configuration checks → journal → M495 with S W → homing and
positioning with no acquisition → HOMED / ARMED_WAIT → capture startup and
sample verification → ARM → ARM_ACK → START → measuring → COMPLETE → 0.5-second
tail → stop and validate capture → verify settings → analyze and publish.
Cancellation or failure can branch to verified cleanup or recovery at any point.

The firmware enters WaitingArm only after native preparation and initialization,
keeps the generator inactive, and returns to normal command processing. Its
30-second arming timeout starts at that transition, independently of homing
length. The dispatcher rejects conflicting movement/configuration while
waiting; status, transport, cancellation and emergency controls remain usable.
ARM validates ID, source port, deadline, cancellation, running/trusted-axis
state and prepared position/buffers before ACK. Direct internal UI/HAL actions
are not a physical interlock; exclusive operator control remains required.

After matching ARMED_WAIT, the worker starts the wrapper and requires a live
process and complete finite CSV samples with strictly increasing timestamps.
Two consecutive bounded-tail observations must show both file growth and a
newer last timestamp. Readiness has a two-second verification limit inside a
25-second total sensor-start/authorization budget. Health is checked again
immediately before queuing the single session-specific ARM; queued terminal
or error events are processed first. No ARM or start retries are scheduled.

Once ARM is queued/sent, sensor failure always requests M496 and waits for a
terminal plus queue fence, including failure before ACK or START reception.
The host cannot infer inactivity from an absent START. A late ARM is rejected
by the firmware even if idle has not yet checked its deadline. Missing ARM or
a disconnect before authorization cannot start excitation; after accepted ARM,
UART cannot establish instantaneous host disconnection or stopped movement.

Marlin owns homing, positioning and excitation. The plugin sends no G1
excitation, G28, M204/M205 edits, or shaping-disable commands. Sensor health,
connection, session ownership and state-specific deadlines remain monitored.
The actual capture budget begins at sensor launch, separately from preparation.
Successful Q/EOF exit and metadata are required; no missing initial samples
are deliberately tolerated. The handshake does not require a LIS wrapper or
sensor-firmware update.

Only matching COMPLETE proves successful native motion. `ok`, M400, estimated
duration and missing busy messages never replace it. A final M118 fence drains
the command queue and checks for late duplicate events. M400 may be used for
settings snapshots before the command or after verified stop, not to establish
native motion completion.

Progress without PROGRESS events is labelled as an elapsed-time estimate;
with PROGRESS it is labelled firmware nominal frequency. Both remain below
100% until COMPLETE and capture validation. Neither is measured motor frequency
or proof that a buffered FT frame has physically executed.

## Cancellation, connection loss and recovery

Normal native cancellation sends M496 through public `Printer.commands`, with
recovery tags and `force=True`. Force ensures normal enqueueing; it does not
bypass OctoPrint's serial flow control, checksums, acknowledgment queue or a
pending synchronous homing command. Cancellation during preparation can wait
until that preparation returns. It is not an immediate emergency stop.

The worker continues reading events and monitoring acquisition, invalidating
or closing the sensor if it fails or reaches its capture budget. Matching
ABORTED or a legitimate preceding COMPLETE confirms termination. Reviewed
protocol errors that explicitly mean rejected preparation or stopped motion
are failures, followed by a cancellation fence; TEST_BUSY is not stop proof.
The unique M118 fence after M496 also proves its queued handler/acknowledgment
has drained before another session. Cancellation never publishes a successful
measurement, including a race won by COMPLETE. No ordinary native cancellation
sends M410 or M112. The separate explicit emergency control remains available.

Native mode verifies the settings it did not modify instead of overwriting
unrelated settings or native M495 configuration. A changed snapshot prevents
cleanup. Disconnect, uncertain stop, canceled command without a verified fence,
or settings mismatch preserves `recovery_required.json`, invalidates results
and blocks further calibration. Outside a session, unresolved native state also
blocks foreign mutating commands while allowing the M110 serial reconnection
handshake. A disconnect does not prove motion stopped.
There is no automatic print resume or calibration restart.

**Restore saved settings** performs explicit native recovery: manually requery
support, send M496, drain its queue, and verify the saved profile and settings.
It does not blindly restore fields this backend never changed. A surviving
session's terminal event is accepted as stop proof for cleanup only. If a reset
erased the old ID/terminal, recovery additionally requires the operator's
explicit stopped/reset confirmation. A fenced M496 alone is not treated as
an observed ABORTED. Changed settings must be reviewed and reconciled; recovery
does not silently rewrite them. Coordinate modes are prepared automatically
with the next axis check or calibration when necessary.

## Signal alignment and native response

Host monotonic capture launch/readiness, stop request/exit and PIS:RT receipt
times are recorded. LIS2DW protocol-2 metadata must indicate valid acquisition,
the selected requested rate, matching CSV sample count, effective rate consistent
with timestamps, g units and `rp2040_fifo_poll` timing. Range-aware metadata
must match the requested range in both `requested_range_g` and `range_g`, with
16384, 8192, 4096 or 2048 `counts_per_g` for 2, 4, 8 or 16 g respectively.
Only the legacy fixed-2-g path permits omitted range fields, with an explicit
16384 counts/g scale. Missing or contradictory negotiated scale metadata is
rejected. CSV values are already in g and are never converted a second time.
Actual range/scale appear in analysis quality metadata and session logs. Excessive
observation windows, bad CSV clocks, gaps, insufficient sampling, clipping and
stale data are rejected. The shared rate gate remains at least 2.5 times the
analysis end frequency. Device capture duration is also checked against the
observed readiness-to-stop interval when those host measurements are present.
This compares durations, not absolute time origins.

There is no subtraction of host event times from CSV timestamps and no claim
of synchronized clocks. Instead, a one-second STFT tracks the known curve
`f(t) = F * 2**(t/O)` in the sensor's own timebase. The bounded search covers
possible offsets at 50 ms increments and duration scales 0.98–1.02. That grid
is a search resolution, not timing accuracy. The reported boundary uncertainty
is at least half a spectral window and includes near-optimal offset spread.
Uncertainty above 1.5 seconds, competing intervals, poor endpoint support or
less than 85% fundamental coverage fail validation. At least 70% support is
required near each end. Fundamental power must exceed six times the larger
of the local spectral median and persistent per-frequency median. Persistent
background lines and a harmonic covering only the upper sweep cannot establish
coverage. Homing/positioning samples are excluded from the response and plotted
time signal; uncertain edge windows are excluded from fitting.

The reviewed Marlin generator uses displacement proportional to
`(C*A/f) * shape(phase)`. For fixed C and A, its fundamental acceleration is
proportional to f under the slowly varying sweep approximation. Consequently,
raw output power is not a mechanical response. The adapter divides tracked,
background-subtracted fundamental output power by a unit-gain f-proportional
chirp processed through the identical STFT. This implements the f² input-power
normalization and corrects finite-window chirp attenuation. The unknown constant
C, acceleration gain and sensor gain cancel in relative normalization. The
result is **relative fundamental sensitivity power**, not raw output PSD or a
calibrated absolute transfer function. Harmonics are not folded into this
fundamental response. Nonlinear mechanics, variable gain, strong group delay,
missed steps or an unsuitable excitation profile can invalidate these assumptions.

An interior peak must exceed the normalized baseline by at least three and pass
absolute/relative prominence gates. Candidate frequency, relative power and a
window/alignment-based frequency uncertainty are recorded. Multiple peaks are
retained. A dominant boundary response requests a wider measurement. A valid
flat response remains viewable as diagnostics with no shapers or apply action.
Weak, ambiguous or incomplete tracking produces failure diagnostics without
fabricated graphs.

The explicit optimizer adapter treats this response R as a band-limited
vibration sensitivity, integrating `R(f)*abs(H(f))² / integral(R(f))` over the
validated measured frequency band with a flat input-power weighting. It uses
the existing damping variation, firmware shaper coefficients and displacement
smoothing penalty. Search stays inside the trusted band at or above 20 Hz.
Predicted residual is relative sensitivity in that band; it is not a measured
whole-machine vibration reduction. The displayed acceleration ceiling limits
theoretical smoothing estimates and does not set native A. No recommendation is
applied automatically. If FTM is currently disabled (M493 S0), applying axis
settings does not enable it; the result warns about that separate operator step.

## Files and output

Each `runs/<plugin-uuid>/` retains configuration and, when created,
raw capture CSV/log/metadata, `protocol_events.json`,
`excitation.json`, `analysis.json`, `result.json`, `signal.png` and `psd.png`.
The native diagnostic records IDs, command/parameters, nominal and observed
timing, clock assumptions, sensor configuration, effective rate/quality,
terminal/error, capture stop state and cleanup/recovery outcome. Diagnostics
also retain HOMED (firmware uptime and host receipt), ARMED_WAIT receipt,
sample-readiness evidence, ARM queued/sent/ACK times, START/COMPLETE receipts,
actual capture duration, and rate/metadata. Clock domains are explicitly separate. Sensor output
is not copied per sample into the OctoPrint system log. Failed runs expose
available diagnostic downloads without requiring a successful result.
Downloads use fixed filenames, UUID validation, authentication and STATUS
permission. No arbitrary filesystem path is accepted.

PNG and Plotly results show the isolated signal and relative fundamental
response with predicted shaper curves. Software results retain PSD labels.
Incomplete captures never enable application; a diagnostic-only native result
is also rejected by the application API even if a caller supplies a shaper.

Analysis completion is also logged with the plugin UUID, native firmware ID,
resonance, recommendation validity, recommended shaper/frequency and sample
quality. Every fitted candidate logs frequency, predicted residual, duration,
acceleration estimate and its axis command. Warnings and diagnostic-only
outcomes are logged explicitly, without applying settings automatically.

### Command visibility and preparation clipping

`Pinput_Shaping.log` records the complete M495/M496 command as `native QUEUED`
when submitted to OctoPrint and `native TX` when the sent hook reports it.
These are separate facts: enqueueing does not prove transmission or execution.
The plugin UUID, numeric firmware ID and monotonic sent timestamp accompany
the records. `native RX` includes the complete PIS:RT line, including READY,
HOMED, ARMED_WAIT, ARM_ACK, START, COMPLETE, ABORTED and ERROR. A verified cancellation also logs its
terminal result and receipt of the queued M496 fence. These records do not
depend on OctoPrint Terminal's display filters. Serial hooks only enqueue
diagnostic notifications; the worker performs the logging.

`excitation.json` additionally retains queued/sent command records, the failure
stage/time, whether START had been observed when failure was detected, and
START/terminal timestamps received during cancellation. Sensor process errors
include bounded `Device error:` details from the wrapper log, such as CLIPPING
or BUFFER_OVERFLOW, rather than showing only exit code 2.

The LIS2DW firmware invalidates acquisition on CLIPPING. With v2, acquisition
is absent during native homing/positioning and begins only inside the prepared
barrier. Cropping cannot salvage invalid captures. Launching acquisition on
START is not supported because serial/process startup could lose initial sweep
samples. Select a wider verified sensor range when excitation clips at ±2 g;
the plugin does not alter native motion equations to compensate for clipping.

## Hardware walkthrough

1. Open **Settings → Pinput Shaping** with the correct printer connected and idle.
   Confirm its profile, sensor mounting and distinct sensor port. Use an already
   verified protocol-2 LIS2DW wrapper; start with **800 Hz** and choose the
   accelerometer range. Verify range-aware wrapper/firmware support for ±16 g.
2. Click **Detect firmware**, then **Detect Native Marlin M495**. Both are manual,
   non-moving operations. Verify supported PIS:RT v2 for this connection; v1 disables native calibration.
3. Select **Native Marlin (M495)**. For the previously validated machine,
   configure X, F20, T60, O20, A40 and conservative preparation/capture/cancel
   budgets. These settings are not universal mechanical limits. Save settings.
4. Use **Sensor values**. Axis checks and Calibrate X/Y prepare coordinate
   modes automatically inside the worker before snapshots or motion. Secure the sensor
   on the component moving with the tested axis and clear all native homing/
   positioning travel; the software position grid does not set M495's position.
5. Select **Calibrate X** and confirm. Observe no capture during preparation, followed by
   ARMED_WAIT, verified capture readiness, ARM/ARM_ACK and START. `ok` may precede
   COMPLETE. The protocol download preserves both events and their numeric ID.
6. Verify COMPLETE, successful sensor stop, valid metadata, useful alignment
   uncertainty/coverage and an interior response peak. Review the relative
   response graph and warnings. Rejected data requires investigation, not lower
   thresholds. Repeat for consistency before using a recommendation.
7. Select a fitted shaper and use **Apply and verify** manually. The backend
   reads fresh settings, validates the saved measurement and verifies the
   axis-specific command. EEPROM is not written. Validate print quality before
   any separate manual EEPROM save. Reposition the sensor appropriately for Y.
8. Separately supervise cancellation during excitation and during preparation:
   expect M496, matching ABORTED or a preceding COMPLETE, and the queued fence.
   A disconnect invalidates measurement; verify physical stop/reset before
   explicit recovery. Do not start another sweep while recovery is pending.

## Validation and remaining limits

Run `python -m pytest -q`, `node tests/test_frontend.js`, and the live C++
coefficient contract with `PINPUT_FIRMWARE_ROOT` pointing at the read-only c13
checkout. Tests cover strict framing/IDs, capability outcomes, cache/persistence,
delayed START and early ok, absent PROGRESS, queue-drained cancellation,
disconnect/recovery, capture faults/metadata/counts, synthetic resonances,
harmonics, stationary interference, multiple peaks, clipping, clock errors,
ambiguous/incomplete sweeps and the unchanged software regressions.

The final v2 offline run on 2026-11-05 passed **307 pytest tests**, including the
live c13 C++ coefficient comparisons with the firmware checkout mounted read
only. It used the local OctoPrint container with Python 3.10 and the project's
scientific dependencies. The JavaScript checks passed with both the offline
interfaces and OctoPrint's installed Knockout library. Source distribution
generation, inclusion of the new modules/documentation, Jinja template syntax
and `git diff --check` passed. The 14 warnings came from Matplotlib/Pyparsing
deprecations. Remote CI and the Python 3.9/3.11/3.12 matrix were not run here.
The v2 suite adds arming order, slow/failed sensor startup, two-observation
readiness, invalid timestamps, timeout before ARM, cancellation before START,
sensor failure after ARM before ACK/START, duplicate ACK, v1 recovery, and stale
queued ARM rejection. Firmware tests additionally exercise source ports,
conflicting command rejection, timeout wraparound, and cancellation during
ACK/START emission.

The production Marlin lifecycle harness passed all six feature/FTM/progress/
serial-port combinations. The current STM32F103RET6_creality build compiled
successfully. A separate baseline with the same configuration and Belaya 8.c
version, reverting only the six v2 production files, used 28,460 bytes RAM and
253,268 bytes flash. The host-armed build uses 28,468 bytes RAM and 254,652 bytes
flash: **+8 bytes static RAM, +1,384 bytes flash**. These are static linker sizes,
not peak stack measurements. The existing FTM_SHAPER_E warning remains.
The dedicated config-08.ini CI runner was added but not run; it replaces
working configurations. No physical printer was exercised.

The source archive is `dist/OctoPrint-Pinput_Shaping-0.0.5.tar.gz`. The release
version is retained, so explicitly reinstall this archive and restart OctoPrint;
the version number alone cannot identify these uncommitted changes. No commit,
push, merge, rebase, pull request, installed-wrapper update or firmware flash
was performed.

Automated tests do not exercise a physical printer or
prove this analysis on a Raspberry Pi under USB/CPU load. Native waveform
assumptions, homing exclusion, boundary uncertainty, peak repeatability and
actual suppression require the supervised walkthrough above. Long high-rate
captures have greater memory/storage cost. There is no general calibrated
force/transfer-function measurement or physical position feedback.

Reference: the local c13 `docs/resonance-host-events.md`,
`resonance_generator.cpp` and `resonance_host_events.cpp` define this custom
protocol and trajectory. This is not an upstream-Marlin capability assumption.
Klipper's [resonance tester](https://github.com/Klipper3d/klipper/blob/master/klippy/extras/resonance_tester.py)
and [shaper calibration](https://github.com/Klipper3d/klipper/blob/master/klippy/extras/shaper_calibrate.py)
were background references; the native adapter follows this Marlin waveform
and retains this plugin's c13 coefficient contract.

## Detection, coordinate and logging follow-up (2026-11-05)

The follow-up passed 323 pytest checks including the live c13 coefficient
contract, plus frontend checks with both offline interfaces and OctoPrint's
installed Knockout. Regression checks cover automatic X/Y preparation,
preparation failure before movement, detection persistence and save failures,
native backend selection, delayed settings saves and diagnostic-only logs.
Only plugin files changed; no firmware rebuild, flash or physical motion was
performed for this follow-up.

## Calibration workspace

The main tab follows the setup and results references in `ui_screens/`.
Cards inherit the active OctoPrint theme, with firmware, native capability,
sensor and backend summaries. Section headings and status labels use plain
text. The connection panel and editable Test Configuration block are
omitted; test parameters remain in plugin Settings. The existing rounded
settings layout, accelerometer choices, range/rate selectors and detection
with automatic saving are retained.

Setup groups sensor values, manual detection, bounded axis checks,
cancellation and emergency stop. The numbered XY grid and vertical Z slider
remain available for software excitation. Native M495 disables those controls
and explicitly states that firmware owns XY/Z positioning. Native requests
still omit host-selected coordinates. Worker stages drive the progress display;
error cleanup cannot mark unperformed excitation phases complete.

Completed analysis opens a separate results view. Operators can return to
setup and reopen the same result without changing its shaper selection.
Results contain the measured resonance, recommendation, actual sample rate
and reported sensor range, quality details, candidate resonances, comparison
table, saved-command preview and application controls. Selecting a fitted
shaper in the table or selector retains the existing server-side validation.
Diagnostic-only results remain visible with application disabled.

Protocol history is loaded from the authenticated `protocol_events.json`
artifact. Displayed times are relative host receipt times from that file;
START means native excitation started, and COMPLETE means native movement
finished, not that analysis finished. No firmware or sensor-clock timestamps
are synthesized. Downloads from stale sessions are ignored. Missing history
leaves the original diagnostic download available.

Analysis tabs provide Overview, Time Signal, Frequency Response and
Diagnostics, with keyboard navigation, optional fullscreen and raw CSV export.
Interactive Plotly charts follow the host background and text colors,
including theme changes after loading. PNG fallback and individual PNG
downloads remain available without Plotly. Request and worker
errors open a dismissible popup. Repeated status polls do not reopen the same
dismissed error; its details remain accessible below Test Progress until the
error clears, or next to the application controls for an application error.
Stage names use blue/green accents; diagnostic session IDs and command
previews use a readable blue accent instead of error-red styling.

Theme checks cover native light/dark and custom host colors, stylesheet
changes and redraw-loop prevention. Firefox previews verify these themes and
failed-session diagnostics. A Chrome check switches the host theme after chart
rendering and verifies chart text and the selected shaper remain correct.

Frontend checks cover view navigation, saved settings and shaper gating,
error notification lifecycle, progress phases, chart tabs, protocol loading
and stale responses, using both offline interfaces and real OctoPrint Knockout.
The complete offline suite passed 416 pytest checks, including the live c13
shaping contract. Firefox previews exercise the actual Knockout templates
at desktop, narrow and mobile widths, including software positioning,
diagnostic-only results and operation without Plotly. No physical printer,
firmware, capture synchronization or analysis equations are changed by this
presentation update.
