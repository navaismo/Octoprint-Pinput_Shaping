import math

import numpy as np
import pandas as pd
import pytest

from octoprint_Pinput_Shaping.inputshaping_analyzer import InputShapingAnalyzer, MeasurementError


def capture(tmp_path, frequencies=(50,), amplitudes=(1,), rate=800, seconds=8, noise=0.01):
    times = np.arange(int(rate * seconds)) / rate
    rng = np.random.default_rng(23)
    signal = sum(a * np.sin(2 * np.pi * f * times) for f, a in zip(frequencies, amplitudes))
    frame = pd.DataFrame({"time": times, "x": signal + rng.normal(0, noise, len(times)),
                          "y": rng.normal(0, noise, len(times)), "z": 1 + rng.normal(0, noise, len(times))})
    path = tmp_path / "capture.csv"
    frame.to_csv(path, index=False)
    return path


def test_ideal_zv_cancels_the_tuned_damped_mode(tmp_path):
    analyzer = InputShapingAnalyzer(tmp_path, tmp_path / "unused.csv", firmware_mode="legacy")
    shaper = analyzer.generate_shapers(50)["ZV"]
    assert analyzer._calculate_residual_vibration(shaper, 50) < 1e-12


def test_coefficients_are_normalized_and_c13_taps_are_quantized(tmp_path):
    analyzer = InputShapingAnalyzer(tmp_path, tmp_path / "unused.csv")
    expected = {"ZV": 2, "ZVD": 3, "ZVDD": 4, "ZVDDD": 5,
                "EI": 3, "2HUMP_EI": 4, "3HUMP_EI": 5, "MZV": 3}
    for name, shaper in analyzer.generate_shapers(37).items():
        assert len(shaper) == expected[name]
        assert math.isclose(sum(gain for _, gain in shaper), 1)
        assert all(math.isclose(t * 1000, round(t * 1000)) for t, _ in shaper)


def test_stronger_resonance_outside_40_to_70_hz_wins(tmp_path):
    path = capture(tmp_path, (50, 90), (0.2, 1))
    analyzer = InputShapingAnalyzer(tmp_path, path, max_freq=120)
    analyzer.analyze()
    assert abs(analyzer.base_freq - 90) < 1
    assert analyzer.get_recommendation().startswith("M493 X ")
    assert all(item["suggested_accel"] <= 2500 for item in analyzer.shaper_results.values())
    assert all("accel" not in item for item in analyzer.shaper_results.values())


@pytest.mark.parametrize("rate", [797.194228, 1594.092351])
def test_measured_lis2dw_rates_cover_the_full_supported_analysis_range(tmp_path, rate):
    analyzer = InputShapingAnalyzer(tmp_path, capture(tmp_path, frequencies=(100,), rate=rate), max_freq=200)
    analyzer.analyze()
    assert analyzer.quality["sampling_rate"] == pytest.approx(rate)
    assert abs(analyzer.base_freq - 100) < 1


def test_legacy_result_is_zv_and_raw_reference_is_preserved(tmp_path):
    analyzer = InputShapingAnalyzer(tmp_path, capture(tmp_path), firmware_mode="legacy")
    analyzer.analyze()
    assert analyzer.best_shaper == "ZV"
    assert analyzer.get_recommendation().startswith("M593 X F")
    frequency, reference = analyzer.compute_psd(analyzer.raw_axes[:, 0])
    expected = sum(analyzer.compute_psd(analyzer.raw_axes[:, i])[1] for i in range(3))
    np.testing.assert_allclose(analyzer.psd, expected)


@pytest.mark.parametrize("defect", ["duplicate", "backwards", "gap", "nan", "short", "flat", "noise", "clipped"])
def test_invalid_captures_never_produce_recommendations(tmp_path, defect):
    path = capture(tmp_path)
    frame = pd.read_csv(path)
    if defect == "duplicate": frame.loc[100, "time"] = frame.loc[99, "time"]
    elif defect == "backwards": frame.loc[100, "time"] = -1
    elif defect == "gap": frame.loc[100:, "time"] += 1
    elif defect == "nan": frame.loc[100, "x"] = np.nan
    elif defect == "short": frame = frame.iloc[:300]
    elif defect == "flat": frame[["x", "y", "z"]] = 0
    elif defect == "noise": frame["x"] = np.random.default_rng(9).normal(size=len(frame))
    elif defect == "clipped": frame["x"] = np.clip(frame["x"], -0.3, 0.3)
    frame.to_csv(path, index=False)
    with pytest.raises(MeasurementError):
        InputShapingAnalyzer(tmp_path, path).analyze()


def test_combined_spectrum_is_invariant_to_sensor_rotation(tmp_path):
    path = capture(tmp_path)
    a = InputShapingAnalyzer(tmp_path, path)
    a.analyze()
    frame = pd.read_csv(path)
    angle = 0.7
    rotation = np.array([[math.cos(angle), -math.sin(angle), 0],
                         [math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
    frame[["x", "y", "z"]] = frame[["x", "y", "z"]].to_numpy() @ rotation
    frame.to_csv(path, index=False)
    b = InputShapingAnalyzer(tmp_path, path)
    b.analyze()
    np.testing.assert_allclose(a.psd, b.psd, atol=1e-10)
    assert a.best_shaper == b.best_shaper


def test_graphs_and_complete_result_are_exportable(tmp_path):
    analyzer = InputShapingAnalyzer(tmp_path, capture(tmp_path))
    analyzer.analyze()
    signal, psd, _, _, measured = analyzer.generate_graphs()
    assert measured == analyzer.base_freq
    assert (tmp_path / "signal.png").stat().st_size > 1000
    assert (tmp_path / "psd.png").stat().st_size > 1000
    data = analyzer.get_plotly_data()
    assert data["best_shaper_freq"] == analyzer.shaper_results[analyzer.best_shaper]["shaper_freq"]
