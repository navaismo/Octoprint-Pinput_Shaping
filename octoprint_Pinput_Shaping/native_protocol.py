"""Strict PIS:RT v1/v2 framing and opt-in host-armed native sweep validation."""

from dataclasses import dataclass
import math
import re
import secrets

from .firmware import finite_number


DEFAULTS = {"excitationBackend": "software", "nativeStart": 20, "nativeEnd": 60,
            "nativeOctave": 20, "nativeAccel": 40, "nativePrepareTimeout": 180,
            "nativeMaxCapture": 300, "nativeCancelTimeout": 200}
ID = r"(?:0|[1-9][0-9]{0,9})"
HZ = r"(?:0|[1-9][0-9]*)\.[0-9]{2}"
ARM_TIMEOUT = 30
SENSOR_START_BUDGET = 25
PATTERNS = {
    "READY": re.compile(r"echo:PIS:RT:READY:([1-9][0-9]*)\Z"),
    "START": re.compile(rf"echo:PIS:RT:START:({ID}):([XYZ]):({HZ}):({HZ})\Z"),
    "PROGRESS": re.compile(rf"echo:PIS:RT:PROGRESS:({ID}):({HZ})\Z"),
    "COMPLETE": re.compile(rf"echo:PIS:RT:COMPLETE:({ID})\Z"),
    "ABORTED": re.compile(rf"echo:PIS:RT:ABORTED:({ID})\Z"),
    "ERROR": re.compile(rf"echo:PIS:RT:ERROR:({ID}):([A-Z][A-Z0-9_]*)\Z"),
    "HOMED": re.compile(rf"echo:PIS:RT:HOMED:({ID}):({ID})\Z"),
    "ARMED_WAIT": re.compile(rf"echo:PIS:RT:ARMED_WAIT:({ID})\Z"),
    "ARM_ACK": re.compile(rf"echo:PIS:RT:ARM_ACK:({ID})\Z"),
    "ARM_REJECTED": re.compile(rf"echo:PIS:RT:ARM_REJECTED:({ID}):([A-Z][A-Z0-9_]*)\Z"),
}


def parse_event(line):
    line = line.rstrip("\r\n")
    for kind, pattern in PATTERNS.items():
        match = pattern.fullmatch(line)
        if match:
            values = match.groups()
            if kind == "READY":
                return {"kind": kind, "version": int(values[0])}
            identifier = int(values[0])
            if identifier > 4294967295 or (identifier == 0 and kind not in ("ERROR", "ARM_REJECTED")):
                return None
            event = {"kind": kind, "id": identifier}
            if kind == "START":
                event.update(axis=values[1], start=float(values[2]), end=float(values[3]))
            elif kind == "PROGRESS":
                event["frequency"] = float(values[1])
            elif kind in ("ERROR", "ARM_REJECTED"):
                event["reason"] = values[1]
            elif kind == "HOMED":
                if int(values[1]) > 4294967295:
                    return None
                event["firmware_millis"] = int(values[1])
            return event
    return None


def new_firmware_id():
    return secrets.randbelow(4294967295) + 1


@dataclass(frozen=True)
class NativeParameters:
    start: float
    end: float
    octave: float
    acceleration: float
    preparation_timeout: float
    max_capture: float
    cancel_timeout: float

    @property
    def duration(self):
        return self.octave * math.log2(self.end / self.start)

    def command(self, axis, identifier, host_arm=False):
        if axis not in ("X", "Y"):
            raise ValueError("Native calibration supports X and Y only")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", str(identifier)) or not 1 <= int(identifier) <= 4294967295:
            raise ValueError("Firmware session ID must be a canonical nonzero uint32")
        return (f"M495 {axis} F{self.start:.2f} T{self.end:.2f} O{self.octave:.2f} "
                f"A{self.acceleration:.4f} I{identifier} S" + (" W" if host_arm else ""))

    @classmethod
    def from_settings(cls, settings):
        values = {**DEFAULTS, **settings}
        start = finite_number(values["nativeStart"], "native start frequency", 5, 199.99)
        end = finite_number(values["nativeEnd"], "native end frequency", 5, 200)
        if end <= start:
            raise ValueError("Native end frequency must exceed start frequency")
        octave = finite_number(values["nativeOctave"], "native octave duration", 20, 60)
        # C can be 1..8; use the strictest Q16 bound without assuming a factory C.
        accel = finite_number(values["nativeAccel"], "native acceleration per Hz", 0.0001,
                              (32767 * 4 * math.pi ** 2 / 8) - 1)
        for value in (start, end, octave):
            if abs(value * 100 - round(value * 100)) > 1e-6:
                raise ValueError("Native frequencies and octave duration support two decimal places")
        prep = finite_number(values["nativePrepareTimeout"], "native preparation timeout", 30, 240)
        cap = finite_number(values["nativeMaxCapture"], "native maximum capture duration", 30, 480)
        cancel = finite_number(values["nativeCancelTimeout"], "native cancellation timeout", 10, 300)
        result = cls(start, end, octave, accel, prep, cap, cancel)
        if SENSOR_START_BUDGET + result.duration * 1.15 + 5 > cap:
            raise ValueError("Maximum native capture must cover sensor startup, the sweep and its tail")
        return result


class NativeLifecycle:
    def __init__(self, identifier, axis, parameters, host_arm=False):
        self.identifier, self.axis, self.parameters = identifier, axis, parameters
        self.start = None
        self.terminal = None
        self.frequency = None
        self.host_arm = host_arm
        self.homed = self.armed_wait = self.arm_requested = self.arm_ack = None

    def request_arm(self, stamp):
        if (not self.host_arm or self.armed_wait is None or self.arm_requested is not None
                or self.start is not None or self.terminal is not None):
            raise ValueError("Native ARM requires a pending prepared session")
        self.arm_requested = stamp

    def accept(self, event, stamp):
        if event is None or event.get("id") != self.identifier:
            return False
        kind = event["kind"]
        if self.terminal is not None:
            raise ValueError("Duplicate or post-terminal native event")
        if kind == "HOMED":
            if not self.host_arm or self.homed is not None or self.armed_wait is not None:
                raise ValueError("Unexpected or duplicate native HOMED")
            self.homed = {**event, "host_time": stamp}
        elif kind == "ARMED_WAIT":
            if not self.host_arm or self.armed_wait is not None or self.homed is None:
                raise ValueError("Unexpected or duplicate native ARMED_WAIT")
            self.armed_wait = stamp
        elif kind == "ARM_ACK":
            if not self.host_arm or self.arm_requested is None:
                raise ValueError("Native ARM_ACK arrived before host authorization")
            if self.arm_ack is None:
                self.arm_ack = stamp
        elif kind == "ARM_REJECTED":
            raise ValueError("Native ARM rejected: " + event["reason"])
        elif kind == "START":
            if self.host_arm and self.arm_ack is None:
                raise ValueError("Native START arrived before ARM_ACK")
            if self.start is not None:
                raise ValueError("Duplicate native START")
            if (event["axis"] != self.axis or abs(event["start"] - self.parameters.start) > 0.005
                    or abs(event["end"] - self.parameters.end) > 0.005):
                raise ValueError("Native START does not match the requested axis or range")
            self.start = stamp
        elif kind == "PROGRESS":
            if self.start is None:
                raise ValueError("Native PROGRESS arrived before START")
            frequency = event["frequency"]
            if (not self.parameters.start <= frequency <= self.parameters.end
                    or (self.frequency is not None and frequency < self.frequency)):
                raise ValueError("Invalid native progress frequency")
            self.frequency = frequency
        elif kind in ("COMPLETE", "ABORTED", "ERROR"):
            if kind == "COMPLETE" and self.start is None:
                raise ValueError("Native COMPLETE arrived before START")
            self.terminal = {**event, "host_time": stamp}
        else:
            return False
        return True
