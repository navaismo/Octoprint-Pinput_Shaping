# PIS v2 events for other Marlin builds

The [patch](marlin-pis-v2.patch) adds the same M495 host-arming protocol used
by `navaismo/Marlin_bugfix_2.1_E3V3SE`, branch `c13_octoprint`. It contains no
Ender-specific configuration or LCD changes.

It targets **MarlinFirmware/Marlin, bugfix-2.1.x**, commit
[`de15c7b9d4e4485cd05ed857ef8b74bd2c4de7d0`](https://github.com/MarlinFirmware/Marlin/commit/de15c7b9d4e4485cd05ed857ef8b74bd2c4de7d0).
The event implementation and host tests come from c13 commit
[`244be9d8012cca3c4eb1efbfa084f1f9df0fa1d4`](https://github.com/navaismo/Marlin_bugfix_2.1_E3V3SE/commit/244be9d8012cca3c4eb1efbfa084f1f9df0fa1d4).
[The manifest](marlin-pis-v2.json) records the original and patched file hashes.

Older releases and other forks may have different generator, motion, serial
or FT Motion APIs. They need a source review and a port when the patch does
not apply cleanly. A matching M495 number alone is not enough. This patch
does not add native resonance generation to a release that lacks it, or add
FTM shapers to a legacy M593 build.

## Apply to your Marlin sources

Keep your printer configuration and make a separate copy of your sources.
From that copy's root, replace `/path/to/plugin` with this plugin checkout:

```bash
git apply --check /path/to/plugin/docs/patches/marlin-pis-v2.patch
git apply /path/to/plugin/docs/patches/marlin-pis-v2.patch
```

If the first command reports a conflict, stop and adapt the affected hooks
to your version. Do not apply rejected hunks blindly. Do not apply it again
to c13 or another build that already has these events.

In your own `Configuration_adv.h`, enable:

```cpp
#define EMERGENCY_PARSER
#define RESONANCE_TEST
#define PINPUT_RESONANCE_EVENTS
```

Keep the input-shaping/FT Motion configuration for your board. Marlin's
existing resonance sanity checks still apply, including the 32-bit CPU
requirement. `PINPUT_RESONANCE_PROGRESS` is optional; leave it off initially.
The patch leaves all new options disabled by default.

## What the patch adds

- `M495 Q` returns `echo:PIS:RT:READY:1`; `M495 Q2` returns `READY:2`.
  Neither query moves the printer. `M495 Q1` is not accepted.
- `M495 ... I<id> S W` prepares the test and returns to command processing at
  `ARMED_WAIT`, with no excitation or blocking serial wait.
- `M495 I<id> R` authorizes the prepared test. ID, serial port, deadline,
  cancellation, trusted axes and prepared state are checked before `ARM_ACK`.
- `HOMED`, `ARMED_WAIT`, `ARM_ACK`, `START`, `COMPLETE`, `ABORTED`, `ERROR` and
  `ARM_REJECTED` use the same `echo:PIS:RT:` framing as c13. Optional progress
  reports are best effort.
- The ARM timeout starts on entry to `WaitingArm` and lasts 30 seconds.
  Conflicting commands are rejected while the prepared state is waiting.
- `M496` cancels preparation, waiting or excitation. Successful completion
  waits for queued FT frames and pending step bits. ISR paths only change
  state; serial reporting runs from idle.

The patch preserves uninstrumented M495 and v1 sessions without `W`. It
does not change the resonance equations, FT Motion internals, shaper
coefficients or the plugin's software excitation backend.

## Check your build

Run the included actual-source host tests:

```bash
python3 buildroot/tests/resonance_host_events/test_protocol.py
```

They compile six variants: events disabled, standard motion, FT Motion,
optional progress, multiple serial ports and standard motion in an FTM build.
The tests cover timeout, cancellation, failed preparation, ID/port mismatches,
duplicate ARM, stale events and reentry during serial reporting.

Compile your normal PlatformIO environment after enabling the options. Once
you have installed that firmware yourself, use the plugin's two detection
buttons. Native calibration needs both compatible shaping firmware and a
`READY:2` response. `ok` or `READY:1` does not enable it.

Validation here checks application to the exact upstream revision, the
patched host harness and preservation of the generator equations. It does
not certify another board configuration or physical printer motion. The
Marlin sources and added event files retain their GPL notices.
