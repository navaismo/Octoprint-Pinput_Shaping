"""Saved detection survives restarts without authorizing stale native motion."""

from copy import deepcopy

import pytest

from octoprint_Pinput_Shaping.calibration import CalibrationController
from test_calibration import controller, run
from test_native_calibration import native, detect


def save_reports(native):
    native.receive("FIRMWARE_NAME:Marlin test-build")
    run(native, "detect")
    detect(native)
    return deepcopy(native._saved_detection)


def test_restart_restores_reports_without_commands_or_background_detection(native, tmp_path):
    reports = save_reports(native)
    commands = list(native.printer.sent_commands)
    restored = CalibrationController(native.printer, native.profile, native.settings,
                                    tmp_path / "restart", lambda payload: None, saved_detection=reports)
    status = restored.status()
    assert status["firmware"]["state"] == "ready"
    assert status["native_capability"]["state"] == "supported"
    assert status["native_capability"]["protocol"] == 2
    assert status["native_capability"]["cached"] is True
    assert restored.thread is None
    assert native.printer.sent_commands == commands
    restored.receive("FIRMWARE_NAME:Marlin replacement-build")
    assert restored.status()["firmware"]["state"] == "unknown"
    assert restored.status()["native_capability"]["state"] == "unknown"


@pytest.mark.parametrize("change", ["port", "baudrate", "profile"])
def test_reports_are_not_restored_for_another_printer_target(native, tmp_path, change):
    reports = save_reports(native)
    profile = deepcopy(native.profile())
    connection = list(native.printer.get_current_connection())
    if change == "port":
        connection[1] = "/dev/another-printer"
    elif change == "baudrate":
        connection[2] = 250000
    else:
        profile["id"] = "another-printer"
    native.printer.get_current_connection = lambda: tuple(connection)
    restored = CalibrationController(native.printer, lambda: profile, native.settings,
                                    tmp_path / "restart", lambda payload: None, saved_detection=reports)
    assert restored.status()["native_capability"]["state"] == "unknown"
    assert restored.status()["firmware"]["state"] == "unknown"


@pytest.mark.parametrize("legacy_report", [False, True])
def test_first_requested_calibration_rechecks_saved_v2_before_preparation(native, legacy_report):
    reports = save_reports(native)
    if legacy_report:
        for report in reports.values():
            report.pop("context")
    native._saved_detection = reports
    native.printer.state = "OFFLINE"
    native.event("Disconnected")
    native.printer.state = "OPERATIONAL"
    before = len(native.printer.sent_commands)
    native.event("Connected")
    assert native.status()["native_capability"]["cached"] is True
    assert len(native.printer.sent_commands) == before
    run(native)
    assert native.error is None
    commands = native.printer.sent_commands[before:]
    assert commands.index("M495 Q2") < commands.index("G21")
    assert any(command.endswith(" S W") for command in commands)
    assert not native.native_capability.get("cached")
    # The following axis measurement on this connection reuses live support.
    native.capture_started = native.native_terminal = False
    before = len(native.printer.sent_commands)
    run(native, axis="Y")
    assert native.error is None
    assert not any(command in ("M495 Q", "M495 Q2") for command in native.printer.sent_commands[before:])


@pytest.mark.parametrize("reply", ["timeout", "echo:PIS:RT:ERROR:0:INVALID_PARAMETERS",
                                 'Error:Unknown command: "M495 Q2"'])
def test_failed_recheck_never_starts_capture_or_prepares_motion(native, reply):
    save_reports(native)
    native.event("Connected")
    native.query2 = reply
    before = len(native.printer.sent_commands)
    run(native)
    assert "could not be confirmed" in native.error
    assert not native.capture_started
    assert not native.journal.exists()
    assert not native.printer.hold
    commands = native.printer.sent_commands[before:]
    assert "M495 Q2" in commands
    assert not any(command.startswith(("G", "M496")) or command.endswith((" S W", " R"))
                   for command in commands)


def test_disconnect_during_cached_recheck_cannot_start_excitation(native):
    save_reports(native)
    native.event("Connected")
    native.query = "disconnect"
    before = len(native.printer.sent_commands)
    run(native)
    assert native.error
    assert not native.capture_started and not native.journal.exists()
    assert not native.printer.hold
    assert not any(command.endswith((" S W", " R")) for command in native.printer.sent_commands[before:])


@pytest.mark.parametrize("report", [None, [], "invalid", {}, {"state": "supported", "protocol": 1},
                                  {"state": "error", "protocol": 2}])
def test_invalid_saved_reports_cannot_enable_native_calibration(native, tmp_path, report):
    restored = CalibrationController(native.printer, native.profile, native.settings,
                                    tmp_path / "restart", lambda payload: None,
                                    saved_detection={"detectedNative": report, "detectedFirmware": None})
    assert restored.status()["native_capability"]["state"] == "unknown"
    assert restored.thread is None
