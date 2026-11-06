"""Native M495 worker operations; the serial hook only timestamps/enqueues."""

from dataclasses import asdict
import math
import queue
import shutil
import time

from .native_protocol import NativeParameters, NativeLifecycle, parse_event, new_firmware_id, ARM_TIMEOUT, SENSOR_START_BUDGET


TAG = "plugin:Pinput_Shaping"
RECOVERY = "pinput:recovery"


class NativeResonanceMixin:
    @staticmethod
    def _unknown_native(message="Click Detect M495 Support for this printer connection."):
        return {"state": "unknown", "protocol": None, "m495": None, "command": "M495", "message": message}

    def _native_send(self, session, commands, recovery=False):
        self._native_log_transmissions(session)
        tags = {TAG, f"pinput:session:{session.id}"}
        if recovery:
            tags.add(RECOVERY)
        for command in [commands] if isinstance(commands, str) else commands:
            if command.split()[0] in ("M495", "M496"):
                session.native_commands.append({"host_time": time.monotonic(), "command": command, "phase": "queued"})
                self.logger.info("Calibration session %s (firmware ID %s) native QUEUED: %s",
                                 session.id, session.firmware_id, command)
        self.printer.commands(commands, tags=tags, force=recovery)

    def _native_log_transmissions(self, session):
        while True:
            try:
                item = session.native_outgoing.get_nowait()
            except queue.Empty:
                return
            session.native_commands.append(item)
            self.logger.info("Calibration session %s (firmware ID %s) native TX [monotonic %.6f]: %s",
                             session.id, session.firmware_id, item["host_time"], item["command"])

    def _native_line(self, session, timeout=0.1):
        self._native_log_transmissions(session)
        try:
            stamp, line = self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None
        event = parse_event(line)
        if event or line.rstrip().startswith("echo:PIS:RT:"):
            session.native_events.append({"host_time": stamp, "line": line.rstrip(), "event": event})
            self.logger.info("Calibration session %s (firmware ID %s) native RX [monotonic %.6f]: %s",
                             session.id, session.firmware_id, stamp, line.rstrip())
        return stamp, line, event

    def _detect_native(self, session):
        self._stage(session, "detecting M495 support")
        self._detect_native_query(session, "M495 Q", 1)
        if self.native_capability["state"] == "supported":
            self._detect_native_query(session, "M495 Q2", 2)

    def _detect_native_query(self, session, command, version):
        opening = f"Pinput_Shaping:{session.id}:native-query-{version}-begin"
        marker = f"Pinput_Shaping:{session.id}:native-query-{version}"
        self._native_send(session, [f"M118 {opening}", command, f"M118 {marker}"])
        deadline = time.monotonic() + 10
        ready, fenced, unsupported, malformed = None, False, False, False
        opened, legacy = False, False
        state, message = "timeout", "M495 detection timed out; support is still unknown."
        try:
            while time.monotonic() < deadline:
                self._check(session)
                item = self._native_line(session)
                if item is None:
                    continue
                _, line, event = item
                text = line.strip()
                if not opened:
                    opened = text in (opening, "echo:" + opening)
                    continue
                unsupported |= "Unknown command:" in text and "M495" in text
                # A previous session can still report its terminal event during
                # explicit recovery. Only the query's READY frame is capability.
                if text.startswith("echo:PIS:RT:") and event is None:
                    malformed = True
                if event and event["kind"] == "READY":
                    if ready is not None:
                        malformed = True
                    ready = event["version"]
                if event and event["kind"] == "ERROR" and event.get("id") == 0:
                    if version == 2 and event.get("reason") == "INVALID_PARAMETERS":
                        legacy = True
                    else:
                        malformed = True
                if text in (marker, "echo:" + marker):
                    fenced = True
                    break
            if fenced:
                if malformed or (unsupported and ready is not None):
                    state, message = "error", "Malformed or conflicting PIS:RT capability response."
                elif ready == version and not legacy:
                    state, message = "supported", f"PIS:RT v{version} confirmed for the current connection."
                elif legacy and ready is None:
                    state, ready = "legacy", 1
                    message = "PIS:RT v1 detected; native calibration requires the v2 host-arming barrier. Software excitation remains available."
                elif ready is not None:
                    state, message = "incompatible_version", f"PIS:RT v{ready} is not supported."
                elif unsupported:
                    state, message = "unsupported", "M495 is not supported by this firmware."
                else:
                    state, message = "unsupported", "M495 answered, but PIS:RT host events were not reported."
        except Exception as exc:
            state, message = "error", str(exc)
            self.error = message
        if session.connection == self.connection:
            self.native_capability = {"state": state, "protocol": ready,
                "m495": False if unsupported else (True if fenced else None), "command": "M495", "message": message}
            self.native_connection = tuple(self.printer.get_current_connection()[1:3]) if state in ("supported", "legacy") else None

    def _native_journal(self, session, parameters):
        self._write_json(self.journal, {"native_only": True, "firmware_session_id": session.firmware_id,
            "snapshot": asdict(session.snapshot), "modes": session.modes, "profile": session.profile,
            "session_id": session.id, "axis": session.axis, "parameters": asdict(parameters)})
        session.changed = True
        self.native_unresolved = True

    def _native_restore(self, session):
        if not session.native_stopped:
            raise RuntimeError("Native motion stop is unverified; explicit recovery is required")
        self._stage(session, "verifying printer settings")
        current = self._snapshot(session, recovery=True)
        if not session.snapshot.matches(current):
            raise RuntimeError("Settings changed during native calibration; review them before recovery")
        # Native mode never disabled shaping or changed motion settings. Verify
        # them, rather than overwriting unrelated values with a blind rollback.
        self.journal.unlink(missing_ok=True)
        session.changed = False
        self.native_unresolved = False

    def _native_stop(self, session, lifecycle, parameters, recovering=False, capture=None, capture_deadline=None):
        self._stage(session, "cancelling native sweep")
        marker = f"Pinput_Shaping:{session.id}:native-stop"
        self._native_send(session, ["M496", f"M118 {marker}"], recovery=True)
        deadline = time.monotonic() + parameters.cancel_timeout
        fenced = False
        fence_time = None
        while time.monotonic() < deadline:
            if session.connection != self.connection or self.printer.get_state_id() != "OPERATIONAL":
                raise RuntimeError("Connection lost; native motion may still be active. Recovery required")
            if capture is not None:
                try:
                    if capture_deadline is not None and time.monotonic() >= capture_deadline:
                        raise RuntimeError("Capture duration limit reached during cancellation")
                    monitor = getattr(capture, "check_health", None)
                    if monitor is not None:
                        monitor()
                except Exception as exc:
                    session.native_capture_error = str(exc)
                    self.logger.warning("Sensor invalidated during native cancellation: %s", exc)
                    capture.abort()
                    capture = None
            terminal = lifecycle.terminal
            stopped = terminal is not None and (terminal["kind"] in ("COMPLETE", "ABORTED")
                or (terminal["kind"] == "ERROR" and terminal.get("reason") in
                    ("AXIS_NOT_HOMED", "MOTION_FAILED", "INVALID_PARAMETERS", "INVALID_SESSION_ID", "ARM_TIMEOUT", "PREPARED_STATE_CHANGED")))
            if fenced and (stopped or not session.native_sent and not recovering):
                session.native_stopped = True
                self.logger.info("Calibration session %s (firmware ID %s) native stop confirmed: terminal=%s; queued M496 fence received",
                                 session.id, lifecycle.identifier, terminal["kind"] if terminal else "command not sent")
                return
            if (recovering and fenced and session.native_idle_confirmed
                    and time.monotonic() - fence_time >= 2):
                # A reset can erase the old ID and terminal. This path requires
                # the operator's explicit stopped/reset confirmation, not a
                # deduction from silence, an acknowledgment, or elapsed time.
                self.logger.info("Native recovery uses operator-confirmed stopped/reset state and drained M496")
                session.native_stopped = True
                return
            item = self._native_line(session)
            if item is None:
                continue
            stamp, line, event = item
            if event and event.get("id") == lifecycle.identifier:
                # START may already be in flight when cancellation is requested.
                # Preserve it if COMPLETE wins that race, without publishing success.
                if lifecycle.terminal is None:
                    if recovering:
                        # Cleanup may have missed START across reconnect. Only
                        # terminal proof is needed; never reconstruct a result.
                        if event["kind"] in ("COMPLETE", "ABORTED", "ERROR"):
                            lifecycle.terminal = {**event, "host_time": stamp}
                    else:
                        lifecycle.accept(event, stamp)
            if line.strip() in (marker, "echo:" + marker):
                fenced = True
                fence_time = time.monotonic()
        raise RuntimeError("M496 cancellation or its queued acknowledgment was not confirmed; recovery required")

    def _native_fence(self, session, lifecycle):
        marker = f"Pinput_Shaping:{session.id}:native-finished"
        self._native_send(session, f"M118 {marker}")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self._check(session)
            item = self._native_line(session)
            if item is None:
                continue
            stamp, line, event = item
            if event and event.get("id") == lifecycle.identifier:
                lifecycle.accept(event, stamp)  # Reject duplicates after COMPLETE.
            if line.strip() in (marker, "echo:" + marker):
                return
        raise RuntimeError("Native command acknowledgment fence timed out")

    def _recover_native(self, session, backup):
        from .firmware import FirmwareSnapshot
        if backup["profile"] != session.profile:
            raise RuntimeError("Recovery belongs to a different printer profile")
        session.firmware_id = backup["firmware_session_id"]
        # Detect only on this explicit operator request. No reconnect probe.
        self._detect_native(session)
        if self.native_capability["state"] not in ("supported", "legacy"):
            raise RuntimeError("Verify PIS:RT support before native recovery")
        parameters = NativeParameters(**backup["parameters"])
        session.axis = backup["axis"]
        lifecycle = NativeLifecycle(backup["firmware_session_id"], session.axis, parameters)
        for item in session.native_events:
            event = item["event"]
            if event and event.get("id") == lifecycle.identifier and event["kind"] in ("COMPLETE", "ABORTED"):
                lifecycle.terminal = {**event, "host_time": item["host_time"]}
        session.native_mode = True
        session.snapshot = FirmwareSnapshot(**backup["snapshot"])
        session.changed = True
        self._native_stop(session, lifecycle, parameters, recovering=True)
        self._native_restore(session)

    def _resonance_native(self, session, folder):
        from .native_analysis import capture_metadata, NativeSweepAnalyzer
        from .inputshaping_analyzer import InputShapingAnalyzer
        parameters = NativeParameters.from_settings(session.settings)
        sensor = session.settings["sensorType"]
        from .capture import sampling_rate
        rate = sampling_rate(sensor, session.settings)
        requested_range = session.settings.get("lis2dwRange", 2) if sensor == "lis2dw" else None
        reserve = max(32 * 1024 * 1024, int(parameters.max_capture * rate * 128 * 2))
        if shutil.disk_usage(folder).free < reserve:
            raise RuntimeError("Insufficient disk space for the bounded native capture")
        self._stage(session, "checking sensor configuration")
        options = dict(damping=session.settings["dampingRatio"], axis=session.axis,
            min_accel=session.settings["accelMin"], max_accel=session.settings["accelMax"],
            min_freq=5, max_freq=max(21, parameters.end), firmware_mode=session.snapshot.mode,
            sensor_axis=session.settings.get("sensorAxis", "sum"), logger=self.logger)
        # Validate analysis settings now; actual acquisition and data checks
        # happen only after the matching firmware ARMED_WAIT has been accepted.
        InputShapingAnalyzer(folder, folder / "capture.csv", **options)
        capture = self._capture(session, folder / "capture.csv")
        capture.prepare()
        session.firmware_id = new_firmware_id()
        lifecycle = NativeLifecycle(session.firmware_id, session.axis, parameters, host_arm=True)
        diagnostic = {"backend": "marlin_m495", "session_id": session.id,
            "firmware_session_id": session.firmware_id, "axis": session.axis,
            "parameters": asdict(parameters), "nominal_duration": parameters.duration,
            "sensor": sensor, "port": session.settings.get("sensorPort", ""), "requested_rate": rate,
            "clock_assumption": "host events and sensor samples are not synchronized",
            "protocol": 2, "capture_trigger": "matching ARMED_WAIT received", "outcome": "incomplete"}
        if sensor == "lis2dw":
            diagnostic["requested_range_g"] = requested_range
        analyzer = None
        capture_attempted = False
        capturing = False
        capture_deadline = None
        try:
            self._check(session)
            self._native_journal(session, parameters)
            session.native_mode = True
            self._stage(session, "waiting for Marlin homing / positioning")
            self.publish({"type": "progress", "session_id": session.id, "progress": 0,
                          "progress_source": "waiting for firmware ARMED_WAIT", "sensor_state": "not started"})
            command = parameters.command(session.axis, session.firmware_id, host_arm=True)
            diagnostic["command"] = command
            issued = time.monotonic()
            self._native_send(session, command)
            last_publish = 0
            while lifecycle.terminal is None:
                self._check(session)
                if capturing:
                    capture.ensure_alive()
                now = time.monotonic()
                if capture_deadline is not None and now >= capture_deadline:
                    raise RuntimeError("Native capture exceeded its configured maximum duration")
                item = self._native_line(session)
                # Serial receipt times retain ordering if this worker was delayed
                # while ACK/START/COMPLETE accumulated in the bounded inbox.
                observed = item[0] if item is not None else time.monotonic()
                if lifecycle.armed_wait is None and observed - issued > parameters.preparation_timeout:
                    raise RuntimeError("Native ARMED_WAIT timed out during preparation")
                if (lifecycle.armed_wait is not None and lifecycle.start is None
                        and observed - lifecycle.armed_wait > ARM_TIMEOUT + 3):
                    raise RuntimeError("Native START timed out after host arming")
                if lifecycle.start is not None and observed - lifecycle.start > parameters.duration * 1.15 + 3:
                    raise RuntimeError("Native COMPLETE timed out; measurement is incomplete")
                if item is not None:
                    stamp, line, event = item
                    if line.rstrip().startswith("echo:PIS:RT:") and event is None:
                        raise RuntimeError("Malformed PIS:RT event during native calibration")
                    if event and event.get("id") == 0 and event["kind"] in ("ERROR", "ARM_REJECTED"):
                        raise RuntimeError("Native request rejected: " + event["reason"])
                    if lifecycle.accept(event, stamp):
                        if event["kind"] == "ARMED_WAIT":
                            self._check(session)
                            self._stage(session, "starting capture at ARMED_WAIT")
                            launch = time.monotonic()
                            capture_attempted = True
                            capture_deadline = launch + parameters.max_capture
                            startup_deadline = stamp + SENSOR_START_BUDGET
                            def check_startup():
                                self._check(session)
                                if time.monotonic() >= startup_deadline:
                                    raise RuntimeError("Sensor startup exceeded the native arming budget")
                            capture.check_cancel = check_startup
                            capture.start()
                            diagnostic["sensor_readiness"] = capture.wait_ready()
                            capture.check_cancel = lambda: self._check(session)
                            capturing = True
                            diagnostic["host_capture_launch"] = getattr(capture, "host_times", {}).get("launch", launch)
                            diagnostic["host_capture_ready"] = getattr(capture, "host_times", {}).get("samples_verified", time.monotonic())
                            self.logger.info("Calibration session %s (firmware ID %s) capture verified before ARM: sensor=%s, port=%s, rate=%d Hz",
                                             session.id, session.firmware_id, sensor, getattr(capture, "port", diagnostic["port"]), rate)
                            self._stage(session, "arming native sweep")
                        elif event["kind"] == "START":
                            if not capturing:
                                raise RuntimeError("Native START arrived without verified acquisition")
                            self._stage(session, "measuring native resonance")
                # Consume already queued terminal/error events before authorizing.
                # After sending ARM, every sensor failure follows M496 cleanup,
                # even if ACK or START has not reached this worker yet.
                if (item is None and capturing and lifecycle.arm_requested is None
                        and lifecycle.terminal is None):
                    self._check(session)
                    if time.monotonic() >= lifecycle.armed_wait + SENSOR_START_BUDGET:
                        raise RuntimeError("Sensor startup exceeded the native arming budget")
                    capture.ensure_alive()  # Last health check immediately before ARM.
                    lifecycle.request_arm(time.monotonic())
                    self._native_send(session, f"M495 I{session.firmware_id} R")
                now = time.monotonic()
                if lifecycle.start is not None and now - last_publish >= 1:
                    frequency = lifecycle.frequency
                    fraction = (math.log2(frequency / parameters.start) / math.log2(parameters.end / parameters.start)
                                if frequency else (now - lifecycle.start) / parameters.duration)
                    self.publish({"type": "progress", "session_id": session.id, "progress": min(0.95, max(0, fraction)),
                        "progress_source": "firmware nominal frequency" if frequency else "estimated from elapsed time",
                        "sensor_state": "capturing", "frequency": frequency})
                    last_publish = now
            terminal = lifecycle.terminal
            session.native_stopped = terminal["kind"] in ("COMPLETE", "ABORTED")
            if terminal["kind"] != "COMPLETE":
                raise RuntimeError("Native sweep " + terminal["kind"] + ": " + terminal.get("reason", "measurement invalid"))
            diagnostic["host_start"] = lifecycle.start
            diagnostic["host_terminal"] = terminal["host_time"]
            diagnostic["host_interval"] = terminal["host_time"] - lifecycle.start
            self._stage(session, "stopping capture")
            self._wait(session, 0.5)
            capture.ensure_alive()
            diagnostic["host_capture_stop_requested"] = time.monotonic()
            capture.stop()
            self._native_fence(session, lifecycle)
            self._stage(session, "validating capture")
            self.publish({"type": "progress", "session_id": session.id, "progress": 0.95,
                          "progress_source": "COMPLETE received; validating capture", "sensor_state": "stopped, pending validation"})
            metadata = capture_metadata(folder / "capture.csv", sensor, rate,
                requested_range=requested_range, range_required=getattr(capture, "range_negotiated", False))
            diagnostic["metadata"] = metadata
            if sensor == "lis2dw":
                self.logger.info("Calibration session %s (firmware ID %s) LIS2DW capture validated: "
                                 "requested range=+/-%s g, actual range=+/-%s g, counts_per_g=%s, units=%s, effective rate=%s Hz",
                                 session.id, session.firmware_id, requested_range, metadata["range_g"],
                                 metadata["counts_per_g"], metadata["units"], metadata["effective_rate"])
            self._native_restore(session)
            self._stage(session, "analyzing native resonance")
            ready = getattr(capture, "host_times", {}).get("ready")
            capture_interval = diagnostic["host_capture_stop_requested"] - ready if ready is not None else None
            analyzer = NativeSweepAnalyzer(folder, folder / "capture.csv", native_parameters=parameters,
                host_interval=diagnostic["host_interval"], sensor_metadata=metadata, capture_interval=capture_interval,
                lis2dw_range=requested_range, **options)
            analyzer.analyze()
            if session.snapshot.mode == "ftm" and session.snapshot.ftm_global.get("S") == 0:
                analyzer.warnings.append("FTM is disabled (M493 S0). Applying axis settings does not enable FTM; review and enable its motion mode separately.")
            if metadata.get("present") is not False:
                if metadata["samples"] != analyzer.quality["samples"]:
                    raise RuntimeError("Native CSV is incomplete relative to its metadata")
                if abs(metadata["effective_rate"] / analyzer.sampling_rate - 1) > 0.005:
                    raise RuntimeError("Native metadata rate does not match captured timestamps")
            diagnostic.update(metadata=metadata, effective_rate=analyzer.sampling_rate, outcome="validated",
                              quality=analyzer.quality, warnings=analyzer.warnings)
            analyzer.generate_graphs()
            self._write_json(folder / "analysis.json", {"quality": analyzer.quality,
                "warnings": analyzer.warnings, "recommendation_valid": analyzer.recommendation_valid})
            self.publish({"type": "progress", "session_id": session.id, "progress": 1,
                          "progress_source": "COMPLETE and validated capture", "sensor_state": "validated"})
            return self._analysis_result(session, analyzer, backend="marlin_m495",
                extra={"native_parameters": asdict(parameters), "firmware_session_id": session.firmware_id,
                       "recommendation_valid": analyzer.recommendation_valid})
        except Exception as exc:
            diagnostic["error"] = str(exc)
            diagnostic["failure_stage"] = session.stage
            diagnostic["host_failure"] = time.monotonic()
            diagnostic["start_observed_before_failure"] = lifecycle.start is not None
            self.logger.warning("Calibration session %s (firmware ID %s) native failure during %s: %s",
                                session.id, session.firmware_id, session.stage, exc)
            if analyzer is not None:
                diagnostic.update(quality=analyzer.quality, warnings=analyzer.warnings)
            session.cancelled.set()
            if session.changed and not session.native_stopped:
                try:
                    self._native_stop(session, lifecycle, parameters, capture=capture if capture_attempted else None,
                                      capture_deadline=capture_deadline)
                except Exception as stop_error:
                    session.transport_fault = True
                    diagnostic["cleanup_error"] = str(stop_error)
                    raise RuntimeError(f"{exc}. {stop_error}") from exc
            raise
        finally:
            capture.abort()
            cleanup_time = time.monotonic()
            self._native_log_transmissions(session)
            diagnostic["commands"] = session.native_commands
            diagnostic["events"] = session.native_events
            diagnostic["terminal"] = lifecycle.terminal
            diagnostic["host_start"] = lifecycle.start
            diagnostic["host_terminal"] = lifecycle.terminal["host_time"] if lifecycle.terminal else None
            if lifecycle.start is not None and lifecycle.terminal is not None:
                diagnostic["host_interval"] = lifecycle.terminal["host_time"] - lifecycle.start
            diagnostic["host_homed"] = lifecycle.homed
            diagnostic["host_armed_wait"] = lifecycle.armed_wait
            diagnostic["host_arm_requested"] = lifecycle.arm_requested
            diagnostic["host_arm_ack"] = lifecycle.arm_ack
            diagnostic["host_capture_duration"] = (
                capture.host_times.get("stopped", cleanup_time) - capture.host_times["launch"]
                if "launch" in getattr(capture, "host_times", {}) else None)
            diagnostic["capture_host_times"] = getattr(capture, "host_times", {})
            diagnostic["capture_error_during_cancellation"] = session.native_capture_error
            diagnostic["motion_stopped_verified"] = session.native_stopped
            self._write_json(folder / "protocol_events.json", session.native_events)
            self._write_json(folder / "excitation.json", diagnostic)
