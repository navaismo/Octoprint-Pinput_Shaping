import math
import os
from pathlib import Path
import re

import pytest

from octoprint_Pinput_Shaping.motion import MIN_SEGMENT_STEPS, bounds_from_profile, plan_excitation, validate_position


@pytest.mark.parametrize("axis", ["X", "Y"])
def test_excitation_restores_millimeter_strokes_and_acceleration_ramp(snapshot, settings, axis):
    bands = plan_excitation(axis, 110, (0, 220), settings, snapshot)
    assert len(bands) == 8
    assert (bands[0].acceleration, bands[-1].acceleration) == (300, 2500)
    assert (bands[0].amplitude, bands[-1].amplitude) == (5, 1)
    for band in bands:
        assert band.cycles >= 4
        assert 5 <= band.low < band.center < band.high <= 215
        assert band.acceleration <= settings["accelMax"]
        assert band.feedrate <= snapshot.reports["M203"][axis] * 60
        assert math.isclose(band.low * 80, round(band.low * 80), abs_tol=1e-8)
        assert math.isclose(band.high * 80, round(band.high * 80), abs_tol=1e-8)
        assert round(band.amplitude * snapshot.reports["M92"][axis]) >= MIN_SEGMENT_STEPS
        assert band.minimum_duration <= band.duration
        positions = [float(command.split()[1][1:]) for command in band.commands(axis)[1:5]]
        assert positions == [band.high, band.center, band.low, band.center]


def test_analysis_frequency_range_does_not_limit_excitation_motion(snapshot, settings):
    settings.update(accelMax=1500, freqEnd=42)
    before = plan_excitation("X", 110, (0, 220), settings, snapshot)
    settings.update(freqStart=5, freqEnd=132)
    assert plan_excitation("X", 110, (0, 220), settings, snapshot) == before
    assert max(band.acceleration for band in before) == 1500


def test_duration_setting_changes_capture_length_without_changing_analysis_range(snapshot, settings):
    before = plan_excitation("X", 110, (0, 220), settings, snapshot)
    settings["sweepDuration"] = 60
    after = plan_excitation("X", 110, (0, 220), settings, snapshot)
    assert sum(b.duration for b in after) > sum(b.duration for b in before)
    assert settings["freqEnd"] == 132


@pytest.mark.parametrize("center", [0, 4, 216, 220])
def test_entire_excursion_must_fit_inside_travel_margin(snapshot, settings, center):
    with pytest.raises(ValueError):
        plan_excitation("X", center, (0, 220), settings, snapshot)


def test_excitation_respects_speed_acceleration_and_transport_budget(snapshot, settings):
    settings["accelMax"] = snapshot.reports["M201"]["X"] + 1
    with pytest.raises(ValueError, match="must not exceed"):
        plan_excitation("X", 110, (0, 220), settings, snapshot)
    settings["accelMax"] = 1500
    snapshot.reports["M203"]["X"] = 20
    assert all(band.feedrate <= 1200 for band in plan_excitation("X", 110, (0, 220), settings, snapshot))
    with pytest.raises(ValueError, match="time budget"):
        plan_excitation("X", 110, (0, 220), settings, snapshot, baudrate=110)


def test_segment_floor_matches_the_reviewed_marlin_configuration():
    root = os.environ.get("PINPUT_FIRMWARE_ROOT")
    if not root:
        pytest.skip("Set PINPUT_FIRMWARE_ROOT to check the planner segment floor")
    configuration = (Path(root) / "Marlin/Configuration_adv.h").read_text()
    match = re.search(r"^\s*#define\s+MIN_STEPS_PER_SEGMENT\s+(\d+)", configuration, re.MULTILINE)
    assert match and int(match.group(1)) == MIN_SEGMENT_STEPS


@pytest.mark.parametrize("value", ["X\nM112", "Z", None])
def test_invalid_axis_cannot_reach_gcode(profile, value):
    with pytest.raises(ValueError):
        validate_position(profile, value, dict(X=110, Y=110, Z=10))


def test_profile_limits_and_numeric_values_are_authoritative(profile):
    profile["volume"]["custom_box"] = {"x_min": 10, "x_max": 200}
    assert bounds_from_profile(profile)["X"] == (10, 200)
    for value in [float("nan"), float("inf"), "10\nG28", True]:
        with pytest.raises(ValueError):
            validate_position(profile, "X", dict(X=value, Y=110, Z=10))
