"""Parse replayable Marlin settings and build axis-specific shaping commands.

The FTM v2 contract and shaper IDs match Marlin_c13_octoprint's M493.cpp.
No settings are inferred from factory defaults: calibration needs a live report.
"""

from dataclasses import dataclass, field
import math
import re


SHAPER_IDS = {"ZV": 1, "ZVD": 2, "ZVDD": 3, "ZVDDD": 4,
              "EI": 5, "2HUMP_EI": 6, "3HUMP_EI": 7, "MZV": 8}
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def finite_number(value, name, minimum=None, maximum=None):
    """Reject booleans, non-finite values, and values outside the given limits."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum:g}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must not exceed {maximum:g}")
    return value


def parameters(text):
    return {k: float(v) for k, v in re.findall(r"([A-Z])\s*(" + NUMBER + r")", text)}


@dataclass
class FirmwareSnapshot:
    reports: dict = field(default_factory=dict)
    ftm_axes: dict = field(default_factory=dict)
    legacy_axes: dict = field(default_factory=dict)
    ftm_global: dict = field(default_factory=dict)
    ftm_version: int = 0
    feed_percent: float = None
    position: dict = field(default_factory=dict)
    trajectory: int = None
    smoothing: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def feed(self, line):
        """Consume one received line; callers run this outside the serial hook."""
        if re.search(r"(?:^|\s)(?:Error:|\?Invalid|\?Wrong|Unknown command:)", line):
            self.errors.append(line.strip())
        version = re.search(r"\bFTM V(\d+)\b", line)
        if version:
            self.ftm_version = int(version[1])
        speed = re.search(r"\bFR:(" + NUMBER + r")%", line)
        if speed:
            self.feed_percent = float(speed[1])
        pos = re.match(r"^(?:echo:)?\s*X:(" + NUMBER + r")\s+Y:(" + NUMBER + r")\s+Z:(" + NUMBER + r")", line)
        if pos:
            self.position = dict(zip("XYZ", map(float, pos.groups())))
        trajectory = re.search(r"Trajectory:.*\((\d)\)", line)
        if trajectory:
            self.trajectory = int(trajectory[1])
        smoothing = re.search(r"\b([XYZ]) smoothing time:\s*(" + NUMBER + r")s", line)
        if smoothing:
            self.smoothing[smoothing[1]] = float(smoothing[2])
        match = re.search(r"\b(M(?:92|201|203|204|205|493|593))\b(.*)", line)
        if not match:
            return
        code, rest = match.groups()
        values = parameters(rest)
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError("Firmware reported non-finite settings")
        axes = re.findall(r"(?:^|\s)([XYZE])(?=\s|$)", rest)
        if code == "M493":
            if axes:
                for axis in axes:
                    self.ftm_axes[axis] = values
            else:
                self.ftm_global = values
        elif code == "M593":
            for axis in axes:
                self.legacy_axes[axis] = values
        else:
            self.reports[code] = values

    @property
    def mode(self):
        if self.ftm_global:
            return "ftm"
        if self.legacy_axes:
            return "legacy"
        raise ValueError("No supported input-shaping settings in M503 report")

    def validate(self, axis):
        for code, keys in {"M92": "XYZ", "M201": "XYZ", "M203": "XYZ",
                           "M204": "PRT", "M205": "BST"}.items():
            if not set(keys).issubset(self.reports.get(code, {})):
                raise ValueError(f"Incomplete {code} report; calibration cannot preserve settings")
        finite_number(self.feed_percent, "reported speed percentage", 1, 1000)
        for key in "XYZ":
            for code in ("M92", "M201", "M203"):
                finite_number(self.reports[code][key], f"reported {code} {key}", 0.001)
        for key in "PRT":
            finite_number(self.reports["M204"][key], f"reported acceleration {key}", 0.001)
        for key in "BST":
            finite_number(self.reports["M205"][key], f"reported advanced motion {key}", 0)
        if self.mode == "ftm":
            if self.ftm_version != 2:
                raise ValueError("Only the verified FTM v2 M493 protocol is supported")
            if self.ftm_global.get("S") != 1 or self.ftm_global.get("D", 0) != 0:
                raise ValueError("Enable FTM with static shaping before calibration")
            if self.trajectory != 0 or any(v != 0 for v in self.smoothing.values()):
                raise ValueError("Host calibration requires trapezoidal FTM without extra smoothing")
            if not {"X", "Y"}.issubset(self.ftm_axes):
                raise ValueError("Incomplete M493 axis report")
            for values in self.ftm_axes.values():
                if not {"C", "A", "I", "Q"}.issubset(values):
                    raise ValueError("Incomplete FTM shaper backup")
                if values["C"] not in range(9):
                    raise ValueError("Unsupported FTM compensator in backup")
                finite_number(values["A"], "FTM frequency", 20, 500)
                finite_number(values["I"], "FTM damping", 0, 0.25)
                finite_number(values["Q"], "FTM vibration tolerance", 0, 1)
        elif not {"X", "Y"}.issubset(self.legacy_axes):
            raise ValueError("Incomplete legacy M593 backup")
        else:
            for values in self.legacy_axes.values():
                if not {"F", "D"}.issubset(values):
                    raise ValueError("Incomplete legacy shaping parameters")
                finite_number(values["F"], "legacy frequency", 0, 500)
                finite_number(values["D"], "legacy damping", 0, 0.99)

    def disable_commands(self):
        axes = self.ftm_axes if self.mode == "ftm" else self.legacy_axes
        return [f"M493 {axis} C0" if self.mode == "ftm" else f"M593 {axis} F0"
                for axis in axes]

    def restore_commands(self, full=False, shaping_parameters=True):
        commands = []
        if full:
            for code in ("M92", "M201", "M203"):
                commands.append(code + " " + " ".join(f"{k}{v:.8g}" for k, v in self.reports[code].items()))
        if self.mode == "ftm":
            # Restore shapers before global dynamic mode; D requires a shaper.
            for axis, values in self.ftm_axes.items():
                # A/I need an enabled shaper, and Q needs an EI shaper. Restore
                # latent parameters through EI before selecting the saved mode,
                # including NONE. c13 compiles EI for every shaped axis.
                if shaping_parameters:
                    commands.append(f"M493 {axis} C5 A{values['A']:.8g} I{values['I']:.8g} Q{values['Q']:.8g}")
                commands.append(f"M493 {axis} C{int(values['C'])}")
            if shaping_parameters:
                commands.append("M493 " + " ".join(f"{k}{v:.8g}" for k, v in self.ftm_global.items()))
        else:
            commands.extend(f"M593 {axis} F{v['F']:.8g}" + (f" D{v['D']:.8g}" if shaping_parameters else "")
                            for axis, v in self.legacy_axes.items())
        for code in ("M204", "M205"):
            values = self.reports[code]
            # Preserve precision of parameters calibration never changed.
            keys = values.keys() if full else (("T",) if code == "M204" else
                                               tuple(k for k in ("B", "S", "T", "X", "Y") if k in values))
            commands.append(code + " " + " ".join(f"{k}{values[k]:.8g}" for k in keys))
        commands.append(f"M220 S{self.feed_percent:.8g}")
        return commands

    def matches(self, other):
        def close_dict(a, b):
            return a.keys() == b.keys() and all(math.isclose(a[k], b[k], rel_tol=0.001, abs_tol=0.001) for k in a)
        return (self.mode == other.mode
                and close_dict(self.ftm_global, other.ftm_global)
                and self.ftm_axes.keys() == other.ftm_axes.keys()
                and self.legacy_axes.keys() == other.legacy_axes.keys()
                and all(close_dict(v, other.ftm_axes[k]) for k, v in self.ftm_axes.items())
                and all(close_dict(v, other.legacy_axes[k]) for k, v in self.legacy_axes.items())
                and all(close_dict(self.reports[k], other.reports.get(k, {})) for k in ("M92", "M201", "M203", "M204", "M205"))
                and self.feed_percent == other.feed_percent)


def shaping_command(mode, axis, name, frequency, damping, tolerance=0.05):
    if axis not in ("X", "Y"):
        raise ValueError("Only X and Y shaping is supported")
    frequency = finite_number(frequency, "shaper frequency", 20, 200)
    damping = finite_number(damping, "damping ratio", 0.01, 0.25)
    if mode == "legacy":
        if name != "ZV":
            raise ValueError("Legacy M593 supports only ZV")
        return f"M593 {axis} F{frequency:.3f} D{damping:.4f}"
    if mode != "ftm" or name not in SHAPER_IDS:
        raise ValueError("Unsupported firmware or shaper")
    command = f"M493 {axis} C{SHAPER_IDS[name]} A{frequency:.3f} I{damping:.4f}"
    if name in ("EI", "2HUMP_EI", "3HUMP_EI"):
        tolerance = finite_number(tolerance, "vibration tolerance", 0.001, 0.5)
        command += f" Q{tolerance:.4f}"
    return command
