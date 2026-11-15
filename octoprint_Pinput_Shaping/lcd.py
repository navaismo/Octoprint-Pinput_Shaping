"""Optional M9001 display mirroring, entirely before/after calibration motion.

Wire format extends Octoprint-LCD_E3V3SE/main_cleanup with explicitly negotiated
full-screen ownership. No print lifecycle, pause/resume or raw serial access.
"""

import base64
from collections import defaultdict
import queue
import re
import secrets
import struct
import time
import zlib


CAPS = re.compile(r"M9001 CAPS 2 42 240 320 00000000 0000\Z")
FULLSCREEN_CAPS = re.compile(r"M9001 CAPS FULLSCREEN 1 42 240 320 00000000 0000\Z")


def packet(kind, identifier, body=b""):
    data = struct.pack(">BI", 2, identifier) + body
    data += struct.pack(">I", zlib.crc32(kind.encode("ascii") + data) & 0xFFFFFFFF)
    return "M9001 " + kind + base64.b64encode(data).decode("ascii").replace("G", "_")


def operations(image):
    """Merge equal RGB565 runs vertically, then group disjoint runs by color."""
    if image.size != (240, 320):
        raise ValueError("Full-screen M9001 requires a 240x320 image")
    raw = image.convert("RGB").tobytes()
    pixels = [((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
              for r, g, b in zip(raw[::3], raw[1::3], raw[2::3])]
    previous, output = {}, []
    for y in range(320):
        current, x = {}, 0
        while x < 240:
            start, value = x, pixels[y * 240 + x]
            x += 1
            while x < 240 and pixels[y * 240 + x] == value:
                x += 1
            key = (start, x - 1, value)
            run = previous.pop(key, [start, y, x - 1, y, value])
            run[3] = y
            current[key] = run
        output.extend(previous.values())
        previous = current
    output.extend(previous.values())
    groups = defaultdict(list)
    for x0, y0, x1, y1, value in output:
        groups[value].append(b"\3" + struct.pack(">HHHH", x0, y0, x1, y1))
    for value, rectangles in sorted(groups.items()):
        yield b"\0" + struct.pack(">H", value)
        yield from rectangles


def batches(image):
    data = bytearray()
    for operation in operations(image):
        if len(data) + len(operation) > 42:
            yield bytes(data)
            data.clear()
        data.extend(operation)
    if data:
        yield bytes(data)


class CalibrationLCDMixin:
    def _lcd_detect(self, session):
        try:
            lines, _ = self._exchange(session, ["M9001 Q"])
            if any(CAPS.fullmatch(re.sub(r"^echo:", "", line.strip())) for line in lines):
                lines, _ = self._exchange(session, ["M9001 QF"])
                if any(FULLSCREEN_CAPS.fullmatch(re.sub(r"^echo:", "", line.strip())) for line in lines):
                    return 2
        except RuntimeError as exc:
            # _exchange has received its queue fence before reporting rejection.
            # An unsupported optional query is safe to replace with plain text.
            if not (str(exc).startswith("Firmware rejected a command:") and
                    re.search(r"\b(?:unknown|unsupported) (?:command|gcode):?\s*[\"']?M9001\b", str(exc), re.I)):
                raise
        self.logger.info("Calibration session %s LCD: full-screen support not confirmed; using M117 text", session.id)
        return None

    def _lcd_send(self, session, command, recovery=False):
        from .calibration import TAG, RECOVERY_TAG
        tags = {TAG, f"pinput:session:{session.id}"}
        if recovery:
            tags.add(RECOVERY_TAG)
        self.printer.commands(command, tags=tags)

    def _lcd_credit(self, session, command, identifier, status, sequence, timeout):
        # Do not append M118 here: it preempts a live v2 drawing session.
        self._check(session)
        self._lcd_send(session, command)
        deadline = min(time.monotonic() + timeout, session.lcd_deadline)
        while time.monotonic() < deadline:
            self._check(session)
            try:
                _, line = self.inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            match = re.fullmatch(r"(?:echo:)?M9001 ([A-Z]+) ([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{4})", line.strip())
            if not match or int(match[2], 16) != identifier:
                continue
            if match[1] == status and int(match[3], 16) == sequence:
                return
            if match[1] not in ("ACK", "DONE"):
                raise RuntimeError("LCD drawing rejected: " + match[1])
        raise TimeoutError("LCD did not confirm " + status)

    def _lcd_render(self, session, image):
        identifier = secrets.randbelow(0xFFFFFFFF) + 1
        session.lcd_deadline = time.monotonic() + 90
        completed = False
        try:
            self._lcd_credit(session, packet("B", identifier, b"\1"), identifier, "ACK", 0, 2)
            checksum, sequence = 0, 0
            for data in batches(image):
                self._lcd_credit(session, packet("D", identifier, struct.pack(">H", sequence) + data),
                                 identifier, "ACK", sequence + 1, 3)
                checksum = zlib.crc32(data, checksum) & 0xFFFFFFFF
                sequence += 1
            self._lcd_credit(session, packet("E", identifier, struct.pack(">HI", sequence, checksum)),
                             identifier, "DONE", sequence, 3)
            session.lcd_identifier = identifier
            completed = True
        finally:
            # Drain our own transfer and queue before any subsequent movement.
            # No automatic retry can start another render or excitation.
            if session.connection == self.connection and self.printer.get_state_id() == "OPERATIONAL":
                self._exchange(session, [packet("A", identifier)], recovery=True, timeout=5)
                if not completed:
                    self._exchange(session, [packet("R", identifier)], recovery=True, timeout=5)

    def _lcd_screen(self, session, folder, result=None):
        if session.settings.get("lcdEnabled", True) is False:
            return
        try:
            if result is None:
                session.lcd_version = self._lcd_detect(session)
                self.logger.info("Calibration session %s LCD: %s", session.id,
                                 "M9001 v2 full-screen" if session.lcd_version == 2 else "M117 text")
            self._stage(session, "rendering LCD result" if result is not None else "preparing LCD")
            if session.lcd_version != 2:
                if result is None:
                    message = f"Calibration {session.axis} in progress"
                else:
                    from .lcd_screens import _number
                    summary = f"{session.axis} Resonance: {_number(result.get('base_freq'), 'Hz')}"
                    candidate = result.get("best_shaper") if result.get("recommendation_valid", True) else None
                    detail = f"{candidate} {_number(result.get('best_shaper_freq'), 'Hz')} NOT APPLIED" if candidate else "No recommendation. See host."
                    message = summary.ljust(30) + detail
                # Other firmware handles its own native status-message layout.
                self._exchange(session, ["M117 " + message[:70]])
                session.lcd_text = True
            else:
                from .lcd_screens import render_screen
                image = render_screen(session.settings, session.axis, result)
                image.save(folder / ("lcd_result.png" if result is not None else "lcd_info.png"))
                self._lcd_render(session, image)
            self.logger.info("Calibration session %s LCD %s confirmed before %s", session.id,
                             "result" if result is not None else "test screen", "release" if result is not None else "motion")
        except Exception as exc:
            self.logger.warning("Calibration session %s LCD update skipped: %s", session.id, exc)
            if session.transport_fault:
                raise
            self._check(session)
            session.lcd_version = None

    def _lcd_failure(self, session):
        if (session.kind != "resonance" or not self.error or not (session.lcd_version == 2 or session.lcd_text)
                or session.changed or session.transport_fault or session.connection != self.connection
                or self.printer.get_state_id() != "OPERATIONAL"
                or session.native_sent and not session.native_stopped):
            return
        try:
            if session.lcd_version == 2 and session.lcd_identifier:
                self._exchange(session, [packet("R", session.lcd_identifier)], recovery=True)
            elif session.lcd_text:
                self._exchange(session, [f"M117 Calibration {session.axis} stopped. See OctoPrint."], recovery=True)
        except Exception as exc:
            self.logger.warning("Calibration session %s LCD stop message skipped: %s", session.id, exc)
