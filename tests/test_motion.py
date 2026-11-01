import math

import pytest

from octoprint_Pinput_Shaping.motion import bounds_from_profile, plan_sweep, validate_position


@pytest.mark.parametrize("axis", ["X", "Y"])
def test_sweep_frequency_and_excursion_follow_the_requested_plan(snapshot, settings, axis):
    bands = plan_sweep(axis, 110, (0, 220), settings, snapshot)
    assert (bands[0].frequency, bands[-1].frequency) == (20, 70)
    for band in bands:
        length = band.high - band.low
        assert math.isclose(1 / (4 * math.sqrt(length / band.acceleration)), band.frequency)
        assert band.acceleration <= settings["accelMax"]
        assert 5 <= band.low < band.high <= 215
        assert math.isclose(band.low * 80, round(band.low * 80), abs_tol=1e-8)
        assert math.isclose(band.high * 80, round(band.high * 80), abs_tol=1e-8)


def test_frequency_settings_change_the_generated_motion(snapshot, settings):
    before = plan_sweep("X", 110, (0, 220), settings, snapshot)
    settings["freqEnd"] = 60
    after = plan_sweep("X", 110, (0, 220), settings, snapshot)
    assert before != after


@pytest.mark.parametrize("center", [0, 4, 216, 220])
def test_entire_excursion_must_fit_inside_travel_margin(snapshot, settings, center):
    with pytest.raises(ValueError):
        plan_sweep("X", center, (0, 220), settings, snapshot)


def test_unachievable_frequency_and_serial_bandwidth_fail_before_motion(snapshot, settings):
    settings["freqEnd"] = 132
    with pytest.raises(ValueError, match="step/acceleration"):
        plan_sweep("X", 110, (0, 220), settings, snapshot)
    settings["freqEnd"] = 70
    with pytest.raises(ValueError, match="bandwidth"):
        plan_sweep("X", 110, (0, 220), settings, snapshot, baudrate=19200)


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
