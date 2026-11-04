from copy import deepcopy
import json
import logging
import queue
import threading
import types

import pytest

import octoprint_Pinput_Shaping.calibration as module
from octoprint_Pinput_Shaping import PinputShapingPlugin
from octoprint_Pinput_Shaping.calibration import CalibrationController, TAG
from fakes import Analyzer, Capture, Clock, Printer


@pytest.fixture
def controller(tmp_path, snapshot, settings, profile, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(module, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    printer = Printer(snapshot, clock)
    events = []
    controller = CalibrationController(printer, lambda: deepcopy(profile), lambda: deepcopy(settings),
        tmp_path, events.append, capture_factory=Capture, analyzer_factory=Analyzer)
    printer.controller = controller
    def wait(session, seconds):
        controller._check(session)
        clock.advance(seconds)
    controller._wait = wait
    controller.modes = dict(units="G21", xyz="G90", extruder="M83", feed=1800)
    controller.events = events
    return controller


def run(controller, kind="resonance", **extra):
    data = dict(axis="X", start_x=110, start_y=110, start_z=10, **extra)
    identifier = controller.submit(kind, data)
    controller.thread.join(timeout=10)
    assert not controller.thread.is_alive()
    return identifier


def test_complete_session_restores_before_publishing_atomic_result(controller, snapshot):
    identifier = run(controller)
    status = controller.status()
    assert status["error"] is None
    assert status["result"]["session_id"] == identifier
    assert snapshot.matches(controller.printer.snapshot)
    assert not status["busy"] and not status["recovery_required"]
    assert not controller.printer.hold
    results = [e for e in controller.events if e["type"] == "results_ready"]
    assert len(results) == 1
    assert results[0]["result"]["best_shaper_freq"] == 50
    assert results[0]["result"]["damping"] == 0.05
    assert controller.printer.sent_commands.index("G1 Z10.00000 F600.000") < controller.printer.sent_commands.index("G1 X110.00000 Y110.00000 F1500.000")


def test_application_uses_saved_result_and_reads_back_only_target_axis(controller, settings):
    identifier = run(controller)
    untouched = deepcopy(controller.printer.snapshot.ftm_axes["Y"])
    settings["dampingRatio"] = "0.15"
    run(controller, "apply", session_id=identifier)
    assert controller.error is None
    assert controller.printer.snapshot.ftm_axes["X"]["A"] == 50
    assert controller.printer.snapshot.ftm_axes["X"]["I"] == 0.05
    assert controller.printer.snapshot.ftm_axes["Y"] == untouched
    assert controller.result is None
    assert not any(cmd.startswith("M500") for cmd in controller.printer.sent_commands)


def test_failed_application_rolls_back_and_never_reports_success(controller, snapshot):
    identifier = run(controller)
    controller.printer.reject_apply = True
    run(controller, "apply", session_id=identifier)
    assert "did not confirm" in controller.error
    assert snapshot.matches(controller.printer.snapshot)
    assert not controller.journal.exists()
    assert not any(e["type"] == "applied" for e in controller.events)


def test_disconnection_keeps_recovery_journal_and_reconnect_cannot_apply_stale_result(controller, snapshot):
    controller.printer.fail_on_move = True
    run(controller)
    assert controller.journal.exists()
    assert controller.result is None
    controller.printer.fail_on_move = False
    controller.printer.state = "OPERATIONAL"
    controller.event("Connected")
    with pytest.raises(RuntimeError, match="Restore"):
        controller.submit("sensor")
    run(controller, "recover")
    assert not controller.journal.exists()
    assert snapshot.matches(controller.printer.snapshot)
    assert controller.error is None


def test_impossible_motion_completion_prevents_analysis_and_restores_settings(controller, snapshot):
    controller.printer.distortion = 0
    identifier = run(controller)
    assert "timing" in controller.error
    assert "minimum" in controller.error and "observed" in controller.error
    assert controller.result is None
    assert snapshot.matches(controller.printer.snapshot)
    timing = json.loads((controller.data_folder / "runs" / identifier / "excitation.json").read_text())
    assert timing[0]["valid_timing"] is False
    assert timing[0]["actual_duration"] + 0.02 < timing[0]["minimum_duration"]


@pytest.mark.parametrize("duration_factor", [0.415484746, 2])
def test_estimated_motion_duration_does_not_claim_a_commanded_frequency(controller, duration_factor):
    controller.printer.distortion = duration_factor
    identifier = run(controller)
    assert controller.error is None
    assert controller.result is not None
    timing = json.loads((controller.data_folder / "runs" / identifier / "excitation.json").read_text())
    assert all(record["kind"] == "acceleration_sweep" and "frequency" not in record for record in timing)
    assert all(record["valid_timing"] for record in timing)


def test_plotting_failure_cannot_leave_shapers_disabled(controller, snapshot):
    class BrokenPlots(Analyzer):
        def generate_graphs(self):
            assert snapshot.matches(controller.printer.snapshot)
            raise RuntimeError("disk full")
    controller.analyzer_factory = BrokenPlots
    run(controller)
    assert controller.error == "disk full"
    assert not controller.journal.exists()
    assert controller.result is None


def test_sensor_failure_prevents_homing_and_any_setting_mutation(controller):
    class BrokenCapture(Capture):
        def start(self):
            raise RuntimeError("sensor unplugged")
    controller.capture_factory = BrokenCapture
    run(controller)
    assert controller.error == "sensor unplugged"
    assert "G28" not in controller.printer.sent_commands
    assert "M493 X C0" not in controller.printer.sent_commands


def test_invalid_sensor_timestamps_fail_before_motion(controller):
    class BadTimestamps(Analyzer):
        def load_data(self):
            raise ValueError("duplicate sensor times")
    controller.analyzer_factory = BadTimestamps
    run(controller)
    assert controller.error == "duplicate sensor times"
    assert "G28" not in controller.printer.sent_commands


def test_concurrent_requests_do_not_replace_session_or_settings(controller):
    entered, release = threading.Event(), threading.Event()
    class PausedCapture(Capture):
        def start(self):
            entered.set()
            assert release.wait(5)
            super().start()
    controller.capture_factory = PausedCapture
    identifier = controller.submit("resonance", dict(axis="X", start_x=110, start_y=110, start_z=10))
    assert entered.wait(5)
    with pytest.raises(RuntimeError, match="Another"):
        controller.submit("sensor")
    assert controller.session.id == identifier
    controller.cancel()
    release.set()
    controller.thread.join(5)
    assert not controller.status()["busy"]
    assert controller.result is None


def test_external_motion_is_blocked_but_emergency_stop_and_temperature_queries_are_allowed(controller):
    entered, release = threading.Event(), threading.Event()
    class PausedCapture(Capture):
        def start(self):
            entered.set()
            release.wait(5)
            super().start()
    controller.capture_factory = PausedCapture
    controller.submit("sensor")
    assert entered.wait(5)
    assert controller.queuing("M105", "M105") is None
    assert controller.queuing("M112", "M112") is None
    assert controller.queuing("G1 X200", "G1") == (None,)
    assert controller.session.cancelled.is_set()
    release.set()
    controller.thread.join(5)


def test_serial_hook_only_enqueues_without_starting_capture(controller):
    controller.session = types.SimpleNamespace(cancelled=threading.Event())
    line = "Pinput_Shaping: Accelerometer|ON"
    assert controller.receive(line) == line
    assert controller.inbox.get_nowait()[1] == line
    assert controller.printer.sent_commands == []
    controller.session = None


def test_normal_host_monitoring_and_empty_lines_do_not_cancel_homing(controller):
    session = types.SimpleNamespace(id="test", kind="resonance", stage="homing", cancelled=threading.Event())
    controller.session = session
    for cmd in ("M105", "M155 S2", "M154 S1", "M113 S2", "M73 P0", "M117 Homing", "M400", "", "; comment"):
        gcode = cmd.split()[0] if cmd else None
        assert controller.queuing(cmd, gcode, {"trigger:printer.commands"}) is None
    assert not session.cancelled.is_set()
    controller.session = None


def test_mutating_foreign_command_is_identified_in_cancellation_and_log(controller, caplog):
    session = types.SimpleNamespace(id="test", kind="resonance", stage="homing", cancelled=threading.Event())
    controller.session = session
    assert controller.queuing("G92 X0", "G92", {"plugin:other"}) == (None,)
    assert session.cancelled.is_set()
    assert "G92" in session.reason
    assert "G92 X0" in caplog.text and "plugin:other" in caplog.text
    controller.session = None


def test_unknown_modes_and_invalid_positions_are_rejected_before_thread_start(controller):
    controller.modes["xyz"] = None
    with pytest.raises(RuntimeError, match="coordinate"):
        controller.submit("axis", dict(axis="X", start_x=110, start_y=110, start_z=10))
    assert controller.thread is None


def test_settings_are_frozen_for_the_session(controller, settings):
    run(controller)
    result = deepcopy(controller.result)
    settings["dampingRatio"] = "0.15"
    assert controller.result == result


def test_real_analyzer_and_plots_in_complete_simulated_session(controller):
    controller.analyzer_factory = None
    run(controller)
    assert controller.error is None
    assert abs(controller.result["base_freq"] - 50) < 1
    folder = controller.data_folder / "runs" / controller.result["session_id"]
    assert (folder / "psd.png").stat().st_size > 1000
    assert json.loads((folder / "result.json").read_text())["axis"] == "X"


def test_132_hz_analysis_range_with_1500_acceleration_can_fit_a_100_hz_signal(controller, settings):
    class HighFrequencyCapture(Capture):
        frequency = 100
    settings.update(freqStart=5, freqEnd=132, accelMax=1500)
    controller.capture_factory = HighFrequencyCapture
    controller.analyzer_factory = None
    run(controller)
    assert controller.error is None
    assert abs(controller.result["base_freq"] - 100) < 1


def test_runtime_plan_diagnostics_accept_numeric_settings_from_the_browser(controller, settings, caplog):
    settings.update(freqStart="5", freqEnd="132", accelMax="1500")
    with caplog.at_level("INFO"):
        run(controller)
    assert controller.error is None
    assert "analysis 5.000–132.000 Hz" in caplog.text
    assert "acceleration 300.000–1500.000 mm/s^2" in caplog.text


def test_cancel_during_motion_finishes_with_verified_restoration(controller, snapshot):
    def publish(event):
        controller.events.append(event)
        if event["type"] == "progress":
            controller.cancel()
    controller._publish_callback = publish
    run(controller)
    assert "cancelled" in controller.error
    assert snapshot.matches(controller.printer.snapshot)
    assert not controller.journal.exists()
    assert "M112" not in controller.printer.sent_commands


def test_lost_motion_confirmation_stops_printer_and_keeps_recovery(controller):
    commands = controller.printer.commands
    def lose_confirmation(lines, **kwargs):
        if "G28" in lines:
            controller.printer.silent = True
        return commands(lines, **kwargs)
    controller.printer.commands = lose_confirmation
    get = controller.inbox.get
    def advance_timeout(**kwargs):
        try:
            return get(block=False)
        except queue.Empty:
            controller.printer.clock.advance(300)
            raise
    controller.inbox.get = advance_timeout
    run(controller)
    assert "timed out" in controller.error
    assert "M112" in controller.printer.sent_commands
    assert controller.journal.exists()
    assert controller.result is None


def test_unverifiable_restoration_stops_printer_and_preserves_backup(controller):
    original = controller.printer.commands
    def reject_restore(lines, **kwargs):
        if controller.session.stage == "restoring":
            controller.printer.emit("?Invalid restoration setting")
        return original(lines, **kwargs)
    controller.printer.commands = reject_restore
    run(controller)
    assert "Recovery required" in controller.error
    assert controller.journal.exists()
    assert "M112" in controller.printer.sent_commands
    assert controller.result is None


def test_print_start_during_motion_cannot_continue_with_test_settings(controller):
    def start_print(event):
        if event["type"] == "progress":
            controller.printer.state = "PRINTING"
            controller.event("PrintStarted")
    controller._publish_callback = start_print
    run(controller)
    assert controller.printer.state == "ERROR"
    assert "M112" in controller.printer.sent_commands
    assert controller.journal.exists()


def test_notification_failure_does_not_skip_settings_restoration(controller, snapshot):
    def broken_notification(event):
        raise RuntimeError("websocket unavailable")
    controller._publish_callback = broken_notification
    run(controller)
    assert controller.error is None
    assert snapshot.matches(controller.printer.snapshot)
    assert not controller.journal.exists()


def test_legacy_zv_lifecycle_restores_and_applies_with_m593(controller):
    snapshot = controller.printer.snapshot
    snapshot.ftm_global, snapshot.ftm_axes = {}, {}
    snapshot.legacy_axes = {"X": {"F": 37.0, "D": 0.1}, "Y": {"F": 42.0, "D": 0.07}}
    original = deepcopy(snapshot)
    identifier = run(controller)
    assert controller.error is None
    assert original.matches(controller.printer.snapshot)
    assert controller.result["command"] == "M593 X F50.000 D0.0500"
    run(controller, "apply", session_id=identifier)
    assert controller.error is None
    assert controller.printer.snapshot.legacy_axes["X"] == {"F": 50.0, "D": 0.05}
    assert controller.printer.snapshot.legacy_axes["Y"] == original.legacy_axes["Y"]


def test_changed_limits_during_measurement_require_explicit_recovery(controller, snapshot):
    def change_limits(event):
        if event["type"] == "progress":
            controller.printer.snapshot.reports["M92"]["X"] = 160
    controller._publish_callback = change_limits
    run(controller)
    assert controller.result is None
    assert controller.journal.exists()
    controller.printer.state = "OPERATIONAL"
    controller.event("Connected")
    run(controller, "recover")
    assert controller.error is None
    assert snapshot.matches(controller.printer.snapshot)
    assert not controller.journal.exists()


def test_usb_sensor_cannot_open_the_printer_serial_port(controller, settings):
    settings["sensorType"] = "adxlusb"
    settings["sensorPort"] = "/dev/ttyACM1"
    controller.printer.get_current_connection = lambda: ("OPERATIONAL", "/dev/ttyACM1", 115200, None)
    run(controller)
    assert "must differ" in controller.error
    assert "G28" not in controller.printer.sent_commands


def test_startup_does_not_probe_an_idle_connected_printer(controller):
    plugin = PinputShapingPlugin()
    plugin._logger = logging.getLogger(__name__)
    plugin.configure_logger = lambda: None
    plugin._printer = controller.printer
    plugin._printer_profile_manager = types.SimpleNamespace(get_current_or_default=controller.profile)
    plugin._settings = types.SimpleNamespace(get=lambda path: controller.settings().get(path[0]))
    plugin._plugin_manager = types.SimpleNamespace(send_plugin_message=lambda *args: None)
    plugin._identifier = "Pinput_Shaping"
    plugin.get_plugin_data_folder = lambda: str(controller.data_folder)
    try:
        plugin.on_after_startup()
        assert plugin.controller.thread is None
        assert plugin.controller.status()["firmware"]["state"] == "unknown"
        assert controller.printer.sent_commands == []
        assert not controller.printer.hold
    finally:
        plugin.on_shutdown()


@pytest.mark.parametrize("state", ["OPERATIONAL", "PRINTING"])
def test_connection_and_idle_events_never_start_detection(controller, state):
    controller.printer.state = state
    for event in ("Connected", "PrinterStateChanged", "PrintDone", "PrintCancelled", "PrintFailed"):
        controller.event(event)
        assert controller.thread is None
        assert controller.status()["firmware"]["state"] == "unknown"
        assert controller.printer.sent_commands == []
        assert not controller.printer.hold


def test_requested_detection_reads_ftm_without_motion_or_setting_changes(controller, snapshot):
    controller.event("Connected")
    run(controller, "detect")
    status = controller.status()
    assert status["firmware"]["state"] == "ready"
    assert status["firmware"]["protocol"] == "ftm"
    assert set(status["firmware"]["allowed_shapers"]) == set(module.SHAPER_IDS)
    assert snapshot.matches(controller.printer.snapshot)
    assert not status["modes_ready"]
    assert not controller.journal.exists()
    assert not controller.printer.hold
    assert all(cmd in ("M400", "M503", "M220", "M114", "M493 V", "M494") or cmd.startswith("M118 ")
               for cmd in controller.printer.sent_commands)


def test_requested_legacy_detection_only_enables_zv(controller):
    snapshot = controller.printer.snapshot
    snapshot.ftm_global, snapshot.ftm_axes = {}, {}
    snapshot.legacy_axes = {"X": {"F": 37.0, "D": 0.1}, "Y": {"F": 42.0, "D": 0.07}}
    run(controller, "detect")
    assert controller.status()["firmware"]["protocol"] == "legacy"
    assert controller.status()["firmware"]["allowed_shapers"] == ["ZV"]
    assert not any(cmd.startswith(("M493", "M494", "M593")) for cmd in controller.printer.sent_commands)


def test_detection_requires_idle_and_a_new_request_after_reconnecting(controller):
    controller.printer.state = "PRINTING"
    controller.event("Connected")
    with pytest.raises(RuntimeError, match="connected and idle"):
        controller.submit("detect")
    assert controller.thread is None
    assert controller.status()["firmware"]["state"] == "unknown"
    controller.printer.state = "OPERATIONAL"
    controller.event("PrinterStateChanged")
    assert controller.thread is None
    run(controller, "detect")
    assert controller.status()["firmware"]["state"] == "ready"
    controller.printer.state = "OFFLINE"
    controller.event("Disconnected")
    assert controller.status()["firmware"]["allowed_shapers"] == []
    assert controller.status()["firmware"]["state"] == "unknown"
    commands = list(controller.printer.sent_commands)
    thread = controller.thread
    controller.printer.state = "OPERATIONAL"
    controller.event("Connected")
    assert controller.thread is thread
    assert controller.printer.sent_commands == commands
    assert controller.status()["firmware"]["state"] == "unknown"
    run(controller, "detect")
    assert controller.status()["firmware"]["state"] == "ready"


@pytest.mark.parametrize("field,value", [("ftm_version", 1), ("trajectory", 2)])
def test_incompatible_probe_disables_application_without_mutating_printer(controller, field, value):
    setattr(controller.printer.snapshot, field, value)
    run(controller, "detect")
    firmware = controller.status()["firmware"]
    assert firmware["state"] == "unsupported"
    assert firmware["allowed_shapers"] == []
    assert firmware["message"]
    assert not controller.journal.exists()
    assert "M112" not in controller.printer.sent_commands
    assert "G28" not in controller.printer.sent_commands


@pytest.mark.parametrize("interruption", ["external_command", "cancel", "reconnect"])
def test_interrupted_detection_never_retries_without_another_request(controller, monkeypatch, interruption):
    def unexpected_timer(*args, **kwargs):
        pytest.fail("Detection must not schedule automatic retries")
    monkeypatch.setattr(module.threading, "Timer", unexpected_timer)
    entered, release = threading.Event(), threading.Event()
    original = controller.printer.commands
    def paused(commands, **kwargs):
        if "M503" in commands:
            entered.set()
            assert release.wait(5)
        return original(commands, **kwargs)
    controller.printer.commands = paused
    controller.submit("detect")
    try:
        assert entered.wait(5)
        if interruption == "external_command":
            assert controller.queuing("G1 X120", "G1") is None
            assert "G1" in controller.session.reason
        elif interruption == "cancel":
            controller.cancel()
        else:
            controller.event("Disconnected")
            controller.event("Connected")
    finally:
        release.set()
        controller.thread.join(5)
    assert not controller.thread.is_alive()
    assert not controller.status()["busy"]
    assert controller.status()["firmware"]["state"] == "unknown"
    assert "M112" not in controller.printer.sent_commands
    commands = list(controller.printer.sent_commands)
    thread = controller.thread
    controller.event("PrinterStateChanged")
    assert controller.thread is thread
    assert controller.printer.sent_commands == commands
    controller.printer.commands = original
    run(controller, "detect")
    assert controller.status()["firmware"]["state"] == "ready"


def test_failed_detection_reservation_does_not_leave_ui_busy(controller):
    controller.printer.set_job_on_hold = lambda *args, **kwargs: False
    run(controller, "detect")
    assert not controller.status()["busy"]
    assert controller.status()["firmware"]["state"] == "unsupported"
    assert controller.status()["firmware"]["allowed_shapers"] == []


def test_missing_shaping_reports_disable_application_without_guessing_legacy(controller):
    original = controller.printer.commands
    controller.printer.commands = lambda commands, **kwargs: original(
        [command for command in commands if command != "M503"], **kwargs)
    run(controller, "detect")
    firmware = controller.status()["firmware"]
    assert firmware["state"] == "unsupported"
    assert firmware["protocol"] is None
    assert firmware["allowed_shapers"] == []
    assert "G28" not in controller.printer.sent_commands


def test_shutdown_rejects_detection_requests_and_events_cannot_start_a_probe(controller):
    controller.shutdown()
    controller.event("Connected")
    controller.event("PrinterStateChanged")
    with pytest.raises(RuntimeError, match="shutting down"):
        controller.submit("detect")
    assert controller.thread is None
    assert controller.printer.sent_commands == []


def test_application_rejects_manual_overrides(controller):
    identifier = run(controller)
    commands = list(controller.printer.sent_commands)
    for key in ("command", "frequency", "damping", "tolerance"):
        with pytest.raises(ValueError, match="overrides"):
            controller.submit("apply", {"session_id": identifier, key: "MZV"})
    assert controller.printer.sent_commands == commands


def test_legacy_application_rejects_advanced_recommendation_before_mutation(controller):
    snapshot = controller.printer.snapshot
    snapshot.ftm_global, snapshot.ftm_axes = {}, {}
    snapshot.legacy_axes = {"X": {"F": 37.0, "D": 0.1}, "Y": {"F": 42.0, "D": 0.07}}
    identifier = run(controller)
    controller.result["best_shaper"] = "MZV"
    start = len(controller.printer.sent_commands)
    with pytest.raises(ValueError, match="incompatible"):
        run(controller, "apply", session_id=identifier)
    assert not controller.journal.exists()
    assert not any(cmd.startswith(("M593", "M493")) for cmd in controller.printer.sent_commands[start:])


@pytest.mark.parametrize("name,identifier", module.SHAPER_IDS.items())
def test_manual_ftm_selection_uses_its_saved_fit_and_preserves_other_axis(controller, settings, name, identifier):
    measurement = run(controller)
    candidate = deepcopy(controller.result["shapers"][name])
    assert candidate["command"].startswith(f"M493 X C{identifier} ")
    untouched = deepcopy(controller.printer.snapshot.ftm_axes["Y"])
    settings["dampingRatio"] = "0.15"
    run(controller, "apply", session_id=measurement, shaper=name)
    assert controller.error is None
    applied = controller.printer.snapshot.ftm_axes["X"]
    assert applied["C"] == identifier
    assert applied["A"] == candidate["shaper_freq"]
    assert applied["I"] == 0.05
    assert controller.printer.snapshot.ftm_axes["Y"] == untouched
    assert any(e.get("shaper") == name and e.get("frequency") == candidate["shaper_freq"]
               for e in controller.events if e["type"] == "applied")
    assert not any(cmd.startswith("M500") for cmd in controller.printer.sent_commands)


@pytest.mark.parametrize("name", ["MZV", "EI", "ZVD", "3HUMP_EI"])
def test_legacy_rejects_manual_advanced_selection_before_any_command(controller, name):
    snapshot = controller.printer.snapshot
    snapshot.ftm_global, snapshot.ftm_axes = {}, {}
    snapshot.legacy_axes = {"X": {"F": 37.0, "D": 0.1}, "Y": {"F": 42.0, "D": 0.07}}
    measurement = run(controller)
    commands = list(controller.printer.sent_commands)
    with pytest.raises(ValueError, match="incompatible"):
        controller.submit("apply", {"session_id": measurement, "shaper": name})
    assert controller.printer.sent_commands == commands


def test_manual_selection_rejects_a_shaper_without_a_saved_fit(controller):
    measurement = run(controller)
    del controller.result["shapers"]["MZV"]
    with pytest.raises(ValueError, match="no saved fit"):
        controller.submit("apply", {"session_id": measurement, "shaper": "MZV"})


@pytest.mark.parametrize("name", ["UNKNOWN", None, {}, "ZV\nM112"])
def test_manual_selection_rejects_invalid_names_before_any_command(controller, name):
    measurement = run(controller)
    commands = list(controller.printer.sent_commands)
    with pytest.raises(ValueError, match="incompatible"):
        controller.submit("apply", {"session_id": measurement, "shaper": name})
    assert controller.printer.sent_commands == commands


def test_failed_manual_application_rolls_back_original_settings(controller, snapshot):
    measurement = run(controller)
    controller.printer.reject_apply = True
    run(controller, "apply", session_id=measurement, shaper="MZV")
    assert "did not confirm" in controller.error
    assert snapshot.matches(controller.printer.snapshot)
    assert not controller.journal.exists()
    assert not any(e["type"] == "applied" for e in controller.events)
