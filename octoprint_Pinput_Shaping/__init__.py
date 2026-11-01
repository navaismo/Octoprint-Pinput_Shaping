"""OctoPrint input-shaping calibration with exclusive, recoverable sessions."""

from pathlib import Path
import re

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.server.util.flask import restricted_access

from .calibration import CalibrationController


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

    def get_settings_defaults(self):
        profile = self._printer_profile_manager.get_current_or_default()
        volume = profile["volume"]
        return {"sizeX": volume["width"], "sizeY": volume["depth"], "sizeZ": volume["height"],
                "accelMin": 300, "accelMax": 2500, "freqStart": 20, "freqEnd": 70,
                "freqStep": 2, "bandDuration": 1, "dampingRatio": "0.05",
                "sensorType": "adxlspi", "sensorAxis": "sum", "sensorPort": ""}

    def get_template_configs(self):
        return [
            dict(type="settings", template="settings_pinput_shaping_settings.jinja2",
                 name="Pinput Shaping", custom_bindings=True),
            dict(type="tab", template="pinput_shaping_tab.jinja2",
                 name="Input Shaping", custom_bindings=True),
        ]

    def get_assets(self):
        return {"js": ["js/pinput_shaping.js"]}

    def on_after_startup(self):
        keys = ("accelMin", "accelMax", "freqStart", "freqEnd", "freqStep",
                "bandDuration", "dampingRatio", "sensorType", "sensorAxis", "sensorPort")
        self.controller = CalibrationController(
            self._printer, self._printer_profile_manager.get_current_or_default,
            lambda: {key: self._settings.get([key]) for key in keys},
            self.get_plugin_data_folder(),
            lambda payload: self._plugin_manager.send_plugin_message(self._identifier, payload),
            logger=self._logger,
        )
        self.controller.detect_if_idle()
        self._logger.info("Input shaping ready; FTM v2 and legacy ZV protocols are verified at runtime")

    def on_shutdown(self):
        if self.controller:
            self.controller.shutdown()

    def on_event(self, event, payload):
        if self.controller:
            self.controller.event(event)

    def get_api_commands(self):
        return {"run_axis_test": ["data"], "run_accelerometer_test": [],
                "run_resonance_test": ["data"], "prepare_modes": [],
                "cancel": [], "apply_result": ["session_id"], "recover": [], "detect_firmware": []}

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
                      "apply_result": "apply", "recover": "recover", "detect_firmware": "detect"}
        try:
            if command == "cancel":
                self.controller.cancel()
                return flask.jsonify(success=True)
            if command not in operations:
                return flask.jsonify(success=False, error="Unknown command"), 400
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
        if not re.fullmatch(r"[a-f0-9]{32}", run_id) or filename not in ("signal.png", "psd.png", "capture.csv"):
            flask.abort(404)
        directory = Path(self.get_plugin_data_folder()) / "runs" / run_id
        return flask.send_from_directory(str(directory), filename,
                                         as_attachment=filename.endswith(".csv"))

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
            self.controller.sent(cmd, gcode)

    def get_update_information(self):
        return {"Pinput_Shaping": {
            "displayName": "Pinput Shaping", "displayVersion": self._plugin_version,
            "type": "github_release", "user": "navaismo", "repo": "OctoPrint-Pinput_Shaping",
            "current": self._plugin_version,
            "pip": "https://github.com/navaismo/OctoPrint-Pinput_Shaping/archive/{target_version}.zip",
        }}


__plugin_pythoncompat__ = ">=3.9,<3.13"
__plugin_version__ = "0.0.4.9"


def __plugin_load__():
    global __plugin_implementation__, __plugin_hooks__
    __plugin_implementation__ = PinputShapingPlugin()
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.gcode_received_handler,
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.gcode_queuing_handler,
        "octoprint.comm.protocol.gcode.sent": __plugin_implementation__.gcode_sent_handler,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    }
