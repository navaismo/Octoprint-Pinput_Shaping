"""Bounded acceleration-sweep excitation for Cartesian Marlin printers."""

from dataclasses import dataclass
import math

from .firmware import finite_number


# Reviewed c13/c14 Configuration_adv.h and planner.cpp reject smaller segments.
MIN_SEGMENT_STEPS = 4


@dataclass(frozen=True)
class ExcitationBand:
    acceleration: float
    center: float
    amplitude: float
    feedrate: float
    cycles: int

    @property
    def low(self):
        return self.center - self.amplitude

    @property
    def high(self):
        return self.center + self.amplitude

    @property
    def minimum_duration(self):
        return 4 * self.amplitude * self.cycles / (self.feedrate / 60)

    @property
    def duration(self):
        speed = self.feedrate / 60
        if speed * speed >= self.amplitude * self.acceleration:
            stroke_time = 2 * math.sqrt(self.amplitude / self.acceleration)
        else:
            stroke_time = self.amplitude / speed + speed / self.acceleration
        return 4 * self.cycles * stroke_time

    def commands(self, axis):
        # Restore the original center/high/center/low acceleration sweep.
        commands = [f"M204 T{self.acceleration:.6f}"]
        for _ in range(self.cycles):
            commands.extend((f"G1 {axis}{self.high:.5f} F{self.feedrate:.3f}",
                             f"G1 {axis}{self.center:.5f} F{self.feedrate:.3f}",
                             f"G1 {axis}{self.low:.5f} F{self.feedrate:.3f}",
                             f"G1 {axis}{self.center:.5f} F{self.feedrate:.3f}"))
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


def plan_excitation(axis, center, limits, settings, snapshot, baudrate=115200):
    """Plan broad excitation; analysis frequencies do not set motor reversals.

    Millimeter strokes avoid the planner's tiny-segment behavior. Estimated
    duration sizes the capture and deadlines, not a claimed excitation clock.
    """
    start = finite_number(settings["freqStart"], "start frequency", 5, 200)
    end = finite_number(settings["freqEnd"], "end frequency", start + 1, 200)
    duration = finite_number(settings.get("sweepDuration", 20), "excitation duration", 10, 120)
    minimum = finite_number(settings["accelMin"], "minimum acceleration", 1)
    maximum = finite_number(settings["accelMax"], "maximum acceleration", minimum,
                            snapshot.reports["M201"][axis])
    steps = snapshot.reports["M92"][axis]
    speed_limit = snapshot.reports["M203"][axis]
    center = math.floor(center * steps + 0.5) / steps
    bands = []
    for index in range(16):
        fraction = index / 15
        accel = minimum + (maximum - minimum) * fraction
        amplitude = max(MIN_SEGMENT_STEPS, round((5 - 4 * fraction) * steps)) / steps
        if center - amplitude < limits[0] + 5 or center + amplitude > limits[1] - 5:
            raise ValueError("Sweep excursion exceeds printer bounds and safety margin")
        feed = min(15000, max(2000, 100 * accel), speed_limit * 60)
        single = ExcitationBand(accel, center, amplitude, feed, 1)
        cycles = max(1, math.ceil(duration / 16 / single.duration))
        bands.append(ExcitationBand(accel, center, amplitude, feed, cycles))
    wire_bytes = sum(sum(len(command) + 14 for command in band.commands(axis)) for band in bands)
    if max(sum(band.duration for band in bands), wire_bytes / (baudrate / 10 * 0.75)) > 400:
        raise ValueError("Excitation exceeds the time budget; review duration, speed or serial bandwidth")
    return bands
