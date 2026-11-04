from pathlib import Path
import sys
import threading
import time

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
        self.closed = False
        self.lines = []
        self.stopped = threading.Event()

    def expect_exact(self, prompt, timeout):
        return 0

    def expect(self, prompt, timeout):
        assert prompt is pexpect.EOF
        self.alive = False
        return 0

    def isalive(self):
        return self.alive

    def read_nonblocking(self, size, timeout):
        if not self.stopped.wait(timeout):
            raise pexpect.TIMEOUT("waiting for driver output")
        raise pexpect.EOF("driver exited")

    def sendline(self, value):
        self.lines.append(value)
        self.alive = False
        self.stopped.set()

    def close(self, force=False):
        self.alive = False
        self.closed = True
        self.stopped.set()

    def terminate(self, force=False):
        self.alive = False
        self.stopped.set()


@pytest.mark.parametrize("sensor,sudo,rate", [("adxlspi", True, 3200), ("adxlusb", False, 3200), ("lis2dw", False, 800)])
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


@pytest.mark.parametrize("rate", [800, 1600, "800", "1600"])
def test_lis2dw_rate_is_passed_to_the_driver(tmp_path, monkeypatch, rate):
    output = tmp_path / "capture.csv"
    calls, child = [], Child()
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/local/bin/" + name)
    def spawn(executable, args, **kwargs):
        calls.append(args)
        output.write_text("time,x,y,z\n" + "\n".join(f"{i / int(rate)},0,0,1" for i in range(20)) + "\n")
        return child
    monkeypatch.setattr(module.pexpect, "spawn", spawn)
    capture = AccelerometerCapture("lis2dw", output, lis2dw_rate=rate)
    try:
        capture.start()
        assert calls[0][calls[0].index("-f") + 1] == str(rate)
    finally:
        capture.abort()


@pytest.mark.parametrize("rate", [True, None, 400, 1200, "800\nM112", float("nan")])
def test_invalid_lis2dw_rates_are_rejected_before_launch(tmp_path, rate):
    with pytest.raises(ValueError):
        AccelerometerCapture("lis2dw", tmp_path / "capture.csv", lis2dw_rate=rate)


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


def wait_until(condition):
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "Simulated driver did not respond"
        time.sleep(0.01)


def simulated_driver(tmp_path, monkeypatch, script):
    path = tmp_path / "capture.csv"
    spawn = pexpect.spawn
    monkeypatch.setattr(module.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(module.pexpect, "spawn", lambda executable, args, **kwargs:
                        spawn(sys.executable, ["-u", "-c", script, str(path)], **kwargs))
    return AccelerometerCapture("adxlusb", path)


DRIVER_START = """
import pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text('time,x,y,z\\n' + ''.join(f'{i / 3200},0,0,1\\n' for i in range(20)))
print('Press Q to stop', flush=True)
"""


def test_output_is_drained_while_motion_worker_is_busy(tmp_path, monkeypatch):
    capture = simulated_driver(tmp_path, monkeypatch, DRIVER_START + """
sys.stderr.write('diagnostic output\\n' * 20000)
sys.stderr.flush()
path.with_suffix('.ready').touch()
assert sys.stdin.readline().strip() == 'Q'
""")
    try:
        capture.start()
        # No ensure_alive/expect call here: the driver must keep making progress
        # while the calibration worker is waiting on printer communication.
        wait_until(lambda: Path(capture.path).with_suffix('.ready').exists())
        capture.stop()
        assert Path(capture.path + ".log").read_text().count("diagnostic output") == 20000
    finally:
        capture.abort()


def test_unexpected_exit_reports_code_and_keeps_trailing_driver_error(tmp_path, monkeypatch):
    capture = simulated_driver(tmp_path, monkeypatch, DRIVER_START + """
sys.stdin.readline()
sys.stderr.write('ERROR: simulated serial failure\\n')
sys.exit(4)
""")
    try:
        capture.start()
        capture.child.sendline("fail")
        wait_until(lambda: not capture._is_alive())
        with pytest.raises(RuntimeError, match="exited during measurement") as error:
            capture.ensure_alive()
        assert "adxl345usb" in str(error.value)
        assert "exit code 4" in str(error.value)
        assert capture.path + ".log" in str(error.value)
        assert "ERROR: simulated serial failure" in Path(capture.path + ".log").read_text()
    finally:
        capture.abort()


def test_exit_before_ready_preserves_the_driver_error(tmp_path, monkeypatch):
    capture = simulated_driver(tmp_path, monkeypatch, "import sys; sys.stderr.write('sensor unavailable\\n'); sys.exit(2)")
    with pytest.raises(RuntimeError, match="exited before becoming ready") as error:
        capture.start()
    assert "exit code 2" in str(error.value)
    assert capture.logfile.closed
    assert "sensor unavailable" in Path(capture.path + ".log").read_text()


@pytest.mark.parametrize("path", ["/tmp/sensor", "/dev/ttyACM0 -s /etc/passwd", "/dev/serial/by-id/../../ttyUSB0"])
def test_sensor_port_is_validated_before_process_creation(tmp_path, path):
    with pytest.raises(ValueError):
        AccelerometerCapture("adxlusb", tmp_path / "capture.csv", port=path)
