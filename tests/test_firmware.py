from copy import deepcopy

import pytest

from octoprint_Pinput_Shaping.firmware import FirmwareSnapshot, SHAPER_IDS, finite_number, shaping_command


@pytest.mark.parametrize("value", [True, None, "bad", float("nan"), float("inf"), -float("inf")])
def test_non_finite_input_is_rejected(value):
    with pytest.raises(ValueError):
        finite_number(value, "parameter")


def test_report_order_does_not_drop_an_axis(snapshot):
    assert snapshot.mode == "ftm"
    assert snapshot.ftm_axes["X"]["C"] == 0
    assert snapshot.ftm_axes["Y"]["C"] == 7
    assert snapshot.feed_percent == 85
    assert snapshot.disable_commands() == ["M493 X C0", "M493 Y C0"]


def test_restore_latent_settings_uses_ei_before_disabling_axis(snapshot):
    commands = snapshot.restore_commands()
    assert commands[:2] == ["M493 X C5 A37 I0.1 Q0.05", "M493 X C0"]
    assert "M204 T3000" in commands
    assert commands[-1] == "M220 S85"


@pytest.mark.parametrize("name,identifier", SHAPER_IDS.items())
def test_c13_shaper_ids_and_axis_scoping(name, identifier):
    command = shaping_command("ftm", "Y", name, 53.5, 0.05)
    assert command.startswith(f"M493 Y C{identifier} A53.500 I0.0500")
    assert "X" not in command
    assert ("Q0.0500" in command) == (name in ("EI", "2HUMP_EI", "3HUMP_EI"))


def test_legacy_firmware_can_only_receive_zv():
    assert shaping_command("legacy", "X", "ZV", 50, 0.05) == "M593 X F50.000 D0.0500"
    with pytest.raises(ValueError):
        shaping_command("legacy", "X", "MZV", 50, 0.05)


@pytest.mark.parametrize("field,value", [("ftm_version", 1), ("trajectory", 2), ("feed_percent", None)])
def test_incomplete_or_unsupported_firmware_is_rejected(snapshot, field, value):
    setattr(snapshot, field, value)
    with pytest.raises(ValueError):
        snapshot.validate("X")


def test_readback_checks_settings_on_other_axes(snapshot):
    current = deepcopy(snapshot)
    current.ftm_axes["Y"]["A"] = 99
    assert not snapshot.matches(current)


def test_normal_restoration_preserves_untouched_parameter_precision(snapshot):
    commands = snapshot.restore_commands(shaping_parameters=False)
    assert commands[:2] == ["M493 X C0", "M493 Y C7"]
    assert "M204 T3000" in commands
    assert not any(" A" in cmd or " I" in cmd or " Q" in cmd for cmd in commands)
    assert not any(" J" in cmd or " P" in cmd or " R" in cmd for cmd in commands)


def test_changed_step_resolution_or_motion_limits_invalidate_a_result(snapshot):
    for code in ("M92", "M201", "M203"):
        current = deepcopy(snapshot)
        current.reports[code]["X"] *= 2
        assert not snapshot.matches(current)


def test_legacy_report_accepts_y_before_x():
    snapshot = FirmwareSnapshot()
    snapshot.feed("echo: M593 Y F42.00 D0.1")
    snapshot.feed("echo: M593 X F37.00 D0.05")
    assert set(snapshot.legacy_axes) == {"X", "Y"}


def test_dynamic_frequency_and_incomplete_backups_are_rejected(snapshot):
    snapshot.ftm_global["D"] = 1
    with pytest.raises(ValueError, match="static"):
        snapshot.validate("X")
    snapshot.ftm_global["D"] = 0
    del snapshot.reports["M204"]["R"]
    with pytest.raises(ValueError, match="Incomplete M204"):
        snapshot.validate("X")


def test_standard_motion_disables_and_restores_legacy_shapers_without_changing_ftm(snapshot):
    snapshot.ftm_global["S"] = 0
    snapshot.legacy_axes = {"X": {"F": 37.123456, "D": 0.1}, "Y": {"F": 42, "D": 0.07}}
    snapshot.validate("X", allow_standard=True)
    assert snapshot.disable_commands() == ["M593 X F0", "M593 Y F0"]
    commands = snapshot.restore_commands(shaping_parameters=False)
    assert commands[:2] == ["M593 X F37.123456", "M593 Y F42"]
    assert not any(cmd.startswith("M493") or " D" in cmd for cmd in commands)
    rollback = snapshot.restore_commands(shaping_parameters=True)
    assert "M493 S0 H1" in rollback
    assert "M593 X F37.123456 D0.1" in rollback
    assert "M593 Y F42 D0.07" in rollback


def test_standard_motion_without_legacy_shapers_does_not_invent_settings(snapshot):
    snapshot.ftm_global["S"] = 0
    snapshot.validate("X", allow_standard=True)
    assert snapshot.disable_commands() == []
    assert not any(cmd.startswith(("M493", "M593"))
                   for cmd in snapshot.restore_commands(shaping_parameters=False))


@pytest.mark.parametrize("values", [{"F": 37}, {"D": 0.05}, {"F": -1, "D": 0.05},
                                   {"F": 37, "D": float("nan")}])
def test_standard_motion_rejects_incomplete_or_invalid_legacy_backups(snapshot, values):
    snapshot.ftm_global["S"] = 0
    snapshot.legacy_axes = {"X": values}
    with pytest.raises(ValueError, match="legacy"):
        snapshot.validate("X", allow_standard=True)


@pytest.mark.parametrize("mode", [None, -1, 2])
def test_missing_or_invalid_ftm_motion_mode_is_not_assumed(snapshot, mode):
    if mode is None:
        del snapshot.ftm_global["S"]
    else:
        snapshot.ftm_global["S"] = mode
    with pytest.raises(ValueError, match="M493 motion mode"):
        snapshot.validate("X", allow_standard=True)
