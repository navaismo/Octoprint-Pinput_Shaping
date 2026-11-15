# Calibration on the printer LCD

Enable **Show calibration on the printer LCD** in plugin Settings. It is on
by default. The display is optional: firmware without our image protocol uses
standard M117 information messages instead.
No LCD plugin needs to be installed alongside Pinput Shaping.

The implementation uses the protocols from
[LCD_E3V3SE main_cleanup](https://github.com/navaismo/Octoprint-LCD_E3V3SE/tree/main_cleanup)
and [main](https://github.com/navaismo/Octoprint-LCD_E3V3SE/tree/main).
Full-screen v2 requires the optional extension implemented in Marlin's
`c13_octoprint_cleanup` branch. Sensor firmware and wrappers are unchanged.

## What appears

- **M9001 v2 with full-screen support:** the host renders the entire 240 × 320
  LCD, x=0..239, y=0..319. Each new screen clears every pixel. The octopus
  logo, title, axis, plot and data are drawn by the host, without native print
  controls, filename, temperatures, progress or a status bar. The test screen shows the selected axis, frequency
  band, sensor, range, requested sample rate and native/software backend.
  After analysis, the result card shows the measured resonance, fitted shaper
  and frequency, and the measured spectrum with the recommended predicted
  curve. Native plots are labeled relative response; software plots are PSD.
  Frequencies outside the selected band are excluded from the small graph.
  The header uses the bundled [original OctoPrint artwork](../octoprint_Pinput_Shaping/static/img/README.md),
  scaled to 64 × 51 pixels with smooth edges and its original colors. Only the
  final RGB565 panel conversion reduces color depth; there is no small fixed
  logo palette. The larger header still leaves the test details and plot readable.
- **Other firmware, including M9001 v1 and v2 without our full-screen extension:**
  the plugin sends only standard `M117` information messages after the read-only
  capability queries. It shows
  `Calibration X in progress`, then the actual resonance and recommendation.
  A safely stopped failed/cancelled test replaces the in-progress message.
  No image packets, print-card commands or legacy START/END drawing commands
  are sent. The firmware controls how its native LCD displays M117; clearing,
  wrapping and scrolling depend on that firmware and panel.

Both result displays say **NOT APPLIED**. A diagnostic-only measurement shows
no recommendation. The display never applies a shaper or saves EEPROM.
Keep using the web calibration controls. Encoder Enter dismisses the screen
and returns to MAIN; it does not cancel the calibration. Native safety dialogs
can take priority over the host screen.

## Detection and timing

Negotiation runs inside the owned calibration worker, before homing or
excitation, never on connection or on a timer. `M9001 Q` must return the exact
supported v2 capabilities, followed by `M9001 QF` returning
`M9001 CAPS FULLSCREEN 1 42 240 320 00000000 0000`.
Images are authorized only by both exact capability replies, not by the
firmware name, a v2 version number or the panel dimensions alone. Missing,
incompatible or unsupported replies select M117 text instead. Standard
`Unknown command: M9001` replies also fall back to text after the queue fence
is received. Silence does not identify a protocol version, and no legacy
START/END probe is used. A missing queue fence or lost connection still stops
calibration rather than sending further commands.
Support is checked again for the next calibration, including after reconnect.

V2 opens an exclusive canvas with a version-2 BEGIN packet whose body is `01`.
It never sends `M9000` to open the print card. The host sends bounded RGB565
rectangle batches over OctoPrint's ordinary command queue, using COLOR and
RECT16 (`03` plus four big-endian 16-bit inclusive coordinates). Packets use the
same G-free Base64, session ID, sequence and CRC32 format as the LCD plugin.
Only one batch is outstanding. Each batch needs its matching application ACK;
the final packet needs DONE. An ordinary `ok` is not drawing completion.
Commands fit numbered/checksummed 96-byte Marlin command buffers.

No M118 fences are inserted inside a live transfer: that command would
preempt the firmware drawing session. After completion or failure, the worker
aborts its session and drains the normal queue before continuing. A completed
screen remains visible; a failed transfer releases the canvas with a
session-specific `R` packet. A safely stopped failed/cancelled calibration also
releases its canvas after settings restoration. A missing
queue fence, disconnect or cancellation prevents subsequent motion. There
are no drawing retries, automatic motion retries, raw serial writes,
print starts, pause/resume operations or M876 answers.

The initial drawing finishes before the test moves. The result drawing starts
only after capture, confirmed movement completion, settings verification and
analysis. No drawing is performed inside WaitingArm or during excitation.
Native menu/modal ownership can reject or cancel a render; the plugin logs
the failure and skips the display if its queue can be safely drained.

## Diagnostics and validation

The dedicated plugin log records negotiation and confirmed/skipped updates.
V2 runs retain `lcd_info.png` and `lcd_result.png` beside the existing capture
and result artifacts. Their names are exposed in the result's `files` mapping
and use the same authenticated, filename-restricted download route as plots.

Offline tests check exact RGB565 replay and drawing bounds, packet sizes and
CRCs, matching credits, stale responses, missing DONE, busy displays,
cancellation, disconnection, failed queue drain, settings persistence and
native/software timing. These do not measure physical LCD latency or verify
full-screen coverage, panel fence behavior or physical readability during homing.
Supervised printer/LCD validation is still required.

The full-screen follow-up passed all **564 pytest tests**, including pixel-for-
pixel replay of both host screens through the actual Marlin C++ executor with
ASan/UBSan, plus the frontend checks. The only pytest warnings were existing
Matplotlib/Pyparsing deprecations. Marlin's 20 protocol tests and four original
thumbnail sender integration tests passed. Eleven of thirteen native display
regression scripts passed; `test_dwin_host_status.py` and
`test_dwin_input_shaping.py` also fail on the unchanged baseline (cancel-message
expectations and a missing commented configuration marker, respectively).

`STM32F103RET6_creality` compiled before and after this change with identical
configuration: PlatformIO reports 18,120 bytes RAM in both builds and Flash
increased from 227,320 to 227,944 bytes (**+624 bytes**). These are linked build
measurements, not runtime peak RAM measurements. No firmware was flashed and
no physical printer motion was performed.

The original-logo follow-up passed 31 LCD tests, including exact RGB565 replay
through the production firmware executor and the native arming-order checks.
A source-package check also verified that the original PNG is included unchanged.
For the example test screen, DATA packets increased from 486 to 864. The
executor's simulated LCD wire/pacing time increased from 7.0 to 13.3 seconds;
this excludes host serial transport and OctoPrint latency and is not a physical
display measurement. The transfer still finishes before acquisition or motion.
