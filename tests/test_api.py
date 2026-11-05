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
