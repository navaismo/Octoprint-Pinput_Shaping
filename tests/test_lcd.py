import base64
from copy import deepcopy
import json
import queue
import struct
import types
import zlib

from PIL import Image
import pytest

from octoprint_Pinput_Shaping import lcd
from test_calibration import controller, run
from test_native_calibration import native, detect, run as run_native


def attach_display(controller, version=2, failure=None, fullscreen=True):
    """Actual packet framing, credits, CRCs and rectangle replay on a mock LCD."""
    original = controller.printer.commands
    state = {"version": version, "sid": None, "seq": 0, "crc": 0, "done": [],
             "pixels": [0] * (240 * 320), "color": 0, "data": 0, "canvas": False, "clears": 0}

    def reply(status, sid, sequence):
        controller.printer.emit(f"M9001 {status} {sid:08X} {sequence:04X}")

    def commands(lines, **kwargs):
        lines = [lines] if isinstance(lines, str) else list(lines)
        for command in lines:
            if failure == "abort_fence" and state.get("aborting") and command.startswith("M118 "):
                continue
            original([command], **kwargs)
            if command == "M9001 Q" and version == 2:
                controller.printer.emit("M9001 CAPS 2 42 240 320 00000000 0000")
            elif command == "M9001 QF" and version == 2 and fullscreen:
                controller.printer.emit("M9001 CAPS FULLSCREEN 1 42 240 320 00000000 0000")
            elif command == "M9001 START" and version == 1:
                controller.printer.emit("M9001 START")
            elif command == "M9001 END" and version == 1:
                controller.printer.emit("M9001 END")
                controller.printer.emit("M9001 thumbnail-rendered")
            elif command.startswith("M9001 ") and version == 2 and command not in ("M9001 Q", "M9001 QF"):
                assert len("N2147483647 " + command + "*255") <= 95
                assert "G" not in command[7:]
                kind = command[6]
                raw = base64.b64decode(command[7:].replace("_", "G"), validate=True)
                assert zlib.crc32(kind.encode() + raw[:-4]) & 0xFFFFFFFF == int.from_bytes(raw[-4:], "big")
                assert raw[0] == 2
                sid = int.from_bytes(raw[1:5], "big")
                body = raw[5:-4]
                if kind == "B":
                    assert body == b"\1"
                    state.update(sid=sid, seq=0, crc=0)
                    state["pixels"] = [0] * (240 * 320)
                    state["clears"] += 1
                    state["canvas"] = failure != "busy"
                    reply("BUSY" if failure == "busy" else "ACK", sid, 0)
                elif kind == "A":
                    state["aborting"] = True
                    reply("ABORTED", sid, state["seq"])
                    state["sid"] = None
                elif kind == "R":
                    state["canvas"] = False
                    reply("CLOSED", sid, state["seq"])
                elif kind == "D":
                    assert sid == state["sid"]
                    sequence = int.from_bytes(body[:2], "big")
                    assert sequence == state["seq"]
                    data = body[2:]
                    assert 0 < len(data) <= 42
                    state["data"] += 1
                    if failure in ("crc", "abort_fence"):
                        reply("CRC", sid, sequence)
                        continue
                    if failure == "stale":
                        reply("ACK", sid ^ 0xFFFFFFFF, sequence + 1)
                        continue
                    if failure == "cancel":
                        controller.cancel()
                        continue
                    if failure == "disconnect":
                        controller.printer.state = "OFFLINE"
                        controller.event("Disconnected")
                        continue
                    pos = 0
                    while pos < len(data):
                        if data[pos] == 0:
                            state["color"] = int.from_bytes(data[pos + 1:pos + 3], "big")
                            pos += 3
                        else:
                            op = data[pos]
                            x0, y0, x1, y1 = struct.unpack(">HHHH", data[pos + 1:pos + 9])
                            assert op == 3 and 0 <= x0 <= x1 <= 239 and 0 <= y0 <= y1 <= 319
                            for y in range(y0, y1 + 1):
                                state["pixels"][y * 240 + x0:y * 240 + x1 + 1] = [state["color"]] * (x1 - x0 + 1)
                            pos += 9
                    state["seq"] += 1
                    state["crc"] = zlib.crc32(data, state["crc"]) & 0xFFFFFFFF
                    reply("ACK", sid, sequence + 1)
                elif kind == "E":
                    assert struct.unpack(">HI", body) == (state["seq"], state["crc"])
                    if failure != "missing_done":
                        state["done"].append(len(controller.printer.sent_commands))
                        reply("DONE", sid, state["seq"])

    controller.printer.commands = commands
    # Advance simulated time on missing credits instead of sleeping in tests.
    original_get = controller.inbox.get

    def get(**kwargs):
        try:
            return original_get(block=False)
        except queue.Empty:
            if state["sid"] is not None or failure == "abort_fence" and state.get("aborting"):
                controller.printer.clock.advance(100)
            raise

    controller.inbox.get = get
    return state


@pytest.fixture(autouse=True)
def display_clock(controller, monkeypatch):
    monkeypatch.setattr(lcd, "time", types.SimpleNamespace(monotonic=controller.printer.clock.monotonic))


def test_v2_cards_finish_before_homing_and_after_restoration_with_exact_rgb565(controller):
    state = attach_display(controller)
    original = deepcopy(controller.printer.snapshot)
    identifier = run(controller)
    assert controller.error is None
    assert len(state["done"]) == 2
    sent = controller.printer.sent_commands
    assert state["done"][0] <= sent.index("G28")
    assert original.matches(controller.printer.snapshot)
    folder = controller.data_folder / "runs" / identifier
    expected = Image.open(folder / "lcd_result.png").convert("RGB")
    raw = expected.tobytes()
    values = [((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3) for r, g, b in zip(raw[::3], raw[1::3], raw[2::3])]
    assert state["pixels"] == values
    assert state["canvas"] and state["clears"] == 2
    assert not any(command.startswith("M9000") for command in sent)
    assert not any(command.startswith(("M24", "M25", "M76", "M876")) for command in sent)
    saved = json.loads((folder / "result.json").read_text())
    assert saved["base_freq"] == controller.result["base_freq"]


def test_v1_uses_cleared_native_text_window_with_actual_result(controller):
    state = attach_display(controller, version=1)
    run(controller, axis="Y")
    assert controller.error is None
    sent = controller.printer.sent_commands
    messages = [command for command in sent if command.startswith("M117 ")]
    assert messages[0] == "M117 Calibration Y in progress"
    assert "Y Resonance: 50Hz" in messages[-1]
    assert "ZV 50Hz NOT APPLIED" in messages[-1]
    assert len(messages[-1]) <= 75
    assert sent.index(messages[0]) < sent.index("G28")
    assert state["data"] == 0 and state["done"] == []
    assert not any(command.startswith("M9000") for command in sent)
    assert "M9001 START" not in sent and "M9001 END" not in sent


def test_unrecognized_firmware_uses_only_m117_information_without_image_packets(controller):
    run(controller)
    assert controller.error is None
    sent = controller.printer.sent_commands
    messages = [command for command in sent if command.startswith("M117 ")]
    assert messages[0] == "M117 Calibration X in progress"
    assert "X Resonance: 50Hz" in messages[-1] and "ZV 50Hz NOT APPLIED" in messages[-1]
    assert sent.index(messages[0]) < sent.index("G28")
    assert not any(command.startswith("M9000") for command in sent)
    assert [command for command in sent if command.startswith("M9001 ")] == ["M9001 Q"]


def test_v2_without_fullscreen_extension_uses_m117_and_never_opens_the_print_card(controller):
    state = attach_display(controller, fullscreen=False)
    run(controller)
    assert controller.error is None
    assert state["data"] == 0 and not state["canvas"]
    assert any(command.startswith("M117 ") for command in controller.printer.sent_commands)
    assert not any(command.startswith(("M9000", "M9001 B"))
                   for command in controller.printer.sent_commands)


@pytest.mark.parametrize("rejected", ["M9001 Q", "M9001 QF"])
def test_unknown_optional_query_falls_back_to_text_after_its_queue_fence(controller, rejected):
    original = controller.printer.commands

    def commands(lines, **kwargs):
        for command in [lines] if isinstance(lines, str) else lines:
            original([command], **kwargs)
            if command == rejected:
                controller.printer.emit(f'echo:Unknown command: "{command}"')
            elif command == "M9001 Q":
                controller.printer.emit("M9001 CAPS 2 42 240 320 00000000 0000")

    controller.printer.commands = commands
    run(controller)
    assert controller.error is None
    sent = controller.printer.sent_commands
    assert len([command for command in sent if command.startswith("M117 ")]) == 2
    assert not any(command.startswith(("M9000", "M9001 B", "M9001 D", "M9001 E", "M9001 A", "M9001 R")) for command in sent)


@pytest.mark.parametrize("capabilities", ["M9001 CAPS FULLSCREEN 2 42 240 320 00000000 0000",
                                       "M9001 CAPS FULLSCREEN 1 42 272 480 00000000 0000",
                                       "M9001 CAPS FULLSCREEN 1 42 240 320",
                                       "FIRMWARE_NAME:Marlin c13_octoprint_cleanup"])
def test_firmware_names_or_incompatible_fullscreen_replies_never_authorize_images(controller, capabilities):
    original = controller.printer.commands

    def commands(lines, **kwargs):
        for command in [lines] if isinstance(lines, str) else lines:
            original([command], **kwargs)
            if command == "M9001 Q":
                controller.printer.emit("M9001 CAPS 2 42 240 320 00000000 0000")
            elif command == "M9001 QF":
                controller.printer.emit(capabilities)

    controller.printer.commands = commands
    run(controller)
    assert controller.error is None
    assert any(command.startswith("M117 ") for command in controller.printer.sent_commands)
    assert not any(command.startswith(("M9000", "M9001 B", "M9001 D")) for command in controller.printer.sent_commands)


def test_disabled_display_sends_no_probes_or_display_commands(controller, settings):
    settings["lcdEnabled"] = False
    run(controller)
    assert controller.error is None
    assert not any(command.startswith(("M900", "M117 ")) for command in controller.printer.sent_commands)


@pytest.mark.parametrize("failure", ["busy", "crc", "stale", "missing_done"])
def test_display_failure_is_aborted_and_drained_before_motion(controller, failure):
    state = attach_display(controller, failure=failure)
    run(controller)
    assert controller.error is None
    assert state["sid"] is None
    sent = controller.printer.sent_commands
    abort = next(i for i, command in enumerate(sent) if command.startswith("M9001 A"))
    assert sent[abort + 1].startswith("M118 ")
    assert abort < sent.index("G28")
    assert sum(command.startswith("M9001 B") for command in sent) == 1
    assert not state["canvas"]


def test_cancel_during_display_never_starts_software_motion(controller):
    attach_display(controller, failure="cancel")
    run(controller)
    assert "cancelled" in controller.error
    assert "G28" not in controller.printer.sent_commands
    assert not controller.journal.exists()


@pytest.mark.parametrize("failure", ["disconnect", "abort_fence"])
def test_uncertain_lcd_queue_or_disconnect_cannot_start_calibration_motion(controller, failure):
    attach_display(controller, failure=failure)
    run(controller)
    assert controller.error
    assert "G28" not in controller.printer.sent_commands
    assert controller.result is None
    assert not controller.printer.hold


def test_v1_cancelled_calibration_clears_in_progress_text_after_restoration(controller):
    attach_display(controller, version=1)
    original = deepcopy(controller.printer.snapshot)
    controller._publish_callback = lambda event: controller.cancel() if event["type"] == "progress" else None
    run(controller)
    assert "cancelled" in controller.error
    assert original.matches(controller.printer.snapshot)
    assert controller.printer.sent_commands[-2].startswith("M117 Calibration X stopped")


def test_failed_calibration_releases_fullscreen_after_confirmed_restoration(controller):
    state = attach_display(controller)
    original = deepcopy(controller.printer.snapshot)
    controller._publish_callback = lambda event: controller.cancel() if event["type"] == "progress" else None
    run(controller)
    assert "cancelled" in controller.error
    assert original.matches(controller.printer.snapshot)
    assert not state["canvas"]
    assert any(command.startswith("M9001 R") for command in controller.printer.sent_commands)


@pytest.mark.parametrize("display_version", [1, 2])
def test_native_v2_display_never_overlaps_the_arming_barrier_or_excitation(native, display_version):
    state = attach_display(native, version=display_version)
    detect(native)
    run_native(native)
    assert native.error is None
    assert len(state["done"]) == (2 if display_version == 2 else 0)
    sent = native.printer.sent_commands
    start = next(i for i, command in enumerate(sent) if command.startswith("M495 X") and " S W" in command)
    arm = next(i for i, command in enumerate(sent) if command.startswith("M495 I") and command.endswith(" R"))
    assert start < arm
    if display_version == 2:
        assert state["done"][0] <= start < arm < state["done"][1]
    else:
        assert any(command.startswith("M117 ") for command in sent[:start])
        assert any(command.startswith("M117 ") for command in sent[arm+1:])
    assert not any(command.startswith(("M900", "M117 ")) for command in sent[start:arm + 1])


def test_reconnect_does_not_reuse_cached_v2_support(controller):
    state = attach_display(controller)
    run(controller)
    assert len(state["done"]) == 2
    sent = len(controller.printer.sent_commands)
    controller.event("Connected")
    # Replace display responder with a firmware that does not support M9001.
    from fakes import Printer
    controller.printer.commands = Printer.commands.__get__(controller.printer, Printer)
    run(controller)
    assert controller.error is None
    assert "M9001 Q" in controller.printer.sent_commands[sent:]
    assert not any(command.startswith("M9001 B") for command in controller.printer.sent_commands[sent:])
