import json

import numpy as np
import pandas as pd
import pytest

from octoprint_Pinput_Shaping.native_analysis import NativeSweepAnalyzer, capture_metadata
from octoprint_Pinput_Shaping.native_protocol import NativeParameters
from octoprint_Pinput_Shaping.inputshaping_analyzer import MeasurementError


def sweep(path, parameters=None, rate=800, noise=0.002, gain=4, harmonic=0, duplicate=False, truncate=0, resonances=None):
    p = parameters or NativeParameters.from_settings({})
    lead, tail = 3, 2
    length = lead + p.duration + tail
    t = np.arange(int((length * (2 if duplicate else 1) - truncate) * rate)) / rate
    rng = np.random.default_rng(13)
    axes = rng.normal(0, noise, (len(t), 3))
    for offset in ([lead, length + lead] if duplicate else [lead]):
        relative = t - offset
        frequency = p.start * 2 ** (relative / p.octave)
        phase = 2 * np.pi * p.start * p.octave / np.log(2) * (2 ** (relative / p.octave) - 1)
        response = 1 + sum(strength * np.exp(-0.5 * ((frequency - center) / width) ** 2)
                           for center, width, strength in (resonances or [(40, 3, gain)]))
        signal = 0.04 * frequency / p.start * (response * np.sin(phase) + harmonic * np.sin(2 * phase))
        axes[:, 0] += np.where((relative >= 0) & (relative <= p.duration), signal, 0)
    axes[t < 1.5, 0] += 0.5 * np.sin(2 * np.pi * 7 * t[t < 1.5])
    axes[:, 2] += 1
    frame = pd.DataFrame({"time": t + 1234, "x": axes[:, 0], "y": axes[:, 1], "z": axes[:, 2]})
    frame.to_csv(path, index=False)
    return len(frame)


def analyzer(path, **kwargs):
    p = NativeParameters.from_settings({})
    return NativeSweepAnalyzer(path.parent, path, native_parameters=p, host_interval=p.duration + 0.1, **kwargs)


def test_native_chirp_finds_mechanical_peak_and_excludes_homing(tmp_path):
    path = tmp_path / "capture.csv"
    sweep(path)
    result = analyzer(path)
    result.analyze()
    assert result.recommendation_valid
    assert abs(result.base_freq - 40) < 1.5
    assert result.quality["coverage"] >= 0.85
    assert result.quality["alignment"]["sensor_start"] == pytest.approx(3, abs=0.6)
    assert result.time[-1] < result.parameters.duration
    assert result.get_recommendation().startswith("M493 X")
    result.generate_graphs()
    assert (tmp_path / "psd.png").stat().st_size > 1000


def test_harmonics_do_not_replace_the_fundamental_resonance(tmp_path):
    path = tmp_path / "capture.csv"
    sweep(path, harmonic=4)
    result = analyzer(path)
    result.analyze()
    assert abs(result.base_freq - 40) < 1.5


@pytest.mark.parametrize("options", [{"duplicate": True}, {"truncate": 10}, {"noise": 1, "gain": 0}])
def test_ambiguous_incomplete_and_low_snr_sweeps_are_rejected(tmp_path, options):
    path = tmp_path / "capture.csv"
    sweep(path, **options)
    with pytest.raises(MeasurementError):
        analyzer(path).analyze()


def test_short_capture_has_no_provisional_missing_initial_samples_allowance(tmp_path):
    path = tmp_path / "capture.csv"
    sweep(path)
    p = NativeParameters.from_settings({})
    frame = pd.read_csv(path)
    frame = frame[(frame.time >= 1234 + 3 + 0.25) & (frame.time <= 1234 + 3 + p.duration + 0.1)]
    frame.to_csv(path, index=False)
    with pytest.raises(MeasurementError, match="complete native sweep duration"):
        analyzer(path).analyze()


def test_flat_response_can_show_diagnostics_without_a_recommendation(tmp_path):
    path = tmp_path / "capture.csv"
    sweep(path, gain=0)
    result = analyzer(path)
    result.analyze()
    assert not result.recommendation_valid
    assert result.best_shaper is None
    assert result.get_plotly_data()["best_shaper_freq"] is None


def test_multiple_resonances_are_tracked_in_the_measured_band(tmp_path):
    path = tmp_path / "capture.csv"
    sweep(path, resonances=[(32, 1.5, 4), (48, 2, 5)])
    result = analyzer(path)
    result.analyze()
    assert result.recommendation_valid
    assert any(abs(f - 32) < 1.5 for f in result.quality["resonances"])
    assert any(abs(f - 48) < 1.5 for f in result.quality["resonances"])
    assert all(result.quality["optimizer_band"][0] <= item["shaper_freq"] <= result.quality["optimizer_band"][1]
               for item in result.shaper_results.values())


def test_persistent_background_tone_cannot_create_a_resonance_recommendation(tmp_path):
    path = tmp_path / "capture.csv"
    sweep(path, gain=0)
    frame = pd.read_csv(path)
    frame["x"] += np.sin(2 * np.pi * 40 * frame.time)
    frame.to_csv(path, index=False)
    result = analyzer(path)
    try:
        result.analyze()
    except MeasurementError:
        pass
    assert not result.recommendation_valid


@pytest.mark.parametrize("failure", ["clipping", "partial_clipping", "timestamps", "sampling", "capture_duration"])
def test_invalid_native_capture_quality_never_reaches_optimizer(tmp_path, failure):
    path = tmp_path / "capture.csv"
    sweep(path, rate=100 if failure == "sampling" else 800)
    if failure in ("clipping", "partial_clipping", "timestamps"):
        frame = pd.read_csv(path)
        if failure == "clipping":
            frame["x"] = np.clip(frame["x"] * 30, -0.5, 0.5)
        elif failure == "partial_clipping":
            clipped = (frame.time >= 1234 + 23) & (frame.time <= 1234 + 26)
            frame.loc[clipped, "x"] = np.clip(frame.loc[clipped, "x"] * 30, -0.5, 0.5)
        else:
            frame.loc[1, "time"] = frame.loc[0, "time"]
        frame.to_csv(path, index=False)
    result = analyzer(path, capture_interval=100 if failure == "capture_duration" else None)
    with pytest.raises(MeasurementError):
        result.analyze()
    assert not result.shaper_results


def test_dominant_boundary_response_needs_a_wider_measurement(tmp_path):
    path = tmp_path / "capture.csv"
    sweep(path, resonances=[(60, 3, 20), (32, 1.5, 4)])
    result = analyzer(path)
    result.analyze()
    assert not result.recommendation_valid
    assert any("boundary" in warning for warning in result.warnings)


def test_protocol_metadata_must_be_present_valid_and_match_csv(tmp_path):
    path = tmp_path / "capture.csv"
    with pytest.raises(MeasurementError):
        capture_metadata(path, "lis2dw", 800)
    sidecar = tmp_path / "capture.csv.meta.json"
    metadata = {"protocol": 2, "valid": True, "requested_rate": 800, "effective_rate": 800,
                "samples": 1000, "max_window_us": 20, "units": "g", "counts_per_g": 16384, "clock": "rp2040_fifo_poll"}
    sidecar.write_text(json.dumps(metadata))
    assert capture_metadata(path, "lis2dw", 800, 1000)["valid"]
    with pytest.raises(MeasurementError):
        capture_metadata(path, "lis2dw", 800, 999)
    metadata["valid"] = False
    sidecar.write_text(json.dumps(metadata))
    with pytest.raises(MeasurementError):
        capture_metadata(path, "lis2dw", 800)
