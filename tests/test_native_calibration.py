from dataclasses import asdict
import json
import logging
import queue
import re
import types

import pytest

import octoprint_Pinput_Shaping.native as native_module
from octoprint_Pinput_Shaping.native_protocol import DEFAULTS, NativeParameters
from fakes import Capture
from test_calibration import controller, run
from test_native_analysis import sweep


@pytest.fixture
def native(controller, settings, monkeypatch):
    settings.update(DEFAULTS, excitationBackend="marlin_m495", sensorType="lis2dw", lis2dwRate=800)
    clock = controller.printer.clock
    monkeypatch.setattr(native_module, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    original_get = controller.inbox.get
    def get(**kwargs):
        try:
            return original_get(block=False)
        except queue.Empty:
            clock.advance(0.25)
            raise
    controller.inbox.get = get
    controller.scenario = "success"
    controller.query = "echo:PIS:RT:READY:1"
    controller.query2 = "echo:PIS:RT:READY:2"
    controller.native_id = None
    controller.arm_sent = controller.sensor_verified = controller.fail_after_arm = False
    controller.native_terminal = False
    controller.capture_started = False
    controller.commands_during_native = []

    class NativeCapture(Capture):
        def prepare(self):
            assert not controller.capture_started

        def wait_ready(self):
            self.ensure_alive()
            if controller.scenario == "stalled_readiness":
                raise RuntimeError("Accelerometer did not produce new timestamped samples before ARM")
            controller.sensor_verified = True
            return {"observations": 2, "previous_last_timestamp": 1, "last_timestamp": 2}

        def start(self):
            if self.path.name == "capture.csv":
                assert controller.native_id is not None, "Capture must wait until preparation has completed"
                controller.capture_started = True
                if controller.scenario == "failed_start":
                    raise RuntimeError("Accelerometer failed before becoming ready")
                if controller.scenario == "slow_start":
                    clock.advance(20)
                count = sweep(self.path, rate=100 if controller.scenario == "low_rate" else int(self.rate))
            else:
                super().start()
                count = 6400
            if self.range_g is None:
                return  # Legacy ADXL wrappers produce CSV without a LIS sidecar.
            metadata = {"protocol": 2, "valid": not (controller.scenario == "bad_metadata" and self.path.name == "capture.csv"),
                "requested_rate": 800, "effective_rate": 800, "samples": count, "max_window_us": 20,
                "units": "g", "counts_per_g": 16384, "clock": "rp2040_fifo_poll"}
            if "lis2dwRange" in settings:
                metadata.update(requested_range_g=int(self.range_g), range_g=int(self.range_g),
                                counts_per_g=32768 // int(self.range_g))
            self.path.with_name(self.path.name + ".meta.json").write_text(json.dumps(metadata))
            if controller.scenario == "incomplete_csv" and self.path.name == "capture.csv":
                self.path.write_text("\n".join(self.path.read_text().splitlines()[:-100]) + "\n")

        def stop(self):
            if controller.scenario == "save_failure" and self.path.name == "capture.csv":
                raise RuntimeError("Accelerometer exited with code 2 while saving")
            super().stop()

        def ensure_alive(self):
            super().ensure_alive()
            if controller.scenario == "sensor_clipping_preparation" and self.path.name == "capture.csv" and controller.native_id:
                raise RuntimeError("Accelerometer exited during measurement (lis2dwusb, exit code 2, device error: CLIPPING)")
            if ((controller.scenario == "sensor_exit" and controller.native_id) or controller.fail_after_arm) and self.path.name == "capture.csv":
                raise RuntimeError("Accelerometer exited during measurement (device error: CLIPPING)")

    controller.capture_factory = NativeCapture
    original = controller.printer.commands
    def commands(lines, tags=None, force=False):
        if isinstance(lines, str):
            lines = [lines]
        for line in lines:
            if controller.native_id and not controller.native_terminal:
                controller.commands_during_native.append(line)
            if ":native-query" in line and controller.query == "timeout":
                continue
            if ":native-stop" in line and controller.scenario == "cancel_no_fence":
                continue
            original([line], tags=tags, force=force)
            if line == "M495 Q":
                if controller.query == "disconnect":
                    controller.printer.state = "OFFLINE"
                    controller.event("Disconnected")
                elif controller.query != "timeout":
                    controller.printer.emit(controller.query)
            elif line == "M495 Q2":
                if controller.query2 != "timeout":
                    controller.printer.emit(controller.query2)
            elif line.startswith("M495 ") and " I" in line and line.endswith(" S W"):
                assert not controller.capture_started, "Acquisition must not run during native preparation"
                identifier = int(re.search(r" I([0-9]+) S W$", line)[1])
                controller.native_id = identifier
                controller.native_axis = line.split()[1]
                if controller.scenario == "cancel_preparation":
                    controller.cancel()
                    continue
                if controller.scenario == "no_start":
                    continue
                if controller.scenario == "error_before":
                    controller.printer.emit(f"echo:PIS:RT:ERROR:{identifier}:MOTION_FAILED")
                    controller.native_terminal = True
                    continue
                if controller.scenario == "aborted_before":
                    controller.printer.emit(f"echo:PIS:RT:ABORTED:{identifier}")
                    controller.native_terminal = True
                    continue
                clock.advance(40 if controller.scenario == "delayed" else 2)
                controller.printer.emit(f"echo:PIS:RT:HOMED:{identifier}:120000")
                controller.printer.emit(f"echo:PIS:RT:ARMED_WAIT:{identifier}")
                if controller.scenario == "duplicate_wait":
                    controller.printer.emit(f"echo:PIS:RT:ARMED_WAIT:{identifier}")
                if controller.scenario == "timeout_during_startup":
                    controller.printer.emit(f"echo:PIS:RT:ERROR:{identifier}:ARM_TIMEOUT")
                    controller.native_terminal = True
                if controller.scenario == "cancel_wait":
                    controller.cancel()
            elif re.fullmatch(r"M495 I[0-9]+ R", line):
                assert controller.capture_started and controller.sensor_verified
                identifier = int(re.search(r"I([0-9]+)", line)[1])
                assert identifier == controller.native_id
                controller.arm_sent = True
                p = NativeParameters.from_settings(settings)
                if controller.scenario == "arm_rejected":
                    controller.printer.emit(f"echo:PIS:RT:ARM_REJECTED:{identifier}:NOT_WAITING")
                    continue
                if controller.scenario != "sensor_fail_before_ack":
                    controller.printer.emit(f"echo:PIS:RT:ARM_ACK:{identifier}")
                if controller.scenario in ("sensor_fail_before_start", "sensor_fail_before_ack"):
                    controller.fail_after_arm = True
                    continue
                axis = "Y" if controller.scenario == "wrong_axis" else controller.native_axis
                start = f"echo:PIS:RT:START:{identifier}:{axis}:{p.start:.2f}:{p.end:.2f}"
                controller.printer.emit("echo:busy: processing")
                controller.printer.emit(start)
                if controller.scenario == "duplicate_ack":
                    controller.printer.emit(f"echo:PIS:RT:ARM_ACK:{identifier}")
                if controller.scenario == "duplicate_start":
                    controller.printer.emit(start)
                if controller.scenario == "cancel_excitation":
                    controller.cancel()
                    continue
                if controller.scenario == "disconnect":
                    controller.printer.state = "OFFLINE"
                    controller.event("Disconnected")
                    continue
                if controller.scenario in ("no_complete", "cancel_timeout", "cancel_no_fence"):
                    continue
                clock.advance(p.duration)
                controller.printer.emit(f"echo:PIS:RT:COMPLETE:{identifier}")
                controller.native_terminal = True
                if controller.scenario == "duplicate_terminal":
                    controller.printer.emit(f"echo:PIS:RT:COMPLETE:{identifier}")
            elif line == "M496" and controller.native_id and not controller.native_terminal:
                if controller.scenario in ("sensor_fail_before_start", "sensor_fail_before_ack"):
                    identifier = controller.native_id
                    p = NativeParameters.from_settings(settings)
                    if controller.scenario == "sensor_fail_before_ack":
                        controller.printer.emit(f"echo:PIS:RT:ARM_ACK:{identifier}")
                    controller.printer.emit(f"echo:PIS:RT:START:{identifier}:{controller.native_axis}:{p.start:.2f}:{p.end:.2f}")
                    clock.advance(0.017)
                if controller.scenario != "cancel_timeout":
                    controller.printer.emit(f"echo:PIS:RT:ABORTED:{controller.native_id}")
                    controller.native_terminal = True
                clock.advance(2)  # Queued M496 acknowledgment precedes its fence.
    controller.printer.commands = commands
    return controller


def detect(native):
    run(native, "detect_native")
    assert native.native_capability["state"] == "supported"


@pytest.mark.parametrize("reply,state", [("echo:PIS:RT:READY:1", "supported"),
    ('Error:Unknown command: "M495 Q"', "unsupported"), ("M495 Resonance Test", "unsupported"),
    ("echo:PIS:RT:READY:2", "incompatible_version"), ("echo:PIS:RT:READY:bad", "error"), ("timeout", "timeout")])
def test_manual_native_detection_is_read_only_and_distinguishes_failures(native, reply, state):
    native.query = reply
    run(native, "detect_native")
    assert native.native_capability["state"] == state
    assert native.native_capability["message"]
    assert all(cmd in ("M495 Q", "M495 Q2") or cmd.startswith("M118 ") for cmd in native.printer.sent_commands)
    assert not native.printer.hold


def test_native_without_confirmed_support_fails_before_any_printer_command(native):
    with pytest.raises(RuntimeError, match="Detect M495"):
        native.submit("resonance", {"axis": "X"})
    assert native.printer.sent_commands == []


def test_manual_query_records_queued_command_actual_transmission_and_ready(native, caplog):
    with caplog.at_level(logging.INFO):
        detect(native)
    assert "native QUEUED: M495 Q" in caplog.text
    assert any("native TX" in record.message and "M495 Q" in record.message for record in caplog.records)
    assert any("native RX" in record.message and "echo:PIS:RT:READY:1" in record.message for record in caplog.records)


@pytest.mark.parametrize("scenario", ["sensor_fail_before_start", "sensor_fail_before_ack"])
def test_sensor_failure_after_arm_before_start_requires_terminal_and_stop_fence(native, caplog, scenario):
    detect(native)
    native.scenario = scenario
    with caplog.at_level(logging.INFO):
        identifier = run(native)
    assert "CLIPPING" in native.error
    assert native.result is None and not native.journal.exists()
    diagnostic = json.loads((native.data_folder / "runs" / identifier / "excitation.json").read_text())
    assert diagnostic["failure_stage"] == "arming native sweep"
    assert diagnostic["start_observed_before_failure"] is False
    assert diagnostic["host_arm_requested"] is not None
    assert diagnostic["host_start"] is not None
    assert diagnostic["motion_stopped_verified"]
    assert diagnostic["terminal"]["kind"] == "ABORTED"
    assert native.arm_sent and "M496" in native.printer.sent_commands
    assert "native stop confirmed: terminal=ABORTED; queued M496 fence received" in caplog.text


def test_clipping_before_arm_cancels_without_authorizing_excitation(native):
    detect(native)
    native.scenario = "sensor_clipping_preparation"
    run(native)
    assert "CLIPPING" in native.error
    assert not native.arm_sent
    assert not native.journal.exists()
    assert "M496" in native.printer.sent_commands


@pytest.mark.parametrize("data", [{"backend": "software"}, {"native_settings": {"nativeAccel": 41}}])
def test_unsaved_backend_or_native_parameters_are_rejected_before_motion(native, data):
    with pytest.raises(ValueError, match="Save"):
        native.submit("resonance", {"axis": "X", **data})
    assert native.printer.sent_commands == []


def test_detected_native_capability_is_invalidated_by_reconnect_and_identity_change(native):
    detect(native)
    native.event("Connected")
    assert native.native_capability["state"] == "unknown"
    detect(native)
    native.receive("FIRMWARE_NAME:old")
    native.receive("FIRMWARE_NAME:new")
    assert native.native_capability["state"] == "unknown"


@pytest.mark.parametrize("scenario", ["success", "delayed", "slow_start", "duplicate_ack"])
def test_native_success_requires_complete_and_valid_metadata_with_no_g1_excitation(native, snapshot, scenario):
    detect(native)
    native.scenario = scenario
    identifier = run(native)
    assert native.error is None
    assert native.result["backend"] == "marlin_m495"
    assert native.result["recommendation_valid"]
    assert abs(native.result["base_freq"] - 40) < 1.5
    assert snapshot.matches(native.printer.snapshot)
    assert not native.journal.exists()
    assert not any(cmd.startswith(("G1", "G28", "M204", "M205", "M493 X", "M493 Y")) for cmd in native.printer.sent_commands)
    assert not any(cmd == "M400" for cmd in native.commands_during_native)
    events = json.loads((native.data_folder / "runs" / identifier / "protocol_events.json").read_text())
    assert [item["event"]["kind"] for item in events] == ["HOMED", "ARMED_WAIT", "ARM_ACK", "START"] + (["ARM_ACK"] if scenario == "duplicate_ack" else []) + ["COMPLETE"]
    assert not native.printer.hold


@pytest.mark.parametrize("full_scale", [2, 4, 8, 16])
def test_native_range_selection_preserves_pis_v2_lifecycle(native, settings, full_scale, caplog):
    settings["lis2dwRange"] = full_scale
    detect(native)
    with caplog.at_level(logging.INFO):
        identifier = run(native)
    assert native.error is None
    assert native.result["recommendation_valid"]
    assert native.result["quality"]["accelerometer"]["range_g"] == full_scale
    folder = native.data_folder / "runs" / identifier
    diagnostic = json.loads((folder / "excitation.json").read_text())
    analysis = json.loads((folder / "analysis.json").read_text())
    assert analysis["quality"]["accelerometer"]["counts_per_g"] == 32768 // full_scale
    assert diagnostic["requested_range_g"] == full_scale
    assert diagnostic["metadata"]["range_g"] == full_scale
    assert [event["event"]["kind"] for event in diagnostic["events"]] == [
        "HOMED", "ARMED_WAIT", "ARM_ACK", "START", "COMPLETE"]
    assert f"actual range=+/-{full_scale} g, counts_per_g={32768 // full_scale}, units=g" in caplog.text
    assert native.sensor_verified and native.arm_sent
    assert not any(cmd.startswith(("G1", "G28")) for cmd in native.printer.sent_commands)


@pytest.mark.parametrize("sensor", ["adxlspi", "adxlusb"])
@pytest.mark.parametrize("rate", [800, 1600, 3200])
def test_native_adxl_accepts_legacy_csv_and_preserves_arming(native, settings, snapshot, sensor, rate):
    settings.update(sensorType=sensor, adxlRate=rate, lis2dwRate=1600, lis2dwRange=16)
    detect(native)
    identifier = run(native)
    assert native.error is None
    assert native.result["recommendation_valid"]
    assert abs(native.result["base_freq"] - 40) < 1.5
    assert native.result["quality"]["sampling_rate"] == pytest.approx(rate)
    assert native.sensor_verified and native.arm_sent
    folder = native.data_folder / "runs" / identifier
    diagnostic = json.loads((folder / "excitation.json").read_text())
    assert diagnostic["sensor"] == sensor and diagnostic["requested_rate"] == rate
    assert diagnostic["metadata"] == {"present": False}
    assert not (folder / "capture.csv.meta.json").exists()
    assert "metadata" not in native.result["files"], "Legacy ADXL must not advertise a missing LIS sidecar"
    assert [item["event"]["kind"] for item in diagnostic["events"]] == [
        "HOMED", "ARMED_WAIT", "ARM_ACK", "START", "COMPLETE"]
    assert snapshot.matches(native.printer.snapshot)
    assert not any(cmd.startswith(("G1", "G28")) for cmd in native.printer.sent_commands)


@pytest.mark.parametrize("sensor", ["adxlspi", "adxlusb"])
@pytest.mark.parametrize("scenario", ["failed_start", "stalled_readiness", "sensor_fail_before_ack",
                                      "sensor_exit", "cancel_wait", "cancel_excitation", "save_failure"])
def test_native_adxl_failures_stop_without_recommendation(native, settings, snapshot, sensor, scenario):
    settings.update(sensorType=sensor, adxlRate=800)
    detect(native)
    native.scenario = scenario
    run(native)
    assert native.error and native.result is None
    if scenario == "save_failure":
        assert native.native_terminal, "The sweep already completed before saving failed"
        assert "M496" not in native.printer.sent_commands
    else:
        assert "M496" in native.printer.sent_commands
    assert snapshot.matches(native.printer.snapshot)
    assert not native.printer.hold and not native.journal.exists()
    assert not any(event["type"] == "results_ready" for event in native.events)


@pytest.mark.parametrize("scenario", ["no_start", "no_complete", "error_before", "aborted_before",
    "cancel_preparation", "cancel_excitation", "sensor_exit", "bad_metadata", "low_rate",
    "duplicate_start", "duplicate_terminal", "incomplete_csv", "save_failure", "failed_start",
    "stalled_readiness", "duplicate_wait", "arm_rejected", "cancel_wait", "timeout_during_startup"])
def test_failed_or_cancelled_native_sessions_cannot_publish_recommendations(native, scenario, snapshot):
    detect(native)
    native.scenario = scenario
    run(native)
    assert native.error is not None
    assert native.result is None
    assert not any(event["type"] == "results_ready" for event in native.events)
    assert not native.journal.exists()
    assert snapshot.matches(native.printer.snapshot)
    assert not native.printer.hold
    assert "M112" not in native.printer.sent_commands
    assert not any(event["type"] == "progress" and event["progress"] == 1 for event in native.events)
    diagnostic = native.status()["diagnostics"]
    assert diagnostic["files"]["events"] == "protocol_events.json"
    assert diagnostic["files"]["analysis"] == "analysis.json"


@pytest.mark.parametrize("scenario", ["cancel_timeout", "cancel_no_fence", "disconnect", "wrong_axis"])
def test_unresolved_native_stop_requires_explicit_recovery_and_blocks_new_operations(native, scenario):
    detect(native)
    native.scenario = scenario
    run(native)
    assert native.journal.exists()
    assert native.native_unresolved
    assert native.result is None
    assert "M112" not in native.printer.sent_commands
    with pytest.raises(RuntimeError, match="Restore|connected"):
        native.submit("sensor")
    assert native.queuing("G1 X100", "G1") == (None,)
    assert native.queuing("M110 N0", "M110") is None
    native.scenario = "success"
    native.printer.state = "OPERATIONAL"
    native.event("Connected")
    run(native, "recover", native_idle_confirmed=True)
    assert native.error is None
    assert not native.journal.exists()
    assert not native.native_unresolved


def test_query_disconnect_clears_capability_without_leaking_the_printer_hold(native):
    native.query = "disconnect"
    run(native, "detect_native")
    assert native.native_capability["state"] == "unknown"
    assert native.error
    assert not native.printer.hold


def test_a_stale_ready_from_a_previous_query_cannot_establish_support(native):
    native.inbox.put((native.printer.clock.monotonic(), "echo:PIS:RT:READY:1"))
    native.query = 'Error:Unknown command: "M495 Q"'
    run(native, "detect_native")
    assert native.native_capability["state"] == "unsupported"


def test_transport_change_invalidates_native_capability_without_probing(native):
    detect(native)
    sent = list(native.printer.sent_commands)
    native.printer.get_current_connection = lambda: ("OPERATIONAL", "/dev/another-printer", 250000, None)
    assert native.status()["native_capability"]["state"] == "unknown"
    assert native.printer.sent_commands == sent


def test_native_probe_cannot_remain_detecting_when_communication_cannot_be_reserved(native):
    native.printer.set_job_on_hold = lambda *args, **kwargs: False
    run(native, "detect_native")
    assert native.native_capability["state"] == "error"
    assert "reserve" in native.native_capability["message"]


def test_native_measurement_accepts_standard_motion_without_enabling_ftm(native):
    native.printer.snapshot.ftm_global["S"] = 0
    detect(native)
    run(native)
    assert native.error is None
    assert native.result["recommendation_valid"]
    assert native.printer.snapshot.ftm_global["S"] == 0
    assert any("FTM is disabled" in warning for warning in native.result["warnings"])


def test_native_y_session_keeps_axis_and_numeric_id_through_disconnect_recovery(native):
    detect(native)
    native.scenario = "disconnect"
    native.submit("resonance", {"axis": "Y"})
    native.thread.join(timeout=10)
    assert not native.thread.is_alive()
    backup = json.loads(native.journal.read_text())
    assert backup["axis"] == "Y"
    native.printer.state = "OPERATIONAL"
    native.event("Connected")
    native.scenario = "success"
    run(native, "recover")
    assert native.error is None
    assert not native.journal.exists()


@pytest.mark.parametrize("standard", [False, True])
def test_valid_native_recommendation_can_be_applied_only_by_explicit_action(native, standard):
    native.printer.snapshot.ftm_global["S"] = 0 if standard else 1
    detect(native)
    run(native)
    result = native.result
    before = list(native.printer.sent_commands)
    assert not any(command.startswith("M493 X C") for command in before)
    run(native, "apply", session_id=result["session_id"], shaper="ZV")
    assert native.error is None
    assert native.result is None
    assert native.printer.snapshot.ftm_axes["X"]["C"] == 1
    assert native.printer.snapshot.ftm_global["S"] == (0 if standard else 1)
    assert "M500" not in native.printer.sent_commands


def test_diagnostic_only_native_response_is_rejected_by_application_api(native):
    native.result = {"session_id": "a" * 32, "connection": native.connection,
                     "recommendation_valid": False}
    with pytest.raises(ValueError, match="no validated recommendation"):
        native.submit("apply", {"session_id": "a" * 32, "shaper": "ZV"})
    assert not native.printer.sent_commands


def test_saved_application_settings_in_standard_mode_can_be_recovered_without_native_probing(native):
    native.printer.snapshot.ftm_global["S"] = 0
    native._write_json(native.journal, {"snapshot": asdict(native.printer.snapshot),
        "modes": native.modes, "profile": native.profile(), "session_id": "a" * 32})
    run(native, "recover")
    assert native.error is None
    assert not native.journal.exists()
    assert native.printer.snapshot.ftm_global["S"] == 0
    assert not any(command.startswith("M495") for command in native.printer.sent_commands)


def test_no_terminal_after_reset_requires_explicit_operator_idle_confirmation(native):
    detect(native)
    native.scenario = "cancel_timeout"
    run(native)
    native.native_terminal = True  # A controller reset erased the active firmware ID.
    native.scenario = "success"
    run(native, "recover")
    assert native.error and native.journal.exists()
    run(native, "recover", native_idle_confirmed=True)
    assert native.error is None
    assert not native.journal.exists()


def test_v1_detection_disables_native_without_motion_and_allows_recovery(native):
    native.query2 = "echo:PIS:RT:ERROR:0:INVALID_PARAMETERS"
    run(native, "detect_native")
    assert native.native_capability["state"] == "legacy"
    assert native.native_capability["protocol"] == 1
    before = list(native.printer.sent_commands)
    with pytest.raises(RuntimeError, match="Detect M495"):
        native.submit("resonance", {"axis": "X"})
    assert native.printer.sent_commands == before
    assert all(cmd in ("M495 Q", "M495 Q2") or cmd.startswith("M118 ") for cmd in before)
    native._write_json(native.journal, {"native_only": True, "firmware_session_id": 123,
        "snapshot": asdict(native.printer.snapshot), "modes": native.modes, "profile": native.profile(),
        "session_id": "a" * 32, "axis": "X", "parameters": asdict(NativeParameters.from_settings({}))})
    native.native_id = 123
    native.native_terminal = False
    run(native, "recover")
    assert native.error is None and not native.journal.exists()
    assert not any(cmd.endswith(" R") or cmd.endswith(" S W") for cmd in native.printer.sent_commands)


def test_cancelled_or_reconnected_session_discards_queued_arm(native):
    from octoprint_Pinput_Shaping.native import TAG
    detect(native)
    native.scenario = "disconnect"
    run(native)
    assert native.journal.exists()
    tags = {TAG, "pinput:session:previous-session"}
    native.printer.state = "OPERATIONAL"
    native.event("Connected")
    assert native.sending(f"M495 I{native.native_id} R", "M495", tags) == (None,)
    assert native.native_capability["state"] == "unknown"


def test_health_is_rechecked_immediately_before_arm(native):
    detect(native)
    original_factory = native.capture_factory
    def factory(*args, **kwargs):
        capture = original_factory(*args, **kwargs)
        original_ensure = capture.ensure_alive
        def ensure():
            original_ensure()
            if native.sensor_verified and not native.arm_sent:
                raise RuntimeError("Sensor failed on the final pre-ARM health check")
        capture.ensure_alive = ensure
        return capture
    native.capture_factory = factory
    run(native)
    assert "pre-ARM health check" in native.error
    assert not native.arm_sent and "M496" in native.printer.sent_commands
    assert not native.journal.exists()


def test_conflicting_v2_query_error_cannot_establish_support(native):
    original_emit = native.printer.emit
    def emit(line):
        original_emit(line)
        if line == "echo:PIS:RT:READY:2":
            original_emit("echo:PIS:RT:ERROR:0:INVALID_PARAMETERS")
    native.printer.emit = emit
    run(native, "detect_native")
    assert native.native_capability["state"] != "supported"
    assert not native.native_unresolved


def test_native_prepares_coordinates_before_m495_without_capturing_during_preparation(native):
    detect(native)
    native.modes = dict(units=None, xyz=None, extruder=None, feed=None)
    before = len(native.printer.sent_commands)
    run(native)
    assert native.error is None
    commands = native.printer.sent_commands[before:]
    assert commands[:4] == ["G21", "G90", "M82", "G1 F1500"]
    assert commands.index("G21") < next(i for i, cmd in enumerate(commands) if cmd.endswith(" S W"))
    assert native.result["recommendation_valid"]


def test_native_analysis_results_and_warnings_are_logged_with_both_ids(native, caplog):
    detect(native)
    native.printer.snapshot.ftm_global["S"] = 0
    with caplog.at_level(logging.INFO):
        identifier = run(native)
    assert f"Calibration session {identifier} (firmware ID {native.native_id}) analysis complete:" in caplog.text
    assert "backend=marlin_m495, axis=X" in caplog.text
    assert f"recommended={native.result['best_shaper']}" in caplog.text
    assert "fitted ZV: frequency=" in caplog.text
    assert "analysis warning: FTM is disabled" in caplog.text


def test_native_diagnostic_only_analysis_logs_no_recommendation(native, caplog, monkeypatch):
    import sys
    original = sweep
    monkeypatch.setattr(sys.modules[__name__], "sweep", lambda path, **kwargs: original(path, gain=0, **kwargs))
    detect(native)
    with caplog.at_level(logging.INFO):
        run(native)
    assert native.error is None
    assert not native.result["recommendation_valid"]
    assert "recommendation_valid=False, recommended=none" in caplog.text
    assert "analysis warning: No confident interior resonance" in caplog.text


def test_manual_native_detection_persists_result_and_backend_notification(native):
    saved = []
    native.persist_detection = lambda kind, report: saved.append((kind, report)) or {"excitationBackend": "marlin_m495"}
    detect(native)
    assert saved == [("detect_native", native.native_capability)]
    assert any(event["type"] == "detection_saved" and event["settings"]["excitationBackend"] == "marlin_m495"
               for event in native.events)
