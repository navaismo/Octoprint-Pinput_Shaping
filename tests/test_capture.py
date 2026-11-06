from pathlib import Path
import sys
import types
import threading
import time

import pexpect
import pytest

import octoprint_Pinput_Shaping.capture as module
from octoprint_Pinput_Shaping.capture import AccelerometerCapture


@pytest.fixture(autouse=True)
def legacy_wrapper_help(monkeypatch):
    # Existing lifecycle tests use the legacy +/-2 g wrapper interface.
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs:
        types.SimpleNamespace(returncode=0, stdout="Usage: lis2dwusb [-f RATE] [-s FILE]"))


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


@pytest.mark.parametrize("sensor", ["adxlspi", "adxlusb"])
@pytest.mark.parametrize("rate", [800, 1600, 3200, "800", "1600", "3200"])
def test_adxl_rates_do_not_negotiate_lis_range(tmp_path, monkeypatch, sensor, rate):
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/local/bin/" + name)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: pytest.fail("ADXL must not probe LIS range support"))
    capture = AccelerometerCapture(sensor, tmp_path / "capture.csv", adxl_rate=rate,
                                   port="/dev/serial/by-id/usb-ADXL345", lis2dw_rate=0, lis2dw_range=0)
    executable, args = capture.prepare()
    assert args[args.index("-f") + 1] == str(rate)
    assert "-r" not in args
    assert capture.range_g is None and not capture.range_negotiated
    if sensor == "adxlspi":
        assert executable == "sudo" and args[:2] == ["-n", "/usr/local/bin/adxl345spi"]
        assert "-p" not in args and capture.port == ""
    else:
        assert executable == "/usr/local/bin/adxl345usb"
        assert args[args.index("-p") + 1] == "/dev/serial/by-id/usb-ADXL345"


@pytest.mark.parametrize("sensor", ["adxlspi", "adxlusb"])
@pytest.mark.parametrize("rate", [None, True, 0, 400, 801, 3200.5, 6400, "bad", float("nan")])
def test_invalid_adxl_rate_fails_before_launch(tmp_path, sensor, rate):
    with pytest.raises(ValueError, match="sampling rate"):
        AccelerometerCapture(sensor, tmp_path / "capture.csv", adxl_rate=rate)


def test_spi_ignores_a_stale_usb_port(tmp_path):
    assert AccelerometerCapture("adxlspi", tmp_path / "capture.csv", port="stale USB configuration").port == ""


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


def simulated_driver(tmp_path, monkeypatch, script, sensor="adxlusb", lis2dw_range=2):
    path = tmp_path / "capture.csv"
    spawn = pexpect.spawn
    monkeypatch.setattr(module.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(module.pexpect, "spawn", lambda executable, args, **kwargs:
                        spawn(sys.executable, ["-u", "-c", script, str(path)], **kwargs))
    return AccelerometerCapture(sensor, path, lis2dw_range=lis2dw_range)


DRIVER_START = """
import pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text('time,x,y,z\\n' + ''.join(f'{i / 3200},0,0,1\\n' for i in range(20)))
print('Press Q to stop', flush=True)
"""


@pytest.mark.parametrize("sensor", ["adxlspi", "adxlusb", "lis2dw"])
def test_output_is_drained_while_motion_worker_is_busy(tmp_path, monkeypatch, sensor):
    capture = simulated_driver(tmp_path, monkeypatch, DRIVER_START + """
sys.stderr.write('diagnostic output\\n' * 20000)
sys.stderr.flush()
path.with_suffix('.ready').touch()
assert sys.stdin.readline().strip() == 'Q'
""", sensor=sensor)
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


@pytest.mark.parametrize("device_error", ["CLIPPING", "BUFFER_OVERFLOW:acq=5304:fmt=5047:usb_ok=3353:usb_busy=8582"])
def test_device_rejection_reason_is_visible_in_the_reported_sensor_error(tmp_path, monkeypatch, device_error):
    capture = simulated_driver(tmp_path, monkeypatch, DRIVER_START + f"""
sys.stdin.readline()
print('Device error: {device_error}', flush=True)
print('Captured 3759 samples; Firmware rejected or invalidated this capture', flush=True)
sys.exit(2)
""", sensor="lis2dw")
    try:
        capture.start()
        capture.child.sendline("fail")
        wait_until(lambda: not capture._is_alive())
        with pytest.raises(RuntimeError, match="exited during measurement") as error:
            capture.ensure_alive()
        assert "lis2dwusb" in str(error.value)
        assert "exit code 2" in str(error.value)
        assert f"device error: {device_error}" in str(error.value)
    finally:
        capture.abort()


def test_exit_before_ready_preserves_the_driver_error(tmp_path, monkeypatch):
    capture = simulated_driver(tmp_path, monkeypatch, "import sys; sys.stderr.write('sensor unavailable\\n'); sys.exit(2)")
    with pytest.raises(RuntimeError, match="exited before becoming ready") as error:
        capture.start()
    assert "exit code 2" in str(error.value)
    assert capture.logfile.closed
    assert "sensor unavailable" in Path(capture.path + ".log").read_text()


@pytest.mark.parametrize("sensor", ["adxlspi", "adxlusb", "lis2dw"])
def test_native_readiness_observes_fresh_timestamped_samples(tmp_path, monkeypatch, sensor):
    capture = simulated_driver(tmp_path, monkeypatch, DRIVER_START + """
import time
for first in range(20, 120, 20):
    time.sleep(0.04)
    with path.open('a') as stream:
        stream.write(''.join(f'{i / 3200},0,0,1\\n' for i in range(first, first + 20)))
assert sys.stdin.readline().strip() == 'Q'
""", sensor=sensor)
    try:
        capture.prepare()
        assert capture.child is None and not Path(capture.path).exists()
        capture.start()
        observed = capture.wait_ready()
        assert observed["observations"] == 2
        assert observed["last_timestamp"] > observed["previous_last_timestamp"]
        assert capture.host_times["samples_verified"] >= capture.host_times["ready"]
        capture.stop()
    finally:
        capture.abort()


@pytest.mark.parametrize("failure", ["stalled", "duplicate", "reverse", "nonfinite"])
@pytest.mark.parametrize("sensor", ["adxlspi", "adxlusb", "lis2dw"])
def test_native_readiness_rejects_stale_or_invalid_samples(tmp_path, monkeypatch, failure, sensor):
    additions = {"stalled": "", "duplicate": "0.0059375,0,0,1\\n",
                 "reverse": "0.001,0,0,1\\n", "nonfinite": "nan,0,0,1\\n"}
    capture = simulated_driver(tmp_path, monkeypatch, DRIVER_START + f"""
import time
time.sleep(0.04)
with path.open('a') as stream:
    stream.write({additions[failure]!r})
sys.stdin.readline()
""", sensor=sensor)
    try:
        capture.start()
        with pytest.raises(RuntimeError, match="timestamp|new.*samples"):
            capture.wait_ready(timeout=0.3)
    finally:
        capture.abort()


@pytest.mark.parametrize("path", ["/tmp/sensor", "/dev/ttyACM0 -s /etc/passwd", "/dev/serial/by-id/../../ttyUSB0"])
def test_sensor_port_is_validated_before_process_creation(tmp_path, path):
    with pytest.raises(ValueError):
        AccelerometerCapture("adxlusb", tmp_path / "capture.csv", port=path)


@pytest.mark.parametrize("full_scale", [2, 4, 8, 16, "2", "4", "8", "16"])
def test_selected_range_arguments_preserve_rate_port_and_output(tmp_path, monkeypatch, full_scale):
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/local/bin/lis2dwusb")
    probes = []
    def help_output(args, **kwargs):
        probes.append(args)
        return types.SimpleNamespace(returncode=0, stdout="Usage: lis2dwusb [-r 2|4|8|16] [-f RATE]")
    monkeypatch.setattr(module.subprocess, "run", help_output)
    path = tmp_path / "capture.csv"
    capture = AccelerometerCapture("lis2dw", path, lis2dw_range=full_scale)
    binary, args = capture.prepare()
    assert args == ["-p", "/dev/ttyACM0", "-f", "800", "-r", str(full_scale), "-s", str(path)]
    assert capture.prepare() == (binary, args)
    assert probes == [[binary, "-h"]]  # No sensor acquisition for capability checks.


@pytest.mark.parametrize("value", [True, False, None, 0, 3, 32, -2, 2.5, "16; M112", float("nan"), float("inf")])
def test_invalid_ranges_are_rejected_before_any_wrapper_process(tmp_path, monkeypatch, value):
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("Unexpected wrapper launch"))
    with pytest.raises(ValueError, match="range"):
        AccelerometerCapture("lis2dw", tmp_path / "capture.csv", lis2dw_range=value)


def test_legacy_wrapper_accepts_only_two_g_without_sending_unsupported_option(tmp_path, monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/local/bin/lis2dwusb")
    capture = AccelerometerCapture("lis2dw", tmp_path / "capture.csv")
    assert "-r" not in capture.prepare()[1]
    assert not capture.range_negotiated
    for full_scale in (4, 8, 16):
        with pytest.raises(RuntimeError, match="Update lis2dwusb"):
            AccelerometerCapture("lis2dw", tmp_path / "capture.csv", lis2dw_range=full_scale).prepare()


@pytest.mark.parametrize("full_scale", [2, 4, 8, 16])
def test_capture_requires_verified_ready_and_final_matching_range_metadata(tmp_path, monkeypatch, full_scale):
    scale = 32768 // full_scale
    capture = simulated_driver(tmp_path, monkeypatch, DRIVER_START + f"""
import json
print('Range: +/- {full_scale} g, {scale} counts/g (verified READY)', flush=True)
assert sys.stdin.readline().strip() == 'Q'
path.with_suffix('.csv.meta.json').write_text(json.dumps({{
    'protocol': 2, 'valid': True, 'requested_range_g': {full_scale}, 'range_g': {full_scale},
    'units': 'g', 'counts_per_g': {scale}}}))
""", sensor="lis2dw", lis2dw_range=full_scale)
    capture._range_option = True
    try:
        capture.start()
        capture.stop()
        assert capture.metadata["range_g"] == full_scale
        assert capture.metadata["counts_per_g"] == scale
    finally:
        capture.abort()


def test_wrong_startup_range_fails_before_native_readiness(tmp_path, monkeypatch):
    capture = simulated_driver(tmp_path, monkeypatch, DRIVER_START + """
print('Range: +/- 2 g, 16384 counts/g (verified READY)', flush=True)
sys.stdin.readline()
""", sensor="lis2dw", lis2dw_range=16)
    capture._range_option = True
    with pytest.raises(RuntimeError, match="READY range/scale does not match"):
        capture.start()
    assert "ready" not in capture.host_times


@pytest.mark.parametrize("metadata", [None, {}, {'protocol': 2, 'valid': True, 'units': 'g', 'counts_per_g': 16384},
    {'protocol': 2, 'valid': True, 'units': 'g', 'requested_range_g': 16, 'range_g': 16, 'counts_per_g': 16384}])
def test_successful_wrapper_exit_cannot_bypass_scale_validation(tmp_path, monkeypatch, metadata):
    capture = simulated_driver(tmp_path, monkeypatch, DRIVER_START + f"""
import json
print('Range: +/- 16 g, 2048 counts/g (verified READY)', flush=True)
assert sys.stdin.readline().strip() == 'Q'
metadata = {metadata!r}
if metadata is not None:
    path.with_suffix('.csv.meta.json').write_text(json.dumps(metadata))
""", sensor="lis2dw", lis2dw_range=16)
    capture._range_option = True
    try:
        capture.start()
        with pytest.raises(RuntimeError, match="range metadata validation failed"):
            capture.stop()
    finally:
        capture.abort()
