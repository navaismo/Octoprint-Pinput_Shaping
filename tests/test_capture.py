from pathlib import Path

import pexpect
import pytest

import octoprint_Pinput_Shaping.capture as module
from octoprint_Pinput_Shaping.capture import AccelerometerCapture


class Child:
    def __init__(self, exitstatus=0):
        self.exitstatus = exitstatus
        self.signalstatus = None
        self.logfile = None
        self.alive = True
        self.lines = []

    def expect_exact(self, prompt, timeout):
        return 0

    def expect(self, prompt, timeout):
        assert prompt is pexpect.EOF
        self.alive = False
        return 0

    def isalive(self):
        return self.alive

    def sendline(self, value):
        self.lines.append(value)

    def close(self, force=False):
        self.alive = False

    def terminate(self, force=False):
        self.alive = False


@pytest.mark.parametrize("sensor,sudo,rate", [("adxlspi", True, 3200), ("adxlusb", False, 3200), ("lis2dw", False, 1600)])
def test_driver_selection_argument_boundaries_and_eof_protocol(tmp_path, monkeypatch, sensor, sudo, rate):
    output = tmp_path / "path with spaces.csv"
    child, calls = Child(), []
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/local/bin/" + name)
    def spawn(executable, args, **kwargs):
        calls.append((executable, args))
        output.write_text("time,x,y,z\n" + "\n".join(f"{i / rate},0,0,1" for i in range(20)) + "\n")
        return child
    monkeypatch.setattr(module.pexpect, "spawn", spawn)
    capture = AccelerometerCapture(sensor, output)
    capture.start()
    capture.stop()
    executable, args = calls[0]
    assert (executable == "sudo") == sudo
    assert args[-1] == str(output)
    assert args[args.index("-f") + 1] == str(rate)
    assert child.lines == ["Q"]
    assert capture.logfile.closed


def test_failed_driver_exit_is_not_accepted_as_saved_data(tmp_path):
    capture = AccelerometerCapture("adxlusb", tmp_path / "capture.csv")
    capture.child = Child(exitstatus=2)
    with pytest.raises(RuntimeError, match="failed while saving"):
        capture.stop()


def test_existing_capture_cannot_be_reused(tmp_path, monkeypatch):
    path = tmp_path / "capture.csv"
    path.write_text("stale data")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/local/bin/" + name)
    with pytest.raises(RuntimeError, match="refusing to reuse"):
        AccelerometerCapture("adxlspi", path).start()


def test_driver_must_still_be_alive_until_stop(tmp_path):
    capture = AccelerometerCapture("adxlusb", tmp_path / "capture.csv")
    capture.child = Child()
    capture.child.alive = False
    with pytest.raises(RuntimeError, match="before the capture was stopped"):
        capture.stop()


def test_stalled_sensor_is_detected_without_waiting_for_test_completion(tmp_path, monkeypatch):
    path = tmp_path / "capture.csv"
    path.write_text("time,x,y,z\n")
    capture = AccelerometerCapture("adxlusb", path)
    capture.child = Child()
    capture.last_size = path.stat().st_size
    capture.last_progress = 1
    monkeypatch.setattr(module.time, "monotonic", lambda: 4)
    with pytest.raises(RuntimeError, match="stopped producing"):
        capture.ensure_alive()


@pytest.mark.parametrize("path", ["/tmp/sensor", "/dev/ttyACM0 -s /etc/passwd", "/dev/serial/by-id/../../ttyUSB0"])
def test_sensor_port_is_validated_before_process_creation(tmp_path, path):
    with pytest.raises(ValueError):
        AccelerometerCapture("adxlusb", tmp_path / "capture.csv", port=path)
