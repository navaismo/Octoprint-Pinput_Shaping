"""Bounded, step-quantized host excitation for Cartesian Marlin printers."""

from dataclasses import dataclass
import math

from .firmware import finite_number


@dataclass(frozen=True)
class SweepBand:
    frequency: float
    acceleration: float
    low: float
    high: float
    feedrate: float
    cycles: int

    @property
    def duration(self):
        return self.cycles / self.frequency

    def commands(self, axis):
        # A reversal at each endpoint has zero junction velocity. For a
        # triangular segment of length L: t=2*sqrt(L/a), hence a=16*L*f^2.
        commands = [f"M204 T{self.acceleration:.6f}"]
        for _ in range(self.cycles):
            commands.extend((f"G1 {axis}{self.high:.5f} F{self.feedrate:.3f}",
                             f"G1 {axis}{self.low:.5f} F{self.feedrate:.3f}"))
        return commands


def bounds_from_profile(profile):
    volume = profile.get("volume", {})
    if volume.get("formFactor", "rectangular") != "rectangular":
        raise ValueError("Host calibration supports rectangular Cartesian beds only")
    if volume.get("origin", "lowerleft") != "lowerleft":
        raise ValueError("Host calibration requires a lower-left bed origin")
    limits = {}
    custom = volume.get("custom_box") or {}
    for axis, dimension in (("X", "width"), ("Y", "depth"), ("Z", "height")):
        size = finite_number(volume.get(dimension), dimension, 1)
        limits[axis] = (finite_number(custom.get(f"{axis.lower()}_min", 0), f"{axis} minimum"),
                        finite_number(custom.get(f"{axis.lower()}_max", size), f"{axis} maximum"))
        if limits[axis][0] >= limits[axis][1]:
            raise ValueError("Printer profile contains invalid travel limits")
    return limits


def validate_position(profile, axis, position, margin=5):
    if axis not in ("X", "Y"):
        raise ValueError("Axis must be X or Y")
    limits = bounds_from_profile(profile)
    checked = {}
    for key in "XYZ":
        minimum, maximum = limits[key]
        inset = margin if key in "XY" else 0
        checked[key] = finite_number(position.get(key), f"{key} position",
                                     minimum + inset, maximum - inset)
    if checked["Z"] < max(10, limits["Z"][0]):
        raise ValueError("Calibration Z height must be at least 10 mm")
    return checked


def plan_sweep(axis, center, limits, settings, snapshot, baudrate=115200):
    """Reject unachievable frequencies instead of silently changing the sweep.

    This is a model of triangular travel, not a claim of measured timing.
    The controller checks actual band durations and rejects distorted sweeps.
    """
    start = finite_number(settings["freqStart"], "start frequency", 5, 200)
    end = finite_number(settings["freqEnd"], "end frequency", start + 1, 200)
    step = finite_number(settings.get("freqStep", 2), "frequency step", 0.5, 10)
    dwell = finite_number(settings.get("bandDuration", 1), "band duration", 0.5, 3)
    minimum = finite_number(settings["accelMin"], "minimum acceleration", 1)
    maximum = finite_number(settings["accelMax"], "maximum acceleration", minimum,
                            snapshot.reports["M201"][axis])
    steps = snapshot.reports["M92"][axis]
    speed_limit = snapshot.reports["M203"][axis]
    # Two microsteps per stroke keep both endpoints on the step grid.
    max_frequency = math.sqrt(maximum * steps / 32)
    if end > max_frequency:
        raise ValueError(f"Requested {end:g} Hz exceeds the step/acceleration limit "
                         f"({max_frequency:.1f} Hz). Lower the end frequency or review acceleration limits.")
    center = math.floor(center * steps + 0.5) / steps
    frequencies = [start + i * step for i in range(int((end - start) / step) + 1)]
    if not math.isclose(frequencies[-1], end):
        frequencies.append(end)
    bands = []
    for freq in frequencies:
        requested = minimum + (maximum - minimum) * (freq - start) / (end - start)
        stroke_steps = max(2, 2 * math.floor(requested * steps / (32 * freq * freq)))
        length = stroke_steps / steps
        accel = 16 * length * freq * freq
        if accel > maximum + 1e-6:
            raise ValueError(f"Cannot generate {freq:g} Hz within the configured acceleration")
        low, high = center - length / 2, center + length / 2
        if low < limits[0] + 5 or high > limits[1] - 5:
            raise ValueError("Sweep excursion exceeds printer bounds and safety margin")
        peak_speed = math.sqrt(accel * length)
        if peak_speed > speed_limit:
            raise ValueError(f"Sweep would exceed the firmware's {axis} speed limit")
        band = SweepBand(freq, accel, low, high, peak_speed * 60, max(4, math.ceil(dwell * freq)))
        # Reserve bandwidth for line numbers, checksums, acknowledgements and
        # temperature traffic. A successful timing check is still mandatory.
        longest = max(map(len, band.commands(axis)[1:3])) + 14
        if 2 * freq * longest > baudrate / 10 * 0.75:
            raise ValueError(f"Insufficient serial bandwidth for {freq:g} Hz; lower the sweep range")
        bands.append(band)
    return bands
