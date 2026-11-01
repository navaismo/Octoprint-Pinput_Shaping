"""Check real OctoPrint registration and transport without opening a device."""

from pathlib import Path
from types import SimpleNamespace

import flask
import octoprint.plugin
import pytest

import octoprint_Pinput_Shaping as plugin_module
from octoprint_Pinput_Shaping.calibration import TAG


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
