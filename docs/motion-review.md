# Resonance excitation review

The earlier sweep split a 20-second target among sixteen independently confirmed
blocks. At 300–1500 mm/s² this produced two cycles in the first blocks. Every
block ended in `M400`, draining the planner before the host sent the next one.
Reducing the number of host exchanges shortened that pause but retained the
forced stops. Those repeated stop/start transients and short changing segments
could affect the measured spectrum; their quantitative impact requires physical
captures and repeatability checks.

The revised ramp uses eight stages with at least four complete cycles each.
All bounded strokes and acceleration commands are queued together, with one
final completion barrier. The reviewed Marlin `M204` changes travel acceleration
without synchronizing the planner. Each `G1` remains subject to the active
profile, steps, speed and acceleration limits. Whole cycles can extend the
requested duration.

Queuing the complete ramp requires cancellation at serial sending time.
OctoPrint's `sending` hook discards unsent commands belonging to cancelled or
stale sessions. The worker monitors sensor health while awaiting completion;
failure cancels remaining motion before restoring settings. Commands already
accepted by Marlin must drain through `M400`. Transport loss retains the existing
emergency-stop and durable recovery behavior.

Host markers establish only the entire sweep's completion and a conservative
distance/feedrate lower bound. They do not measure individual stage durations
or a motor excitation frequency. Analysis continues to use validated device
sample times. Millimeter strokes supply broad excitation, whose useful
frequency coverage, lack of planner starvation and reproducibility still need
hardware validation. An uninterrupted command stream alone cannot establish
uniform input energy or print quality.
