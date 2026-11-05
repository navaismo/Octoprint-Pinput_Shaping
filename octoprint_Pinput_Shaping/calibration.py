"""Single-owner calibration sessions; serial hooks only enqueue notifications."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import logging
import math
from pathlib import Path
import queue
import re
import threading
import time
import uuid

from .capture import AccelerometerCapture
from .firmware import FirmwareSnapshot, SHAPER_IDS, finite_number, shaping_command
from .motion import bounds_from_profile, plan_excitation, validate_position
from .native import NativeResonanceMixin
from .native_protocol import NativeParameters


TAG = "plugin:Pinput_Shaping"
RECOVERY_TAG = "pinput:recovery"
READ_ONLY = {"M105", "M114", "M115", "M119", "M27", "M400"}
# Automatic reports, keepalives and display updates do not change motion.
STATUS_COMMANDS = READ_ONLY | {"M154", "M155", "M113", "M73", "M117"}


@dataclass
class Session:
    kind: str
    axis: str
    settings: dict
    profile: dict
    position: dict
    connection: int
    shaper: str = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancelled: threading.Event = field(default_factory=threading.Event)
    reason: str = "Calibration cancelled"
    stage: str = "preparing"
    counter: int = 0
    changed: bool = False
    transport_fault: bool = False
    snapshot: object = None
    modes: dict = field(default_factory=dict)
    native_mode: bool = False
    native_sent: bool = False
    native_stopped: bool = False
    firmware_id: int = None
    native_events: list = field(default_factory=list)
    native_commands: list = field(default_factory=list)
    native_outgoing: object = field(default_factory=lambda: queue.Queue(maxsize=64), repr=False)
    native_capture_error: str = None
    native_idle_confirmed: bool = False
    deadline: float = field(default_factory=lambda: time.monotonic() + 600)


class CalibrationController(NativeResonanceMixin):
    def __init__(self, printer, profile, settings, data_folder, publish,
                 logger=None, capture_factory=AccelerometerCapture, analyzer_factory=None):
        self.printer = printer
        self.profile = profile
        self.settings = settings
        self.data_folder = Path(data_folder)
        self._publish_callback = publish
        self.logger = logger or logging.getLogger(__name__)
        self.capture_factory = capture_factory
        self.analyzer_factory = analyzer_factory
        self.lock = threading.RLock()
        self.inbox = queue.Queue(maxsize=8192)
        self.session = None
        self.thread = None
        self.connection = 0
        self.result = None
        self.error = None
        self.firmware = self._unknown_firmware()
        self.native_capability = self._unknown_native()
        self.native_connection = None
        self.native_diagnostics = None
        self._firmware_identity = None
        self._shutting_down = False
        self.modes = dict(units=None, xyz=None, extruder=None, feed=None)
        self.journal = self.data_folder / "recovery_required.json"
        self.native_unresolved = False
        if self.journal.exists():
            try:
                with self.journal.open(encoding="utf-8") as stream:
                    self.native_unresolved = json.load(stream).get("native_only", False)
            except (OSError, ValueError):
                self.native_unresolved = True

    @staticmethod
    def _unknown_firmware():
        return {"state": "unknown", "protocol": None, "label": "Not detected",
                "allowed_shapers": [], "message": "Click Detect firmware while the printer is connected and idle."}

    def _set_firmware(self, session, snapshot=None, error=None):
        with self.lock:
            if session.connection != self.connection:
                return
            try:
                protocol = snapshot.mode if snapshot is not None else None
            except ValueError:
                protocol = None
            ftm_label = f"FTM v{snapshot.ftm_version} (M493)" if snapshot and snapshot.ftm_version else "FTM (M493)"
            label = {"ftm": ftm_label, "legacy": "Legacy ZV (M593)"}.get(protocol, "Not detected")
            if self.firmware["protocol"] is not None and (protocol, label) != (self.firmware["protocol"], self.firmware["label"]):
                self.native_capability = self._unknown_native("Firmware identification changed; detect M495 again.")
                self.native_connection = None
            self.firmware = {
                "state": "unsupported" if error else "ready", "protocol": protocol,
                "label": label,
                "allowed_shapers": [] if error else (list(SHAPER_IDS) if protocol == "ftm" else ["ZV"]),
                "message": error or ("Select a fitted shaper or use the recommendation." if protocol == "ftm"
                                     else "Only the recommended ZV settings can be applied; advanced shapers are unavailable."),
            }

    def publish(self, payload):
        try:
            self._publish_callback(payload)
        except Exception:
            # A browser notification failure must never bypass restoration.
            self.logger.exception("Could not publish calibration status")

    def status(self):
        with self.lock:
            if self.native_connection and self.native_connection != tuple(self.printer.get_current_connection()[1:3]):
                self.native_capability = self._unknown_native("Printer transport changed; detect again.")
                self.native_connection = None
                if self.result and self.result.get("backend") == "marlin_m495":
                    self.result = None
                self.cancel("Printer transport changed during calibration")
            return {"busy": self.session is not None,
                    "session_id": self.session.id if self.session else None,
                    "stage": self.session.stage if self.session else "idle",
                    "error": self.error, "result": deepcopy(self.result),
                    "firmware": deepcopy(self.firmware),
                    "native_capability": deepcopy(self.native_capability),
                    "diagnostics": deepcopy(self.native_diagnostics),
                    "recovery_required": self.journal.exists() and self.session is None,
                    "modes_ready": all(v is not None for v in self.modes.values())}

    def submit(self, kind, data=None):
        data = data or {}
        if kind not in ("sensor", "axis", "resonance", "apply", "recover", "prepare", "detect", "detect_native"):
            raise ValueError("Unknown calibration operation")
        with self.lock:
            if self._shutting_down:
                raise RuntimeError("OctoPrint is shutting down")
            if self.session:
                raise RuntimeError("Another calibration operation is still active")
            if self.printer.get_state_id() != "OPERATIONAL":
                raise RuntimeError("The printer must be connected and idle")
            if self.journal.exists() and kind != "recover":
                raise RuntimeError("Restore the interrupted calibration before starting another operation")
            if kind not in ("prepare", "sensor", "recover", "detect", "detect_native") and self.modes["units"] != "G21":
                raise RuntimeError("Prepare millimeter coordinate modes before calibration")
            settings, profile = deepcopy(self.settings()), deepcopy(self.profile())
            axis = data.get("axis", "X")
            if kind == "resonance" and data.get("backend", settings.get("excitationBackend", "software")) != settings.get("excitationBackend", "software"):
                raise ValueError("Save plugin settings before calibration; the selected backend differs from the saved backend")
            native = kind == "resonance" and settings.get("excitationBackend", "software") == "marlin_m495"
            if kind == "resonance" and settings.get("excitationBackend", "software") not in ("software", "marlin_m495"):
                raise ValueError("Unsupported excitation backend")
            if native:
                parameters = NativeParameters.from_settings(settings)
                if "native_settings" in data:
                    if not isinstance(data["native_settings"], dict):
                        raise ValueError("Native settings must be an object")
                    if NativeParameters.from_settings({**settings, **data["native_settings"]}) != parameters:
                        raise ValueError("Save native settings before calibration; the parameters differ from the saved values")
                if (self.native_capability["state"] != "supported" or self.native_capability.get("protocol") != 2
                        or self.native_connection != tuple(self.printer.get_current_connection()[1:3])):
                    raise RuntimeError("Detect M495 Support for the current connection before native calibration")
            position = {}
            if kind in ("axis", "resonance"):
                if native:
                    if axis not in ("X", "Y"):
                        raise ValueError("Native calibration supports X and Y only")
                else:
                    position = validate_position(profile, axis, {"X": data.get("start_x"),
                        "Y": data.get("start_y"), "Z": data.get("start_z")})
                if not all(v is not None for v in self.modes.values()):
                    raise RuntimeError("Prepare coordinate modes before calibration")
            if kind == "apply":
                if {"command", "frequency", "damping", "tolerance"}.intersection(data):
                    raise ValueError("Select a saved shaper; command and parameter overrides are not accepted")
                if not self.result or data.get("session_id") != self.result["session_id"]:
                    raise ValueError("The result is stale or incomplete; run calibration again")
                if self.result["connection"] != self.connection:
                    raise ValueError("The printer connection changed after calibration")
                if self.result.get("recommendation_valid") is False:
                    raise ValueError("This measurement has no validated recommendation")
                axis = self.result["axis"]
                shaper, _ = self._application_candidate(self.result, data.get("shaper", self.result["best_shaper"]))
            session = Session(kind, axis, settings, profile, position, self.connection)
            session.native_idle_confirmed = kind == "recover" and data.get("native_idle_confirmed") is True
            session.native_mode = native or (kind == "apply" and self.result.get("backend") == "marlin_m495")
            if native:
                session.deadline = time.monotonic() + parameters.preparation_timeout + parameters.max_capture + parameters.cancel_timeout + 60
                self.native_diagnostics = None
            if kind == "apply":
                session.shaper = shaper
            session.modes = deepcopy(self.modes)
            self.session = session
            self.error = None
            if kind not in ("apply", "recover", "detect", "detect_native"):
                self.result = None
            if kind == "detect":
                self.firmware = {**self._unknown_firmware(), "state": "detecting",
                                 "message": "Reading firmware input-shaping settings."}
            if kind == "detect_native":
                self.native_capability = {**self._unknown_native(), "state": "detecting"}
            self.thread = threading.Thread(target=self._run, args=(session,), daemon=True,
                                           name=f"pinput-{session.id[:8]}")
            self.thread.start()
            return session.id

    def cancel(self, reason="Calibration cancelled"):
        session = self.session
        if session:
            session.reason = reason
            session.cancelled.set()

    def receive(self, line):
        """Non-blocking serial callback: no file, process, or analysis work."""
        if line.startswith(("FIRMWARE_NAME:", "echo:FIRMWARE_NAME:")):
            identity = line.strip()
            if self._firmware_identity is not None and identity != self._firmware_identity:
                self.native_capability = self._unknown_native("Firmware identity changed; detect again.")
                self.native_connection = None
                self.result = None
                self.cancel("Firmware identity changed during calibration")
            self._firmware_identity = identity
        if self.session:
            try:
                self.inbox.put_nowait((time.monotonic(), line))
            except queue.Full:
                self.cancel("Serial event buffer overflowed")
        return line

    def queuing(self, cmd, gcode, tags=None):
        session = self.session
        # M110 is needed to establish OctoPrint's line-number handshake after
        # reconnect, including when the native recovery journal is pending.
        if not session and self.native_unresolved and gcode not in STATUS_COMMANDS | {"M112", "M496", "M110"}:
            return (None,)
        if not session or gcode == "M112":
            return None
        if not cmd or not cmd.strip() or cmd.lstrip().startswith(";"):
            return None
        tags = tags or set()
        if TAG in tags:
            if session.cancelled.is_set() and RECOVERY_TAG not in tags and gcode not in ("M400", "M118"):
                return (None,)
            return None
        if gcode in STATUS_COMMANDS:
            return None
        if session.kind in ("detect", "detect_native"):
            self.cancel(f"Firmware detection interrupted by command {gcode or 'unknown'}; click Detect firmware to retry")
            return None
        self.logger.warning("Calibration session %s (%s) blocked command %r, gcode=%r, tags=%r",
                            session.id, session.stage, cmd[:160], gcode, sorted(map(str, tags)))
        self.cancel(f"Command {gcode or 'unknown'} interrupted calibration; see Pinput_Shaping.log for its source")
        return (None,)

    def sent(self, cmd, gcode, tags=None):
        session = self.session
        tags = tags or set()
        if gcode == "M495" and session and session.native_mode and " I" in cmd and re.search(r"\bS(?: W)?$", cmd):
            session.native_sent = True
        if (session and gcode in ("M495", "M496") and TAG in tags
                and f"pinput:session:{session.id}" in tags):
            # The worker writes diagnostics. Never perform log/file I/O on
            # OctoPrint's serial sender thread.
            try:
                session.native_outgoing.put_nowait({"host_time": time.monotonic(), "command": cmd, "phase": "sent"})
            except queue.Full:
                self.cancel("Native transmission diagnostics overflowed")
        if gcode in ("G20", "G21"):
            self.modes["units"] = gcode
        elif gcode in ("G90", "G91"):
            self.modes["xyz"] = gcode
        elif gcode in ("M82", "M83"):
            self.modes["extruder"] = gcode
        elif gcode in ("G0", "G1"):
            match = re.search(r"\bF([\d.]+)", cmd)
            if match:
                self.modes["feed"] = float(match[1]) * (25.4 if self.modes["units"] == "G20" else 1)

    def sending(self, cmd, gcode, tags=None):
        """Discard owned commands still in OctoPrint's queue after cancellation."""
        tags = tags or set()
        if TAG not in tags or gcode in ("M112", "M400", "M118"):
            return None
        session = self.session
        if (session is None or session.connection != self.connection
                or f"pinput:session:{session.id}" not in tags
                or (session.cancelled.is_set() and RECOVERY_TAG not in tags)):
            return (None,)
        return None

    def event(self, event):
        if event in ("Connected", "Disconnected", "Error"):
            with self.lock:
                self.connection += 1
                self.result = None
                self.firmware = self._unknown_firmware()
                self.native_capability = self._unknown_native()
                self.native_connection = None
                self._firmware_identity = None
                self.modes = dict(units=None, xyz=None, extruder=None, feed=None)
                self.cancel("Printer connection was interrupted; recovery may be required")
            self.publish({"type": "state", **self.status()})
        elif event in ("PrintStarted", "PrintResumed"):
            self.cancel("A print started during calibration")

    def _check(self, session):
        if session.cancelled.is_set():
            raise RuntimeError(session.reason)
        if session.connection != self.connection or self.printer.get_state_id() != "OPERATIONAL":
            raise RuntimeError("Printer is no longer idle on the original connection")
        if session.native_mode and self.native_connection != tuple(self.printer.get_current_connection()[1:3]):
            self.native_capability = self._unknown_native("Printer transport changed; detect again.")
            raise RuntimeError("Printer transport changed during native calibration")
        if self.profile() != session.profile:
            raise RuntimeError("Printer profile changed during calibration")
        if time.monotonic() > session.deadline:
            raise RuntimeError("Calibration exceeded its ten-minute deadline")

    def _stage(self, session, stage):
        session.stage = stage
        self.logger.info("Calibration session %s (%s): %s", session.id, session.kind, stage)
        self.publish({"type": "state", **self.status()})

    def _exchange(self, session, commands, timeout=15, recovery=False, timestamps=None, monitor=None):
        if not recovery:
            self._check(session)
        session.counter += 1
        marker = f"Pinput_Shaping:{session.id}:{session.counter}"
        tags = {TAG, f"pinput:session:{session.id}"}
        if recovery:
            tags.add(RECOVERY_TAG)
        self.printer.commands(list(commands) + [f"M118 {marker}"], tags=tags)
        deadline, lines = time.monotonic() + timeout, []
        while time.monotonic() < deadline:
            if not recovery:
                self._check(session)
                if monitor is not None:
                    monitor()
            elif self.printer.get_state_id() != "OPERATIONAL" or session.connection != self.connection:
                raise RuntimeError("Cannot restore settings on a changed or unavailable connection")
            try:
                stamp, line = self.inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            cleaned = re.sub(r"^(?:echo:)?\s*", "", line.strip())
            if timestamps is not None and cleaned in timestamps:
                timestamps[cleaned] = stamp
            if cleaned == marker:
                if timestamps is not None and any(value is None for value in timestamps.values()):
                    raise RuntimeError("Firmware start confirmation was missing; results are invalid")
                errors = [x for x in lines if re.search(r"(?:Error:|\?Invalid|\?Wrong|Unknown command:)", x)]
                if errors:
                    raise RuntimeError("Firmware rejected a command: " + errors[-1][:200])
                return lines, stamp
            lines.append(line)
        session.transport_fault = True
        raise RuntimeError("Firmware confirmation timed out; results are invalid")

    def _snapshot(self, session, recovery=False):
        result = FirmwareSnapshot()
        try:
            commands = (["G21"] if session.kind == "recover" else []) + ["M400", "M503", "M220", "M114"]
            lines, _ = self._exchange(session, commands, recovery=recovery)
            for line in lines:
                result.feed(line)
            if result.mode == "ftm":
                lines, _ = self._exchange(session, ["M493 V", "M494"], recovery=recovery)
                for line in lines:
                    result.feed(line)
            result.validate(session.axis, allow_standard=session.native_mode or session.kind in ("detect", "recover"))
        except Exception as exc:
            self._set_firmware(session, result, error=str(exc))
            raise
        self._set_firmware(session, result)
        return result

    def _write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.flush()
            import os
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _backup(self, session):
        self._write_json(self.journal, {"snapshot": asdict(session.snapshot),
            "modes": session.modes, "profile": session.profile, "session_id": session.id})
        session.changed = True

    def _mode_commands(self, modes):
        commands = ["G21", "G90"]
        if modes.get("feed") is not None:
            commands.append(f"G1 F{modes['feed']:.5f}")
        commands += [modes.get("units") or "G21", modes.get("xyz") or "G90"]
        if modes.get("extruder"):
            commands.append(modes["extruder"])
        return commands

    def _restore(self, session):
        if session.native_mode and session.kind in ("resonance", "recover"):
            return self._native_restore(session)
        self._stage(session, "restoring")
        commands = ["M400"] + session.snapshot.restore_commands(full=session.kind == "recover",
            shaping_parameters=session.kind in ("apply", "recover")) + self._mode_commands(session.modes)
        self._exchange(session, commands, timeout=30, recovery=True)
        current = self._snapshot(session, recovery=True)
        if not session.snapshot.matches(current):
            raise RuntimeError("Firmware settings did not match the saved configuration after restoration")
        self.journal.unlink(missing_ok=True)
        session.changed = False

    def _wait(self, session, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check(session)
            session.cancelled.wait(min(0.1, max(0, deadline - time.monotonic())))

    def _capture(self, session, path):
        import os
        sensor = session.settings.get("sensorType", "adxlspi")
        port = session.settings.get("sensorPort", "") or {"adxlusb": "/dev/ttyACM1", "lis2dw": "/dev/ttyACM0"}.get(sensor, "")
        if port and sensor != "adxlspi":
            printer_port = self.printer.get_current_connection()[1]
            if printer_port and os.path.realpath(port) == os.path.realpath(printer_port):
                raise ValueError("The accelerometer port must differ from the printer connection")
        options = {"lis2dw_rate": session.settings.get("lis2dwRate", 800)} if sensor == "lis2dw" else {}
        return self.capture_factory(sensor, path, check_cancel=lambda: self._check(session), port=port, **options)

    def _analyzer(self, session, path):
        if self.analyzer_factory is None:
            from .inputshaping_analyzer import InputShapingAnalyzer
            factory = InputShapingAnalyzer
        else:
            factory = self.analyzer_factory
        return factory(path.parent, path, damping=session.settings["dampingRatio"],
            axis=session.axis, min_accel=session.settings["accelMin"],
            max_accel=session.settings["accelMax"], min_freq=session.settings["freqStart"],
            max_freq=session.settings["freqEnd"], firmware_mode=session.snapshot.mode,
            sensor_axis=session.settings.get("sensorAxis", "sum"), logger=self.logger)

    def _home(self, session):
        self._stage(session, "homing")
        self._exchange(session, ["G21", "G90", "G28", "M400"], timeout=180)
        p = session.position
        zfeed = min(600, session.snapshot.reports["M203"]["Z"] * 60)
        xyfeed = min(1500, min(session.snapshot.reports["M203"][a] for a in "XY") * 60)
        park_accel = min(500, min(session.snapshot.reports["M201"][a] for a in "XYZ"))
        self._exchange(session, [f"M204 T{park_accel:.6f}", f"G1 Z{p['Z']:.5f} F{zfeed:.3f}",
            f"G1 X{p['X']:.5f} Y{p['Y']:.5f} F{xyfeed:.3f}", "M400"], timeout=90)

    def _sensor_test(self, session, path):
        capture = self._capture(session, path)
        try:
            capture.start()
            self._wait(session, 2.2)
            capture.ensure_alive()
            capture.stop()
        finally:
            capture.abort()
        import csv
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
        if len(rows) < 16:
            raise RuntimeError("Sensor returned too few samples")
        for row in rows:
            for key in ("time", "x", "y", "z"):
                finite_number(row.get(key), f"sensor {key}")
        return rows[:5], len(rows)

    def _motion_test(self, session, folder):
        self._backup(session)
        self._home(session)
        limits = bounds_from_profile(session.profile)[session.axis]
        center = session.position[session.axis]
        if center - 2 < limits[0] + 5 or center + 2 > limits[1] - 5:
            raise ValueError("Axis test exceeds the travel margin")
        feedrate = min(600, session.snapshot.reports["M203"][session.axis] * 60)
        self._exchange(session, [f"G1 {session.axis}{center + 2:.5f} F{feedrate:.3f}",
            f"G1 {session.axis}{center - 2:.5f} F{feedrate:.3f}",
            f"G1 {session.axis}{center:.5f} F{feedrate:.3f}", "M400"], timeout=30)
        self._restore(session)

    def _resonance(self, session, folder):
        backend = session.settings.get("excitationBackend", "software")
        if backend == "marlin_m495":
            return self._resonance_native(session, folder)
        if backend != "software":
            raise ValueError("Unsupported excitation backend")
        return self._resonance_software(session, folder)

    def _resonance_software(self, session, folder):
        limits = bounds_from_profile(session.profile)
        baud = self.printer.get_current_connection()[2] or 115200
        bands = plan_excitation(session.axis, session.position[session.axis], limits[session.axis],
                           session.settings, session.snapshot, baudrate=baud)
        self.logger.info("Calibration session %s: acceleration-sweep excitation, analysis %.3f–%.3f Hz, "
                         "acceleration %.3f–%.3f mm/s^2, estimated motion %.3f s",
                         session.id, float(session.settings["freqStart"]), float(session.settings["freqEnd"]),
                         bands[0].acceleration, bands[-1].acceleration, sum(b.duration for b in bands))
        # Sensor readiness is checked before any physical motion is commanded.
        self._stage(session, "checking sensor")
        self._sensor_test(session, folder / "sensor_check.csv")
        sensor_check = self._analyzer(session, folder / "sensor_check.csv")
        sensor_check.load_data()
        self._backup(session)
        self._exchange(session, session.snapshot.disable_commands() + ["M220 S100", "M205 B0 S0 T0"] +
            (["M205 X0 Y0"] if "X" in session.snapshot.reports["M205"] else []))
        self._home(session)
        capture = self._capture(session, folder / "capture.csv")
        measurement = {"kind": "acceleration_sweep", "continuous": True,
                       "estimated_duration": sum(b.duration for b in bands),
                       "minimum_duration": sum(b.minimum_duration for b in bands),
                       "actual_duration": None, "valid_timing": False,
                       "blocks": [{**asdict(b), "estimated_duration": b.duration,
                                   "minimum_duration": b.minimum_duration} for b in bands]}
        timing = [measurement]
        try:
            capture.start()
            self._wait(session, 0.3)
            self._stage(session, "measuring")
            start_marker = f"Pinput_Shaping:{session.id}:excitation:start"
            timestamps = {start_marker: None}
            commands = [f"M118 {start_marker}"]
            for band in bands:
                commands.extend(band.commands(session.axis))
            commands.append("M400")
            # Stream the complete bounded ramp. The sending hook suppresses
            # unsent motion if cancellation, sensor failure or disconnect occurs.
            _, end = self._exchange(session, commands,
                                    timeout=max(30, measurement["estimated_duration"] * 3),
                                    timestamps=timestamps, monitor=capture.ensure_alive)
            actual = end - timestamps[start_marker]
            valid_timing = actual + 0.02 >= measurement["minimum_duration"]
            measurement.update(actual_duration=actual, valid_timing=valid_timing)
            self.logger.info("Excitation session %s axis %s: %d continuous blocks, "
                             "estimated %.6f s, observed %.6f s, minimum %.6f s",
                             session.id, session.axis, len(bands), measurement["estimated_duration"],
                             actual, measurement["minimum_duration"])
            if not valid_timing:
                raise RuntimeError(f"Excitation timing is invalid: observed {actual:.6f} s, "
                                   f"minimum at the programmed feed is {measurement['minimum_duration']:.6f} s; "
                                   "no recommendation will be produced")
            self.publish({"type": "progress", "session_id": session.id, "progress": 1})
            self._exchange(session, ["G4 P500"], timeout=15, monitor=capture.ensure_alive)
            capture.stop()
        except Exception:
            session.cancelled.set()
            raise
        finally:
            capture.abort()
            self._write_json(folder / "excitation.json", timing)
        # Restore before CPU-intensive analysis, so plotting failures cannot
        # leave the printer's shaping or acceleration configuration altered.
        self._restore(session)
        self._stage(session, "analyzing")
        analyzer = self._analyzer(session, folder / "capture.csv")
        analyzer.analyze()
        analyzer.generate_graphs()
        return self._analysis_result(session, analyzer)

    def _analysis_result(self, session, analyzer, backend="software", extra=None):
        payload = analyzer.get_plotly_data()
        for name, candidate in payload["shapers"].items():
            candidate["command"] = shaping_command(session.snapshot.mode, session.axis, name,
                candidate["shaper_freq"], analyzer.damping, analyzer.tolerance)
        return {**payload, "session_id": session.id, "connection": session.connection,
                "axis": session.axis, "command": analyzer.get_recommendation() if payload.get("best_shaper") else None,
                "damping": analyzer.damping, "tolerance": analyzer.tolerance,
                "firmware_mode": session.snapshot.mode, "quality": analyzer.quality,
                "warnings": analyzer.warnings, "profile": session.profile,
                "firmware_snapshot": asdict(session.snapshot),
                "backend": backend,
                "files": {"signal": "signal.png", "psd": "psd.png", "csv": "capture.csv",
                    **({"events": "protocol_events.json", "excitation": "excitation.json", "analysis": "analysis.json",
                        "metadata": "capture.csv.meta.json", "sensor_log": "capture.csv.log"} if backend == "marlin_m495" else {})},
                **(extra or {})}

    @staticmethod
    def _application_candidate(result, name):
        allowed = SHAPER_IDS if result["firmware_mode"] == "ftm" else {"ZV": 1}
        if not isinstance(name, str) or name not in allowed:
            raise ValueError("The selected shaper is incompatible with the detected firmware")
        candidate = result.get("shapers", {}).get(name)
        if not isinstance(candidate, dict):
            raise ValueError("The selected shaper has no saved fit in this measurement")
        frequency = finite_number(candidate.get("shaper_freq"), "saved shaper frequency", 20, 200)
        return name, frequency

    def _apply(self, session):
        result = deepcopy(self.result)
        if result["profile"] != session.profile or result["firmware_mode"] != session.snapshot.mode:
            raise RuntimeError("The printer or firmware changed after this measurement")
        original = FirmwareSnapshot(**result["firmware_snapshot"])
        if not original.matches(session.snapshot):
            raise RuntimeError("Printer settings changed after calibration; run a new measurement")
        name, frequency = self._application_candidate(result, session.shaper)
        command = shaping_command(result["firmware_mode"], result["axis"], name,
                                  frequency, result["damping"], result["tolerance"])
        self._backup(session)
        self._exchange(session, ["M400", command])
        current = self._snapshot(session)
        axis_values = current.ftm_axes[result["axis"]] if current.mode == "ftm" else current.legacy_axes[result["axis"]]
        expected = FirmwareSnapshot()
        expected.feed(command)
        wanted = expected.ftm_axes[result["axis"]] if current.mode == "ftm" else expected.legacy_axes[result["axis"]]
        if not all(math.isclose(axis_values.get(k, -1), v, abs_tol=0.011) for k, v in wanted.items()):
            raise RuntimeError("Firmware did not confirm the selected settings")
        # Check that unrelated axes and global settings remained unchanged.
        verified = deepcopy(current)
        if current.mode == "ftm":
            verified.ftm_axes[result["axis"]] = session.snapshot.ftm_axes[result["axis"]]
        else:
            verified.legacy_axes[result["axis"]] = session.snapshot.legacy_axes[result["axis"]]
        if not session.snapshot.matches(verified):
            raise RuntimeError("An unrelated firmware setting changed while applying the result")
        self.journal.unlink(missing_ok=True)
        session.changed = False
        self.result = None
        self.publish({"type": "applied", "shaper": name, "frequency": frequency,
                      "message": f"{name} at {frequency:g} Hz confirmed by firmware. EEPROM was not written."})

    def _recover(self, session):
        with self.journal.open(encoding="utf-8") as stream:
            backup = json.load(stream)
        if backup["profile"] != session.profile:
            raise RuntimeError("Recovery belongs to a different printer profile")
        original = FirmwareSnapshot(**backup["snapshot"])
        original.validate(session.axis, allow_standard=True)
        if original.mode != session.snapshot.mode:
            raise RuntimeError("Recovery firmware protocol does not match this printer")
        session.snapshot, session.modes, session.changed = original, backup["modes"], True
        self._restore(session)

    def _run(self, session):
        held = False
        try:
            held = self.printer.set_job_on_hold(True, blocking=False)
            if not held:
                raise RuntimeError("Could not reserve printer communication for calibration")
            self._stage(session, "detecting firmware" if session.kind == "detect" else "preparing")
            if session.kind == "prepare":
                self._exchange(session, ["G21", "G90", "M82", "G1 F1500"])
                self.publish({"type": "completed", "message": "Coordinate modes prepared; no axis movement was commanded."})
            elif session.kind == "sensor":
                folder = self.data_folder / "runs" / session.id
                folder.mkdir(parents=True, exist_ok=False)
                samples, count = self._sensor_test(session, folder / "sensor_check.csv")
                self.publish({"type": "sensor_values", "samples": samples,
                              "summary": f"Captured {count} sensor samples"})
            elif session.kind == "detect_native":
                self._detect_native(session)
            elif session.kind == "recover" and self.native_unresolved:
                with self.journal.open(encoding="utf-8") as stream:
                    self._recover_native(session, json.load(stream))
                self.publish({"type": "completed", "message": "Native stop and printer settings verified. Prepare coordinate modes again."})
            else:
                session.snapshot = self._snapshot(session)
                if session.kind == "detect":
                    pass
                elif session.kind == "recover":
                    self._recover(session)
                    self.publish({"type": "completed", "message": "Saved firmware settings restored and verified."})
                elif session.kind == "apply":
                    self._apply(session)
                else:
                    folder = self.data_folder / "runs" / session.id
                    folder.mkdir(parents=True, exist_ok=False)
                    self._write_json(folder / "configuration.json", {"settings": session.settings,
                        "profile": session.profile, "firmware": asdict(session.snapshot), "modes": session.modes})
                    if session.kind == "axis":
                        self._motion_test(session, folder)
                        self.publish({"type": "completed", "message": f"{session.axis} axis test completed."})
                    else:
                        result = self._resonance(session, folder)
                        self._check(session)
                        self._write_json(folder / "result.json", result)
                        self.result = result
                        self.publish({"type": "results_ready", "result": result})
        except Exception as exc:
            if session.kind == "detect":
                with self.lock:
                    if session.cancelled.is_set() and session.connection == self.connection:
                        self.firmware = self._unknown_firmware()
                    elif self.firmware["state"] == "detecting":
                        self._set_firmware(session, error=str(exc))
            else:
                if session.kind == "detect_native" and session.connection == self.connection:
                    self.native_capability = {**self._unknown_native(), "state": "error", "message": str(exc)}
                    self.native_connection = None
                self.error = str(exc)
                self.result = None
                session.cancelled.set()
            if session.kind == "detect" and session.cancelled.is_set():
                self.logger.info("Firmware detection %s interrupted: %s", session.id, session.reason)
            else:
                self.logger.exception("Calibration session %s failed", session.id)
            if (not (session.native_mode and session.kind in ("resonance", "recover")) and session.changed
                    and (session.transport_fault or self.printer.get_state_id() not in ("OPERATIONAL", "OFFLINE", "CLOSED", "ERROR"))):
                self.printer.commands("M112", tags={TAG, RECOVERY_TAG})
        finally:
            if session.changed and not session.transport_fault:
                try:
                    self._restore(session)
                except Exception as exc:
                    self.error = f"{self.error or 'Calibration failed'}. Recovery required: {exc}"
                    self.logger.exception("Calibration restoration failed")
                    if self.printer.get_state_id() == "OPERATIONAL" and not (session.native_mode and session.kind in ("resonance", "recover")):
                        self.printer.commands("M112", tags={TAG, RECOVERY_TAG})
            self._native_log_transmissions(session)
            if held:
                try:
                    self.printer.set_job_on_hold(False)
                except Exception:
                    self.logger.exception("Could not release the printer hold")
            if session.native_mode and session.kind == "resonance":
                path = self.data_folder / "runs" / session.id / "excitation.json"
                if path.parent.exists():
                    try:
                        diagnostic = {"backend": "marlin_m495", "session_id": session.id,
                                      "axis": session.axis, "outcome": "failed before excitation"}
                        if path.exists():
                            with path.open(encoding="utf-8") as stream:
                                diagnostic = json.load(stream)
                        diagnostic["cleanup"] = {"settings_verified": not session.changed,
                                                 "recovery_required": self.journal.exists(), "error": self.error}
                        self._write_json(path, diagnostic)
                        self._write_json(path.parent / "protocol_events.json", session.native_events)
                        if self.error and not (path.parent / "analysis.json").exists():
                            self._write_json(path.parent / "analysis.json", {
                                "recommendation_valid": False, "error": self.error})
                        names = {"csv": "capture.csv", "metadata": "capture.csv.meta.json",
                                 "sensor_log": "capture.csv.log", "events": "protocol_events.json",
                                 "excitation": "excitation.json", "analysis": "analysis.json",
                                 "configuration": "configuration.json", "sensor_check": "sensor_check.csv"}
                        self.native_diagnostics = {"session_id": session.id,
                            "files": {key: name for key, name in names.items() if (path.parent / name).is_file()}}
                    except Exception:
                        self.logger.exception("Could not record native cleanup outcome")
            with self.lock:
                self.session = None
            self.publish({"type": "state", **self.status()})

    def shutdown(self):
        with self.lock:
            self._shutting_down = True
        self.cancel("OctoPrint is shutting down")
        if self.thread:
            self.thread.join(timeout=4)
            session = self.session
            if self.thread.is_alive() and session and session.changed and not (session.native_mode and session.kind in ("resonance", "recover")):
                self.printer.commands("M112", tags={TAG, RECOVERY_TAG})
