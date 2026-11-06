from pathlib import Path
import inspect
import types

import flask
import pytest
from werkzeug.exceptions import NotFound

from octoprint_Pinput_Shaping import PinputShapingPlugin
from octoprint_Pinput_Shaping import Permissions


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(Permissions.STATUS, "can", lambda: True)
    monkeypatch.setattr(Permissions.CONTROL, "can", lambda: True)
    plugin = PinputShapingPlugin()
    plugin.get_plugin_data_folder = lambda: str(tmp_path)
    plugin.controller = types.SimpleNamespace(status=lambda: {"busy": False},
        submit=lambda kind, data: "a" * 32, cancel=lambda: None)
    plugin._printer_profile_manager = types.SimpleNamespace(get_current_or_default=lambda: {"volume": {}})
    app = flask.Flask(__name__)
    # Authentication belongs to OctoPrint's server. Exercise the plugin's
    # authorization and path restrictions without starting that server.
    route = inspect.unwrap(type(plugin).run_file)
    app.add_url_rule("/file/<run_id>/<filename>", view_func=lambda **kwargs: route(plugin, **kwargs))
    return app, plugin


def test_control_permission_is_required_for_every_mutating_api_action(api, monkeypatch):
    app, plugin = api
    monkeypatch.setattr(Permissions.CONTROL, "can", lambda: False)
    with app.test_request_context():
        for command in ("run_axis_test", "run_resonance_test", "apply_result", "recover", "cancel", "prepare_modes", "detect_firmware", "detect_m495"):
            assert plugin.on_api_command(command, {}).status_code == 403


def test_simple_api_explicitly_requires_authentication(api):
    _, plugin = api
    assert plugin.is_api_protected() is True


def test_status_permission_is_required_for_results_and_downloads(api, monkeypatch):
    app, plugin = api
    monkeypatch.setattr(Permissions.STATUS, "can", lambda: False)
    with app.test_request_context():
        assert plugin.on_api_get(flask.request).status_code == 403
    assert app.test_client().get("/file/" + "a" * 32 + "/capture.csv").status_code == 403


def test_downloads_are_scoped_to_known_artifacts(api, tmp_path):
    app, plugin = api
    folder = tmp_path / "runs" / ("a" * 32)
    folder.mkdir(parents=True)
    (folder / "capture.csv").write_text("time,x,y,z\n")
    client = app.test_client()
    response = client.get("/file/" + "a" * 32 + "/capture.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    response.close()
    for run, name in [("bad", "capture.csv"), ("a" * 32, "recovery_required.json"),
                       ("a" * 32, "configuration.json")]:
        assert client.get(f"/file/{run}/{name}").status_code == 404


def test_validation_failures_return_actionable_http_status(api):
    app, plugin = api
    with app.test_request_context():
        assert plugin.on_api_command("unknown", {})[1] == 400
        assert plugin.on_api_command("run_axis_test", {"data": "invalid"})[1] == 400
        def busy(kind, data):
            raise RuntimeError("Another session is active")
        plugin.controller.submit = busy
        response, code = plugin.on_api_command("run_axis_test", {"data": {}})
        assert code == 409
        assert "Another session" in response.get_json()["error"]


def test_api_accepts_a_shaper_name_and_rejects_parameter_overrides(api):
    app, plugin = api
    with app.test_request_context():
        for key in ("command", "frequency", "damping", "tolerance"):
            response, code = plugin.on_api_command("apply_result", {"session_id": "a" * 32, key: "MZV"})
            assert code == 400
            assert "overrides" in response.get_json()["error"]
        response, code = plugin.on_api_command("apply_result", {"session_id": "a" * 32})
        assert code == 202
        received = []
        plugin.controller.submit = lambda kind, data: received.append((kind, data)) or "b" * 32
        response, code = plugin.on_api_command("apply_result", {"session_id": "a" * 32, "shaper": "MZV"})
        assert code == 202
        assert received == [("apply", {"session_id": "a" * 32, "shaper": "MZV"})]


@pytest.mark.parametrize("selection", [{}, {"shaper": "MZV"}])
def test_apply_accepts_octoprint_command_envelope_without_forwarding_it(api, selection):
    app, plugin = api
    received = []
    plugin.controller.submit = lambda kind, data: received.append((kind, data)) or "b" * 32
    payload = {"command": "apply_result", "session_id": "a" * 32, **selection}
    with app.test_request_context(json=payload):
        # Use OctoPrint's real parser when available; its return data includes
        # the routing command, just as the browser's simpleApiCommand sends it.
        import octoprint.server.util.flask as api_util
        if hasattr(api_util, "get_json_command_from_request"):
            command, data, error = api_util.get_json_command_from_request(flask.request, plugin.get_api_commands())
            assert error is None
        else:
            command, data = payload["command"], flask.request.get_json()
        response, code = plugin.on_api_command(command, data)
    assert code == 202
    assert response.get_json()["success"]
    assert received == [("apply", {"session_id": "a" * 32, **selection})]
    assert payload["command"] == "apply_result"


@pytest.mark.parametrize("override", [{"frequency": 99}, {"damping": 0.1}, {"tolerance": 0.1},
                                     {"gcode": "M493 X C8 A99 I0.1"}, {"command": "M493 X C8 A99 I0.1"}])
def test_apply_command_envelope_cannot_bypass_saved_result_validation(api, override):
    app, plugin = api
    plugin.controller.submit = lambda *args: pytest.fail("Invalid application reached the controller")
    with app.test_request_context():
        response, code = plugin.on_api_command("apply_result", {
            "command": "apply_result", "session_id": "a" * 32, "shaper": "MZV", **override})
    assert code == 400
    assert "overrides" in response.get_json()["error"]


def test_native_detection_uses_a_separate_manual_api_operation(api):
    app, plugin = api
    received = []
    plugin.controller.submit = lambda kind, data: received.append((kind, data)) or "a" * 32
    with app.test_request_context():
        response, code = plugin.on_api_command("detect_m495", {})
        assert code == 202
        assert response.get_json()["session_id"] == "a" * 32
    assert received == [("detect_native", {})]


def test_native_defaults_preserve_software_backend_and_lis800(api):
    _, plugin = api
    plugin._printer_profile_manager.get_current_or_default = lambda: {"volume": dict(width=220, depth=220, height=250)}
    defaults = plugin.get_settings_defaults()
    assert defaults["excitationBackend"] == "software"
    assert defaults["lis2dwRate"] == 800
    assert defaults["lis2dwRange"] == 16
    assert [defaults[key] for key in ("nativeStart", "nativeEnd", "nativeOctave", "nativeAccel")] == [20, 60, 20, 40]


@pytest.mark.parametrize("filename", ["capture.csv.meta.json", "capture.csv.log", "protocol_events.json",
                                     "excitation.json", "analysis.json", "configuration.json", "sensor_check.csv"])
def test_native_diagnostics_downloads_use_authenticated_whitelisted_route(api, tmp_path, filename):
    app, _ = api
    folder = tmp_path / "runs" / ("a" * 32)
    folder.mkdir(parents=True)
    (folder / filename).write_text("{}")
    response = app.test_client().get("/file/" + "a" * 32 + "/" + filename)
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    response.close()


@pytest.mark.parametrize("kind,report,backend", [
    ("detect", {"state": "ready", "protocol": "ftm"}, "software"),
    ("detect_native", {"state": "supported", "protocol": 2}, "marlin_m495"),
    ("detect_native", {"state": "legacy", "protocol": 1}, "software"),
    ("detect_native", {"state": "unsupported", "protocol": None}, "software"),
])
def test_detection_persists_reports_and_selects_only_confirmed_native_v2(api, kind, report, backend):
    import logging
    _, plugin = api
    values, saved = {"excitationBackend": "software", "lis2dwRate": 800}, []
    plugin._settings = types.SimpleNamespace(
        set=lambda path, value: values.update({path[0]: value}),
        get=lambda path: values.get(path[0]), save=lambda: saved.append(dict(values)))
    plugin._logger = logging.getLogger(__name__)
    changes = plugin._save_detection(kind, report)
    key = "detectedFirmware" if kind == "detect" else "detectedNative"
    assert saved == [values]
    assert saved[0][key] == report
    assert saved[0]["excitationBackend"] == backend
    assert saved[0]["lis2dwRate"] == 800
    assert changes[key] == report


@pytest.fixture
def range_settings(api, tmp_path):
    import octoprint.plugin
    if not hasattr(octoprint.plugin, "PluginSettings"):
        pytest.skip("Requires the real OctoPrint settings manager")
    from octoprint.settings import Settings
    _, plugin = api
    plugin._printer_profile_manager.get_current_or_default = lambda: {"volume": dict(width=220, depth=220, height=250)}
    manager = Settings(configfile=str(tmp_path / "config.yaml"), basedir=str(tmp_path))
    plugin._settings = octoprint.plugin.PluginSettings(manager, "Pinput_Shaping", defaults=plugin.get_settings_defaults())
    return plugin, manager


@pytest.mark.parametrize("full_scale", [2, 4, 8, 16, "2", "4", "8", "16"])
def test_accelerometer_range_settings_validate_and_persist(range_settings, full_scale):
    plugin, manager = range_settings
    plugin.on_settings_save({"lis2dwRange": full_scale, "lis2dwRate": 800})
    plugin._settings.save()
    manager.load()
    assert plugin._settings.get(["lis2dwRange"]) == int(full_scale)
    assert plugin._settings.get(["lis2dwRate"]) == 800
    assert plugin._settings.get(["_config_version"]) == 1
    plugin.on_settings_migrate(1, 1)
    assert plugin._settings.get(["lis2dwRange"]) == int(full_scale)


@pytest.mark.parametrize("value", [True, None, 1, 3, 32, 16.5, "bad", float("nan")])
def test_invalid_range_setting_does_not_overwrite_saved_values(range_settings, value):
    plugin, _ = range_settings
    plugin.on_settings_save({"lis2dwRange": 8, "lis2dwRate": 800})
    with pytest.raises(ValueError, match="range"):
        plugin.on_settings_save({"lis2dwRange": value, "lis2dwRate": 1600})
    assert plugin._settings.get(["lis2dwRange"]) == 8
    assert plugin._settings.get(["lis2dwRate"]) == 800


@pytest.mark.parametrize("saved,expected", [
    ({}, 16), ({"sensorType": "lis2dw", "lis2dwRate": 800}, 2),
    ({"sensorType": "adxlusb"}, 2), ({"lis2dwRange": 4}, 4),
    ({"lis2dwRange": 8}, 8), ({"lis2dwRange": 16}, 16),
])
def test_range_migration_preserves_existing_configurations(range_settings, saved, expected):
    plugin, manager = range_settings
    plugin._settings.set([], saved)
    plugin.on_settings_migrate(1, None)
    plugin._settings.save()
    manager.load()
    assert plugin._settings.get(["lis2dwRange"]) == expected
    for key, value in saved.items():
        assert plugin._settings.get([key]) == value


@pytest.mark.parametrize("sensor", ["adxlspi", "adxlusb", "lis2dw"])
@pytest.mark.parametrize("axis", ["sum", "x", "y", "z"])
def test_sensor_configuration_persists_across_driver_changes(range_settings, sensor, axis):
    plugin, manager = range_settings
    for rate in (800, 1600, 3200):
        plugin.on_settings_save({"sensorType": sensor, "sensorAxis": axis, "adxlRate": str(rate),
                                 "lis2dwRate": 800, "lis2dwRange": 16,
                                 "sensorPort": "/dev/serial/by-id/usb-accelerometer"})
        plugin._settings.save()
        manager.load()
        for key, value in {"sensorType": sensor, "sensorAxis": axis, "adxlRate": rate,
                           "lis2dwRate": 800, "lis2dwRange": 16,
                           "sensorPort": "/dev/serial/by-id/usb-accelerometer"}.items():
            assert plugin._settings.get([key]) == value


@pytest.mark.parametrize("change", [{"sensorType": "unknown"}, {"sensorAxis": "XY"}, {"adxlRate": 400},
                                   {"adxlRate": True}, {"lis2dwRate": 3200}, {"sensorPort": None},
                                   {"sensorPort": "/tmp/sensor"}])
def test_invalid_sensor_settings_do_not_partially_save(range_settings, change):
    plugin, _ = range_settings
    assert plugin._settings.get(["adxlRate"]) == 3200
    with pytest.raises(ValueError):
        plugin.on_settings_save({"lis2dwRange": 4, **change})
    assert plugin._settings.get(["lis2dwRange"]) == 16
    assert plugin._settings.get(["adxlRate"]) == 3200
