"""Accelerometer process lifecycle with an explicit file-ready handshake."""

import csv
import os
import re
import shutil
import threading
import time

import pexpect

from .firmware import finite_number


class AccelerometerCapture:
    DRIVERS = {"adxlspi": ("adxl345spi", True, 3200),
               "adxlusb": ("adxl345usb", False, 3200),
               "lis2dw": ("lis2dwusb", False, 800)}

    def __init__(self, sensor_type, path, check_cancel=lambda: None, port="", lis2dw_rate=800):
        if sensor_type not in self.DRIVERS:
            raise ValueError("Unsupported accelerometer type")
        self.driver, self.needs_sudo, self.rate = self.DRIVERS[sensor_type]
        if sensor_type == "lis2dw":
            rate = finite_number(lis2dw_rate, "LIS2DW sampling rate", 800, 1600)
            if rate not in (800, 1600):
                raise ValueError("LIS2DW sampling rate must be 800 or 1600 Hz")
            self.rate = int(rate)
        self.port = port or {"adxlusb": "/dev/ttyACM1", "lis2dw": "/dev/ttyACM0"}.get(sensor_type, "")
        if self.port and not re.fullmatch(r"/dev/(?:tty(?:ACM|USB)\d+|serial/by-id/[A-Za-z0-9_.:+-]+)", self.port):
            raise ValueError("Use a sensor serial device under /dev/ttyACM*, /dev/ttyUSB*, or /dev/serial/by-id/")
        self.path = str(path)
        self.child = None
        self.logfile = None
        self.check_cancel = check_cancel
        self.last_size = 0
        self.last_progress = 0
        self._reader = None
        self._output_done = threading.Event()
        self._output_error = None

    def _read_output(self):
        """Drain the PTY while motion runs so diagnostics cannot block a driver."""
        try:
            while True:
                try:
                    self.child.read_nonblocking(size=4096, timeout=0.2)
                except pexpect.TIMEOUT:
                    continue
                except pexpect.EOF:
                    return
        except (pexpect.ExceptionPexpect, OSError) as exc:
            self._output_error = str(exc)
        finally:
            self._output_done.set()

    def _wait_for_exit(self, timeout):
        if self._reader is not None:
            if not self._output_done.wait(timeout):
                raise pexpect.TIMEOUT("Accelerometer did not close its output")
            self._reader.join()
        else:
            self.child.expect(pexpect.EOF, timeout=timeout)
        self.child.close()

    def _is_alive(self):
        if self._reader is not None:
            # Only the reader polls/reaps the child while it owns the PTY.
            return not self._output_done.is_set()
        return self.child is not None and self.child.isalive()

    def _failure(self, message):
        details = [self.driver]
        if self.child is not None:
            if not self._is_alive() and not self.child.closed:
                # Collect trailing stderr before reporting an unexpected exit.
                try:
                    self._wait_for_exit(timeout=2)
                except (pexpect.ExceptionPexpect, OSError) as exc:
                    details.append(f"output cleanup: {exc}")
            if self.child.exitstatus is not None:
                details.append(f"exit code {self.child.exitstatus}")
            if self.child.signalstatus is not None:
                details.append(f"signal {self.child.signalstatus}")
        if self._output_error:
            details.append(f"output reader: {self._output_error}")
        return RuntimeError(f"{message} ({', '.join(details)}); inspect {self.path}.log")

    def start(self):
        binary = shutil.which(self.driver)
        if not binary:
            raise RuntimeError(f"Install the {self.driver} wrapper before calibration")
        if os.path.exists(self.path):
            raise RuntimeError("Capture path already exists; refusing to reuse old data")
        args = (["-p", self.port] if self.port and not self.needs_sudo else []) + ["-f", str(self.rate), "-s", self.path]
        executable = binary
        if self.needs_sudo:
            executable, args = "sudo", ["-n", binary] + args
        self.logfile = open(self.path + ".log", "w", encoding="utf-8")
        try:
            self.child = pexpect.spawn(executable, args, encoding="utf-8", codec_errors="replace", timeout=8)
            self.child.logfile_read = self.logfile
            prompt_deadline = time.monotonic() + 8
            while True:
                self.check_cancel()
                try:
                    self.child.expect_exact("Press Q to stop", timeout=0.2)
                    break
                except pexpect.TIMEOUT:
                    if time.monotonic() >= prompt_deadline:
                        raise RuntimeError("Accelerometer did not become ready before the start deadline")
            self._reader = threading.Thread(target=self._read_output, daemon=True,
                                            name="pinput-sensor-output")
            self._reader.start()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                self.check_cancel()
                if self._output_error:
                    raise self._failure("Could not read accelerometer output")
                if not self._is_alive():
                    raise self._failure("Accelerometer exited before data became available")
                if os.path.isfile(self.path):
                    with open(self.path, newline="", encoding="utf-8") as stream:
                        rows = list(csv.reader(stream.read(16384).splitlines()))
                    if rows and [x.strip().lower() for x in rows[0]] != ["time", "x", "y", "z"]:
                        raise RuntimeError("Accelerometer returned an unsupported CSV format")
                    if len(rows) >= 17:
                        self.last_size = os.path.getsize(self.path)
                        self.last_progress = time.monotonic()
                        return
                time.sleep(0.05)
            raise RuntimeError("Accelerometer did not produce samples before the start deadline")
        except pexpect.EOF as exc:
            error = self._failure("Accelerometer exited before becoming ready")
            self.abort()
            raise error from exc
        except Exception:
            self.abort()
            raise

    def ensure_alive(self):
        self.check_cancel()
        if self._output_error:
            raise self._failure("Could not read accelerometer output")
        if not self._is_alive():
            raise self._failure("Accelerometer exited during measurement")
        size, now = os.path.getsize(self.path), time.monotonic()
        if size > self.last_size:
            self.last_size, self.last_progress = size, now
        elif now - self.last_progress > 2:
            raise self._failure("Accelerometer stopped producing samples")

    def stop(self):
        """Wait for EOF and successful exit, not driver-specific summary text."""
        try:
            if not self._is_alive():
                raise self._failure("Accelerometer exited before the capture was stopped")
            self.child.sendline("Q")
            self._wait_for_exit(timeout=12)
            if self.child.exitstatus != 0 or self.child.signalstatus is not None:
                raise self._failure("Accelerometer failed while saving samples")
            if self._output_error:
                raise self._failure("Could not read accelerometer output")
        except Exception:
            self.abort()
            raise
        finally:
            if self.logfile:
                self.logfile.close()

    def abort(self):
        """Best-effort cleanup; forced output is never accepted for analysis."""
        if self.child and not self.child.closed:
            try:
                if self._is_alive():
                    self.child.sendline("Q")
                self._wait_for_exit(timeout=2)
                self.child.close(force=True)
            except (pexpect.ExceptionPexpect, OSError):
                self.child.terminate(force=True)
                self.child.close(force=True)
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self.logfile:
            self.logfile.close()
