"""In-memory c13 FTM v2 transport and accelerometer for lifecycle tests."""

from copy import deepcopy
import math
from pathlib import Path
import re

import numpy as np
import pandas as pd

from octoprint_Pinput_Shaping.firmware import SHAPER_IDS, parameters, shaping_command


class Clock:
    def __init__(self):
        self.value = 1000.0

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Printer:
    def __init__(self, snapshot, clock):
        self.snapshot = deepcopy(snapshot)
        self.clock = clock
        self.controller = None
        self.state = "OPERATIONAL"
        self.sent_commands = []
        self.hold = False
        self.distortion = 1
        self.fail_on_move = False
        self.silent = False
        self.reject_apply = False

    def get_state_id(self):
        return self.state

    def get_current_connection(self):
        return self.state, "/dev/fake-printer", 115200, None

    def set_job_on_hold(self, value, blocking=True):
        self.hold = value
        return True

    def emit(self, line):
        if not self.silent:
            self.controller.receive(line)

    def commands(self, commands, tags=None, force=False):
        if isinstance(commands, str):
            commands = [commands]
        # OctoPrint queues commands before the serial sender processes them.
        commands = [cmd for cmd in commands
                    if self.controller.queuing(cmd, cmd.split()[0], tags) != (None,)]
        for cmd in commands:
            gcode = cmd.split()[0]
            if self.state != "OPERATIONAL" and gcode != "M112":
                continue
            if self.controller.sending(cmd, gcode, tags) == (None,):
                continue
            self.sent_commands.append(cmd)
            self.controller.sent(cmd, gcode, tags)
            values = parameters(cmd[len(gcode):])
            self.clock.advance(0.0001)
            if gcode == "M118":
                self.emit(cmd[5:])
            elif gcode == "M503":
                for code, params in self.snapshot.reports.items():
                    self.emit(code + " " + " ".join(f"{k}{v}" for k, v in params.items()))
                if self.snapshot.mode == "ftm":
                    self.emit("M493 " + " ".join(f"{k}{v}" for k, v in self.snapshot.ftm_global.items()))
                    axes, code = self.snapshot.ftm_axes, "M493"
                else:
                    axes, code = self.snapshot.legacy_axes, "M593"
                for axis, params in axes.items():
                    self.emit(code + " " + axis + " " + " ".join(f"{k}{v}" for k, v in params.items()))
            elif cmd == "M493 V":
                self.emit(f"FTM V{self.snapshot.ftm_version}")
            elif gcode == "M494":
                self.emit(f"  Trajectory: selected({self.snapshot.trajectory})")
            elif gcode == "M493":
                axes = re.findall(r"(?:^|\s)([XYZE])(?=\s|$)", cmd)
                if not axes:
                    self.snapshot.ftm_global.update(values)
                else:
                    for axis in axes:
                        current = self.snapshot.ftm_axes[axis]
                        if self.reject_apply and "A" in values and values.get("C") != 5:
                            continue
                        current.update({k: v for k, v in values.items() if k == "C"})
                        for key in ("A", "I", "Q"):
                            if key in values:
                                if current["C"] == 0 or (key == "Q" and current["C"] not in (5, 6, 7)):
                                    self.emit("?Wrong mode for axis parameter")
                                else:
                                    current[key] = values[key]
            elif gcode == "M593":
                axes = re.findall(r"(?:^|\s)([XYZE])(?=\s|$)", cmd)
                for axis in axes:
                    self.snapshot.legacy_axes[axis].update(values)
            elif gcode in ("M92", "M201", "M203", "M204", "M205"):
                self.snapshot.reports[gcode].update(values)
            elif gcode == "M220":
                if "S" in values:
                    self.snapshot.feed_percent = values["S"]
                else:
                    self.emit(f"FR:{self.snapshot.feed_percent}%")
            elif gcode == "M114":
                self.emit("X:110.00 Y:110.00 Z:10.00 E:0.00")
            elif gcode == "G28":
                self.snapshot.position = dict(X=0, Y=0, Z=0)
            elif gcode == "G1" and set(values).intersection("XYZ"):
                if self.fail_on_move:
                    self.state = "OFFLINE"
                    self.controller.event("Disconnected")
                    continue
                distance = math.sqrt(sum((values.get(k, self.snapshot.position.get(k, 0)) -
                                          self.snapshot.position.get(k, 0)) ** 2 for k in "XYZ"))
                acceleration = self.snapshot.reports["M204"]["T"]
                self.clock.advance(2 * math.sqrt(distance / acceleration) * self.distortion)
                self.snapshot.position.update({k: v for k, v in values.items() if k in "XYZ"})
            elif gcode == "M112":
                self.state = "ERROR"
                self.controller.event("Error")


class Capture:
    frequency = 50

    def __init__(self, sensor_type, path, check_cancel, port="", lis2dw_rate=800):
        self.path = Path(path)
        self.check_cancel = check_cancel
        self.stopped = False
        self.rate = lis2dw_rate if sensor_type == "lis2dw" else 3200

    def start(self):
        self.check_cancel()
        t = np.arange(6400) / 800
        rng = np.random.default_rng(99)
        data = {"time": t, "x": np.sin(2 * np.pi * self.frequency * t) + rng.normal(0, 0.01, len(t)),
                "y": rng.normal(0, 0.01, len(t)), "z": 1 + rng.normal(0, 0.01, len(t))}
        pd.DataFrame(data).to_csv(self.path, index=False)

    def stop(self):
        self.stopped = True

    def ensure_alive(self):
        self.check_cancel()

    def abort(self):
        pass


class Analyzer:
    def __init__(self, directory, csv_path, **kwargs):
        self.directory = Path(directory)
        self.axis = kwargs["axis"]
        self.damping = float(kwargs["damping"])
        self.tolerance = 0.05
        self.mode = kwargs.get("firmware_mode", "ftm")
        self.quality, self.warnings = {}, []

    def load_data(self):
        pass

    def analyze(self):
        pass

    def generate_graphs(self):
        for name in ("signal.png", "psd.png"):
            (self.directory / name).write_bytes(b"fake plot")

    def get_recommendation(self):
        return shaping_command(self.mode, self.axis, "ZV", 50, self.damping)

    def get_plotly_data(self):
        names = SHAPER_IDS if self.mode == "ftm" else {"ZV": 1}
        return {"base_freq": 50, "best_shaper_freq": 50, "best_shaper": "ZV",
                "shapers": {name: {"shaper_freq": 50 + index * 2, "vibr": 0.1,
                             "shaper_duration_ms": 10, "suggested_accel": 2500}
                            for index, name in enumerate(names)}}
