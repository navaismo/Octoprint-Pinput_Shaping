"""OctoPrint input-shaping calibration with exclusive, recoverable sessions."""

from pathlib import Path
import logging
from logging.handlers import TimedRotatingFileHandler
import re

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.server.util.flask import restricted_access

from .calibration import CalibrationController
from .native_protocol import DEFAULTS as NATIVE_DEFAULTS
from .capture import validate_sensor_settings


class PinputShapingPlugin(octoprint.plugin.StartupPlugin,
                          octoprint.plugin.ShutdownPlugin,
                          octoprint.plugin.EventHandlerPlugin,
                          octoprint.plugin.SimpleApiPlugin,
                          octoprint.plugin.SettingsPlugin,
                          octoprint.plugin.AssetPlugin,
                          octoprint.plugin.TemplatePlugin,
                          octoprint.plugin.BlueprintPlugin):
    def __init__(self):
        self.controller = None
        self._log_handler = None

    def configure_logger(self):
        if self._log_handler is not None:
            self._logger.removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None
        try:
            log_path = Path(self._settings.get_plugin_logfile_path()).with_name("Pinput_Shaping.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = TimedRotatingFileHandler(str(log_path), when="D", backupCount=3,
                                               encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
            handler.setLevel(logging.INFO)
            self._logger.addHandler(handler)
            self._log_handler = handler
            self._logger.info("Input-shaping diagnostics: %s", log_path)
        except OSError:
            self._logger.exception("Could not open Pinput_Shaping.log; using OctoPrint's main log")

    def get_settings_defaults(self):
        profile = self._printer_profile_manager.get_current_or_default()
        volume = profile["volume"]
        return {"sizeX": volume["width"], "sizeY": volume["depth"], "sizeZ": volume["height"],
                "accelMin": 300, "accelMax": 2500, "freqStart": 5, "freqEnd": 132,
                "sweepDuration": 20, "dampingRatio": "0.05",
                "sensorType": "adxlspi", "sensorAxis": "sum", "sensorPort": "", "adxlRate": 3200, "lis2dwRate": 800,
                "lis2dwRange": 16,
                "detectedFirmware": None, "detectedNative": None,
                **NATIVE_DEFAULTS}

    def get_settings_version(self):
        return 1

    def on_settings_migrate(self, target, current):
        if current is None or current < 1:
            saved = self._settings.get_all_data(merged=False) or {}
            # Preserve the former fixed range for existing saved configurations.
            # A new configuration uses the default; explicit ranges are retained.
            if "lis2dwRange" not in saved and any(not key.startswith("_") for key in saved):
                self._settings.set(["lis2dwRange"], 2)

    def on_settings_save(self, data):
        data = validate_sensor_settings(data)
        return octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

    def _save_detection(self, kind, result):
        # Saved detection reports are informational; live capabilities still
        # expire on reconnect and are never restored from these settings.
        key = "detectedNative" if kind == "detect_native" else "detectedFirmware"
        changes = {key: result}
        if kind == "detect_native" and result.get("state") == "supported" and result.get("protocol") == 2:
            changes["excitationBackend"] = "marlin_m495"
        for name, value in changes.items():
            self._settings.set([name], value)
        self._settings.save()
        self._logger.info("Detection settings saved: %s; backend=%s", key, self._settings.get(["excitationBackend"]))
        return changes

    def get_template_configs(self):
        return [
            dict(type="settings", template="settings_pinput_shaping_settings.jinja2",
                 name="Pinput Shaping", custom_bindings=True),
            dict(type="tab", template="pinput_shaping_tab.jinja2",
                 name="Input Shaping", custom_bindings=True),
        ]

    def get_assets(self):
        return {"js": ["js/pinput_shaping.js"], "css": ["css/pinput_shaping.css"]}

    def on_after_startup(self):
        self.configure_logger()
        keys = ("accelMin", "accelMax", "freqStart", "freqEnd", "sweepDuration",
                "dampingRatio", "sensorType", "sensorAxis", "sensorPort", "adxlRate", "lis2dwRate", "lis2dwRange", *NATIVE_DEFAULTS)
        self.controller = CalibrationController(
            self._printer, self._printer_profile_manager.get_current_or_default,
            lambda: {key: self._settings.get([key]) for key in keys},
            self.get_plugin_data_folder(),
            lambda payload: self._plugin_manager.send_plugin_message(self._identifier, payload),
            logger=self._logger,
            persist_detection=self._save_detection,
        )
        self._logger.info("Input shaping ready; firmware detection is available on demand")

    def on_shutdown(self):
        try:
            if self.controller:
                self.controller.shutdown()
        finally:
            if self._log_handler is not None:
                self._logger.removeHandler(self._log_handler)
                self._log_handler.close()
                self._log_handler = None

    def on_event(self, event, payload):
        if self.controller:
            self.controller.event(event)

    def get_api_commands(self):
        return {"run_axis_test": ["data"], "run_accelerometer_test": [],
                "run_resonance_test": ["data"], "prepare_modes": [],
                "cancel": [], "apply_result": ["session_id"], "recover": [], "detect_firmware": [], "detect_m495": []}

    def is_api_protected(self):
        return True

    def on_api_get(self, request):
        if not Permissions.STATUS.can():
            return flask.make_response("Status permission required", 403)
        if not self.controller:
            return flask.jsonify(busy=False, error="Plugin is starting")
        profile = self._printer_profile_manager.get_current_or_default()
        return flask.jsonify(**self.controller.status(), volume=profile["volume"])

    def on_api_command(self, command, data):
        if not Permissions.CONTROL.can():
            return flask.make_response("Printer control permission required", 403)
        if not self.controller:
            return flask.make_response("Plugin is starting", 503)
        operations = {"run_axis_test": "axis", "run_accelerometer_test": "sensor",
                      "run_resonance_test": "resonance", "prepare_modes": "prepare",
                      "apply_result": "apply", "recover": "recover", "detect_firmware": "detect", "detect_m495": "detect_native"}
        try:
            if command == "cancel":
                self.controller.cancel()
                return flask.jsonify(success=True)
            if command not in operations:
                return flask.jsonify(success=False, error="Unknown command"), 400
            # Simple API retains its routing command in the JSON body. Remove
            # only the matching envelope field before validating operation data.
            if data.get("command") == command:
                data = {key: value for key, value in data.items() if key != "command"}
            payload = data.get("data", {}) if command in ("run_axis_test", "run_resonance_test") else data
            if not isinstance(payload, dict):
                raise ValueError("Command data must be an object")
            if command == "apply_result" and ("session_id" not in data or set(data) - {"session_id", "shaper"}):
                raise ValueError("Select a saved shaper; command and parameter overrides are not accepted")
            session_id = self.controller.submit(operations[command], payload)
            return flask.jsonify(success=True, session_id=session_id), 202
        except ValueError as exc:
            return flask.jsonify(success=False, error=str(exc)), 400
        except RuntimeError as exc:
            return flask.jsonify(success=False, error=str(exc)), 409

    @octoprint.plugin.BlueprintPlugin.route("/files/<run_id>/<filename>", methods=["GET"])
    @restricted_access
    def run_file(self, run_id, filename):
        if not Permissions.STATUS.can():
            flask.abort(403)
        if not re.fullmatch(r"[a-f0-9]{32}", run_id) or filename not in (
                "signal.png", "psd.png", "capture.csv", "capture.csv.meta.json", "capture.csv.log",
                "protocol_events.json", "excitation.json", "analysis.json", "configuration.json",
                "sensor_check.csv", "sensor_check.csv.log", "sensor_check.csv.meta.json"):
            flask.abort(404)
        directory = Path(self.get_plugin_data_folder()) / "runs" / run_id
        return flask.send_from_directory(str(directory), filename,
                                         as_attachment=filename.endswith((".csv", ".json", ".log")))

    def gcode_received_handler(self, comm, line, *args, **kwargs):
        if self.controller:
            return self.controller.receive(line)
        return line

    def gcode_queuing_handler(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if self.controller:
            return self.controller.queuing(cmd, gcode, kwargs.get("tags"))
        return None

    def gcode_sent_handler(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if self.controller:
            self.controller.sent(cmd, gcode, kwargs.get("tags"))

    def gcode_sending_handler(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if self.controller:
            return self.controller.sending(cmd, gcode, kwargs.get("tags"))
        return None

    def get_update_information(self):
        return {"Pinput_Shaping": {
            "displayName": "Pinput Shaping", "displayVersion": self._plugin_version,
            "type": "github_release", "user": "navaismo", "repo": "OctoPrint-Pinput_Shaping",
            "current": self._plugin_version,
            "pip": "https://github.com/navaismo/OctoPrint-Pinput_Shaping/archive/{target_version}.zip",
        }}


__plugin_pythoncompat__ = ">=3.9,<3.13"
__plugin_version__ = "0.0.5"


def __plugin_load__():
    global __plugin_implementation__, __plugin_hooks__
    __plugin_implementation__ = PinputShapingPlugin()
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.gcode_received_handler,
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.gcode_queuing_handler,
        "octoprint.comm.protocol.gcode.sent": __plugin_implementation__.gcode_sent_handler,
        "octoprint.comm.protocol.gcode.sending": __plugin_implementation__.gcode_sending_handler,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    }
