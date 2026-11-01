"""Accelerometer process lifecycle with an explicit file-ready handshake."""

import csv
import os
import re
import shutil
import time

import pexpect


class AccelerometerCapture:
    DRIVERS = {"adxlspi": ("adxl345spi", True, 3200),
               "adxlusb": ("adxl345usb", False, 3200),
               "lis2dw": ("lis2dwusb", False, 1600)}

    def __init__(self, sensor_type, path, check_cancel=lambda: None, port=""):
        if sensor_type not in self.DRIVERS:
            raise ValueError("Unsupported accelerometer type")
        self.driver, self.needs_sudo, self.rate = self.DRIVERS[sensor_type]
        self.port = port or {"adxlusb": "/dev/ttyACM1", "lis2dw": "/dev/ttyACM0"}.get(sensor_type, "")
        if self.port and not re.fullmatch(r"/dev/(?:tty(?:ACM|USB)\d+|serial/by-id/[A-Za-z0-9_.:+-]+)", self.port):
            raise ValueError("Use a sensor serial device under /dev/ttyACM*, /dev/ttyUSB*, or /dev/serial/by-id/")
        self.path = str(path)
        self.child = None
        self.logfile = None
        self.check_cancel = check_cancel
        self.last_size = 0
        self.last_progress = 0

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
            self.child = pexpect.spawn(executable, args, encoding="utf-8", timeout=8)
            self.child.logfile = self.logfile
            prompt_deadline = time.monotonic() + 8
            while True:
                self.check_cancel()
                try:
                    self.child.expect_exact("Press Q to stop", timeout=0.2)
                    break
                except pexpect.TIMEOUT:
                    if time.monotonic() >= prompt_deadline:
                        raise RuntimeError("Accelerometer did not become ready before the start deadline")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                self.check_cancel()
                if not self.child.isalive():
                    raise RuntimeError("Accelerometer exited before data became available")
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
        except Exception:
            self.abort()
            raise

    def ensure_alive(self):
        self.check_cancel()
        if self.child is None or not self.child.isalive():
            raise RuntimeError("Accelerometer exited during measurement")
        size, now = os.path.getsize(self.path), time.monotonic()
        if size > self.last_size:
            self.last_size, self.last_progress = size, now
        elif now - self.last_progress > 2:
            raise RuntimeError("Accelerometer stopped producing samples")

    def stop(self):
        """Wait for EOF and successful exit, not driver-specific summary text."""
        try:
            if self.child is None or not self.child.isalive():
                raise RuntimeError("Accelerometer exited before the capture was stopped")
            self.child.sendline("Q")
            self.child.expect(pexpect.EOF, timeout=12)
            self.child.close()
            if self.child.exitstatus != 0 or self.child.signalstatus is not None:
                raise RuntimeError("Accelerometer failed while saving samples; inspect capture log")
        except Exception:
            self.abort()
            raise
        finally:
            if self.logfile:
                self.logfile.close()

    def abort(self):
        """Best-effort cleanup; forced output is never accepted for analysis."""
        if self.child:
            try:
                if self.child.isalive():
                    self.child.sendline("Q")
                    self.child.expect(pexpect.EOF, timeout=2)
                self.child.close(force=True)
            except (pexpect.ExceptionPexpect, OSError):
                self.child.terminate(force=True)
        if self.logfile:
            self.logfile.close()
