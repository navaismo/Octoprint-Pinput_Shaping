import pytest

from octoprint_Pinput_Shaping.native_protocol import NativeLifecycle, NativeParameters, parse_event, new_firmware_id


@pytest.mark.parametrize("line,kind", [
    ("echo:PIS:RT:READY:1", "READY"),
    ("echo:PIS:RT:START:123:X:20.00:60.00", "START"),
    ("echo:PIS:RT:PROGRESS:123:40.00", "PROGRESS"),
    ("echo:PIS:RT:COMPLETE:123", "COMPLETE"),
    ("echo:PIS:RT:ABORTED:123", "ABORTED"),
    ("echo:PIS:RT:ERROR:0:INVALID_SESSION_ID", "ERROR"),
    ("echo:PIS:RT:READY:2", "READY"),
    ("echo:PIS:RT:HOMED:123:4294967295", "HOMED"),
    ("echo:PIS:RT:ARMED_WAIT:123", "ARMED_WAIT"),
    ("echo:PIS:RT:ARM_ACK:123", "ARM_ACK"),
    ("echo:PIS:RT:ARM_REJECTED:0:INVALID_PARAMETERS", "ARM_REJECTED"),
])
def test_exact_events(line, kind):
    assert parse_event(line + "\r\n")["kind"] == kind


@pytest.mark.parametrize("line", ["ok", "echo:busy: processing", "M118 echo:PIS:RT:COMPLETE:123",
    "echo:echo:PIS:RT:COMPLETE:123", " echo:PIS:RT:READY:1", "echo:PIS:RT:COMPLETE:01",
    "echo:PIS:RT:COMPLETE:0", "echo:PIS:RT:COMPLETE:4294967296", "echo:PIS:RT:START:1:X:20:60",
    "echo:PIS:RT:COMPLETE:1:extra", "echo:PIS:RT:START:1:Z:NaN:60.00",
    "echo:PIS:RT:HOMED:123:4294967296", "echo:PIS:RT:HOMED:123:01",
    "echo:PIS:RT:ARM_ACK:0", "echo:PIS:RT:ARMED_WAIT:123:extra"])
def test_unrelated_or_malformed_lines_are_not_events(line):
    assert parse_event(line) is None


@pytest.mark.parametrize("identifier", ["01", "+1", "-1", "1.0", "0x1", "1\nM112", 0, 4294967296, True])
def test_command_rejects_noncanonical_ids(identifier):
    with pytest.raises(ValueError):
        NativeParameters.from_settings({}).command("X", identifier)


@pytest.mark.parametrize("key,value", [("nativeStart", 4), ("nativeEnd", 20), ("nativeEnd", 201),
    ("nativeOctave", 19), ("nativeOctave", 61), ("nativeAccel", 0), ("nativeAccel", float("inf")),
    ("nativeAccel", 1e9), ("nativeStart", 20.001), ("nativeMaxCapture", 50)])
def test_native_parameter_limits(key, value):
    with pytest.raises(ValueError):
        NativeParameters.from_settings({key: value})


def test_command_and_duration_match_contract():
    parameters = NativeParameters.from_settings({})
    assert parameters.command("X", 12345678) == "M495 X F20.00 T60.00 O20.00 A40.0000 I12345678 S"
    assert parameters.command("X", 12345678, host_arm=True).endswith("I12345678 S W")
    assert parameters.duration == pytest.approx(31.6992500144)
    assert 1 <= new_firmware_id() <= 4294967295


@pytest.mark.parametrize("start,end", [(20.01, 20.02), (199.99, 200)])
def test_decimal_frequency_boundaries_do_not_depend_on_floating_point_addition(start, end):
    parameters = NativeParameters.from_settings({"nativeStart": start, "nativeEnd": end})
    assert parameters.end > parameters.start


def test_lifecycle_ignores_other_ids_and_requires_start_complete():
    lifecycle = NativeLifecycle(123, "X", NativeParameters.from_settings({}))
    assert not lifecycle.accept(parse_event("echo:PIS:RT:COMPLETE:124"), 1)
    with pytest.raises(ValueError, match="before START"):
        lifecycle.accept(parse_event("echo:PIS:RT:COMPLETE:123"), 1)
    lifecycle.accept(parse_event("echo:PIS:RT:START:123:X:20.00:60.00"), 2)
    with pytest.raises(ValueError, match="Duplicate"):
        lifecycle.accept(parse_event("echo:PIS:RT:START:123:X:20.00:60.00"), 3)
    lifecycle.accept(parse_event("echo:PIS:RT:COMPLETE:123"), 35)
    with pytest.raises(ValueError, match="post-terminal"):
        lifecycle.accept(parse_event("echo:PIS:RT:COMPLETE:123"), 36)


@pytest.mark.parametrize("event", ["START:123:Y:20.00:60.00", "START:123:X:21.00:60.00", "PROGRESS:123:40.00"])
def test_unexpected_axis_range_or_progress_before_start(event):
    with pytest.raises(ValueError):
        NativeLifecycle(123, "X", NativeParameters.from_settings({})).accept(parse_event("echo:PIS:RT:" + event), 1)


@pytest.mark.parametrize("kind", ["ABORTED", "ERROR"])
def test_failure_is_valid_without_start(kind):
    lifecycle = NativeLifecycle(123, "X", NativeParameters.from_settings({}))
    suffix = ":MOTION_FAILED" if kind == "ERROR" else ""
    assert lifecycle.accept(parse_event("echo:PIS:RT:" + kind + ":123" + suffix), 1)
    assert lifecycle.terminal["kind"] == kind


def test_optional_progress_tracks_nominal_frequency_and_rejects_regression():
    lifecycle = NativeLifecycle(123, "X", NativeParameters.from_settings({}))
    lifecycle.accept(parse_event("echo:PIS:RT:START:123:X:20.00:60.00"), 1)
    lifecycle.accept(parse_event("echo:PIS:RT:PROGRESS:123:35.00"), 10)
    assert lifecycle.frequency == 35
    for frequency in ("34.00", "61.00"):
        with pytest.raises(ValueError, match="progress"):
            lifecycle.accept(parse_event("echo:PIS:RT:PROGRESS:123:" + frequency), 11)


def test_v2_requires_preparation_authorization_and_ack_before_start():
    lifecycle = NativeLifecycle(123, "X", NativeParameters.from_settings({}), host_arm=True)
    for line in ("ARMED_WAIT:123", "ARM_ACK:123", "START:123:X:20.00:60.00"):
        with pytest.raises(ValueError):
            lifecycle.accept(parse_event("echo:PIS:RT:" + line), 1)
    with pytest.raises(ValueError):
        lifecycle.request_arm(1)
    assert not lifecycle.accept(parse_event("echo:PIS:RT:ARMED_WAIT:124"), 1)
    lifecycle.accept(parse_event("echo:PIS:RT:HOMED:123:0"), 2)
    lifecycle.accept(parse_event("echo:PIS:RT:ARMED_WAIT:123"), 3)
    lifecycle.request_arm(4)
    with pytest.raises(ValueError):
        lifecycle.request_arm(4.1)
    lifecycle.accept(parse_event("echo:PIS:RT:ARM_ACK:123"), 5)
    lifecycle.accept(parse_event("echo:PIS:RT:START:123:X:20.00:60.00"), 6)
    lifecycle.accept(parse_event("echo:PIS:RT:ARM_ACK:123"), 7)
    assert lifecycle.arm_ack == 5
    lifecycle.accept(parse_event("echo:PIS:RT:COMPLETE:123"), 38)
    with pytest.raises(ValueError, match="post-terminal"):
        lifecycle.accept(parse_event("echo:PIS:RT:ARM_ACK:123"), 39)


def test_v2_terminal_before_arm_prevents_late_authorization():
    lifecycle = NativeLifecycle(123, "X", NativeParameters.from_settings({}), host_arm=True)
    lifecycle.accept(parse_event("echo:PIS:RT:HOMED:123:400"), 1)
    lifecycle.accept(parse_event("echo:PIS:RT:ARMED_WAIT:123"), 2)
    lifecycle.accept(parse_event("echo:PIS:RT:ERROR:123:ARM_TIMEOUT"), 32)
    with pytest.raises(ValueError):
        lifecycle.request_arm(33)
