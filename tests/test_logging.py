import logging
from types import SimpleNamespace

from octoprint_Pinput_Shaping import PinputShapingPlugin


def plugin_with_logs(tmp_path, path):
    plugin = PinputShapingPlugin()
    plugin._logger = logging.getLogger(f"test.pinput.{tmp_path.name}")
    plugin._logger.setLevel(logging.INFO)
    plugin._settings = SimpleNamespace(get_plugin_logfile_path=lambda: str(path))
    return plugin


def test_dedicated_log_uses_octoprints_configured_folder_and_records_failures(tmp_path):
    folder = tmp_path / "custom-base" / "logs"
    plugin = plugin_with_logs(tmp_path, folder / "plugin_Pinput_Shaping.log")
    plugin.configure_logger()
    try:
        try:
            raise RuntimeError("timing failed")
        except RuntimeError:
            plugin._logger.exception("Calibration session failed")
        text = (folder / "Pinput_Shaping.log").read_text()
        assert "Calibration session failed" in text
        assert "RuntimeError: timing failed" in text
        assert "Input-shaping diagnostics:" in text
    finally:
        plugin.on_shutdown()


def test_repeated_configuration_does_not_duplicate_log_records(tmp_path):
    plugin = plugin_with_logs(tmp_path, tmp_path / "plugin_Pinput_Shaping.log")
    plugin.configure_logger()
    plugin.configure_logger()
    plugin._logger.info("one diagnostic record")
    plugin.on_shutdown()
    plugin.on_shutdown()
    assert (tmp_path / "Pinput_Shaping.log").read_text().count("one diagnostic record") == 1
    assert not plugin._logger.handlers


def test_unavailable_log_folder_falls_back_to_octoprint_logger(tmp_path, caplog):
    unavailable = tmp_path / "not-a-directory"
    unavailable.write_text("preserve this file")
    plugin = plugin_with_logs(tmp_path, unavailable / "plugin_Pinput_Shaping.log")
    plugin.configure_logger()
    assert "Could not open Pinput_Shaping.log" in caplog.text
    assert unavailable.read_text() == "preserve this file"
    assert not plugin._logger.handlers
