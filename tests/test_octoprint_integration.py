"""Check real OctoPrint registration and transport without opening a device."""

from pathlib import Path
import logging
import threading
from types import SimpleNamespace

import flask
import octoprint.plugin
import pytest

import octoprint_Pinput_Shaping as plugin_module
from octoprint_Pinput_Shaping.calibration import CalibrationController, Session, TAG, RECOVERY_TAG


pytestmark = pytest.mark.skipif(
    not getattr(octoprint.plugin, "__file__", None),
    reason="Install OctoPrint to exercise its real plugin interfaces",
)


def test_real_plugin_load_registers_hooks_and_download_route():
    plugin_module.__plugin_load__()
    plugin = plugin_module.__plugin_implementation__
    plugin._identifier = "Pinput_Shaping"
    plugin._basefolder = str(Path(plugin_module.__file__).parent)
    assert isinstance(plugin, octoprint.plugin.SimpleApiPlugin)
    assert isinstance(plugin, octoprint.plugin.ShutdownPlugin)
    assert plugin_module.__plugin_hooks__["octoprint.comm.protocol.gcode.queuing"].__self__ is plugin
    assert plugin_module.__plugin_hooks__["octoprint.comm.protocol.gcode.received"].__self__ is plugin
    assert plugin_module.__plugin_hooks__["octoprint.comm.protocol.gcode.sending"].__self__ is plugin
    app = flask.Flask(__name__)
    app.register_blueprint(plugin.get_blueprint(), url_prefix="/plugin/Pinput_Shaping")
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/plugin/Pinput_Shaping/files/<run_id>/<filename>" in rules


def test_real_printer_commands_preserve_session_tags():
    from octoprint.printer.standard import Printer

    sent = []
    printer = Printer.__new__(Printer)
    printer._comm = SimpleNamespace(sendCommand=lambda command, **kwargs: sent.append((command, kwargs)))
    printer.commands(["M400", "M118 session-marker"], tags={TAG, "pinput:session:test"})
    assert [command for command, _ in sent] == ["M400", "M118 session-marker"]
    assert all({TAG, "pinput:session:test", "trigger:printer.commands"} <= options["tags"]
               for _, options in sent)


def test_real_sent_hook_defers_native_logging_to_the_worker(tmp_path):
    from octoprint.util.comm import MachineCom

    writing_from_hook = True
    records = []
    def info(message, *args):
        assert not writing_from_hook, "Serial hooks must not write plugin diagnostics"
        records.append(message % args)
    controller = CalibrationController(None, lambda: {}, lambda: {}, tmp_path, lambda payload: None,
                                       logger=SimpleNamespace(info=info))
    session = Session("resonance", "X", {}, {}, {}, controller.connection,
                      native_mode=True, firmware_id=123)
    controller.session = session
    plugin = plugin_module.PinputShapingPlugin()
    plugin.controller = controller
    comm = SimpleNamespace(isStreaming=lambda: False, isPrinting=lambda: False,
        _log_command_phase=lambda *args, **kwargs: None, _logger=logging.getLogger(__name__),
        _gcode_hooks={"sent": {"Pinput_Shaping": plugin.gcode_sent_handler}})
    MachineCom._process_command_phase(comm, "sent", "M495 X F20 T60 O20 A40 I123 S", gcode="M495",
                                      tags={TAG, f"pinput:session:{session.id}"})
    assert session.native_sent
    assert not records
    writing_from_hook = False
    controller._native_log_transmissions(session)
    assert len(records) == 1
    assert "native TX" in records[0]
    assert "I123 S" in records[0]


def test_real_sending_phase_drops_cancelled_or_stale_motion_and_allows_restoration(tmp_path):
    from octoprint.util.comm import MachineCom

    plugin = plugin_module.PinputShapingPlugin()
    controller = CalibrationController(None, lambda: {}, lambda: {}, tmp_path, lambda payload: None)
    plugin.controller = controller
    controller.session = SimpleNamespace(id="test", connection=controller.connection, cancelled=threading.Event())
    comm = SimpleNamespace(isStreaming=lambda: False, isPrinting=lambda: False,
        _log_command_phase=lambda *args, **kwargs: None, _logger=logging.getLogger(__name__),
        _gcode_hooks={"sending": {"Pinput_Shaping": plugin.gcode_sending_handler}})
    tags = {TAG, "pinput:session:test"}
    def process(command, gcode, command_tags):
        return MachineCom._process_command_phase(comm, "sending", command, gcode=gcode, tags=command_tags)
    assert process("G1 X111", "G1", tags)
    controller.session.cancelled.set()
    assert process("G1 X111", "G1", tags) == []
    assert process("M204 T1500", "M204", tags) == []
    assert process("M204 T3000", "M204", tags | {RECOVERY_TAG})
    assert process("M400", "M400", tags)
    assert process("M112", "M112", {TAG})
    controller.session = None
    assert process("G1 X111", "G1", tags) == []
    assert process("M204 T3000", "M204", tags | {RECOVERY_TAG}) == []
