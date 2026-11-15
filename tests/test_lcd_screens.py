from copy import deepcopy

from PIL import Image, ImageDraw
import pytest

from octoprint_Pinput_Shaping import lcd_screens as screens


@pytest.fixture
def lcd_settings():
    return {"excitationBackend": "marlin_m495", "nativeStart": 20, "nativeEnd": 60,
            "sensorType": "lis2dw", "lis2dwRate": 800, "lis2dwRange": 16,
            "freqStart": 5, "freqEnd": 132}


@pytest.fixture
def lcd_result():
    return {"base_freq": 43.2, "best_shaper": "MZV", "best_shaper_freq": 47.5,
            "recommendation_valid": True, "freqs": [20, 30, 40, 50, 60],
            "psd_original": [1, 3, 10, 2, 1],
            "shapers": {"MZV": {"psd": [0.2, 0.5, 1, 0.1, 0.1]}}}


@pytest.mark.parametrize("size", [(240, 320)])
@pytest.mark.parametrize("axis", ["X", "Y"])
def test_host_cards_preserve_dimensions_and_use_different_real_results(lcd_settings, lcd_result, size, axis):
    settings, result = deepcopy(lcd_settings), deepcopy(lcd_result)
    before = screens.render_screen(settings, axis, size=size)
    after = screens.render_screen(settings, axis, result, size=size)
    assert before.size == after.size == size
    assert before.mode == after.mode == "RGB"
    assert before.tobytes() != after.tobytes()
    assert settings == lcd_settings and result == lcd_result
    # The original logo keeps its gradients; the data area still uses solid colors.
    assert len(after.crop((0, 0, 240, 58)).getcolors(240 * 58)) > 6
    assert len(after.crop((0, 58, 240, 320)).getcolors(240 * 262)) <= 6


@pytest.mark.parametrize("size", [(240, 320)])
def test_result_labels_come_from_measurement_not_reference_image(lcd_settings, lcd_result, size, monkeypatch):
    labels = []
    original = screens._text

    def record(draw, xy, text, *args, **kwargs):
        labels.append(str(text))
        original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(screens, "_text", record)
    screens.render_screen(lcd_settings, "Y", lcd_result, size=size)
    joined = " ".join(labels)
    assert "43.2 Hz" in joined
    assert "MZV" in joined
    assert "Y" in joined
    assert "NOT APPLIED" in joined
    assert "52.9" not in joined and "3HUMP_EI" not in joined


def test_software_card_uses_software_band_and_selected_adxl_rate(lcd_settings, monkeypatch):
    lcd_settings.update(excitationBackend="software", sensorType="adxlusb", adxlRate=1600)
    labels = []
    monkeypatch.setattr(screens, "_text", lambda draw, xy, text, *args, **kwargs: labels.append(str(text)))
    screens.render_screen(lcd_settings, "X")
    text = " ".join(labels)
    assert "5-132 Hz" in text and "Software" in text
    assert "ADXL345 USB" in text and "1600 Hz" in text and "+/-16 g" in text
    assert "20-60" not in text and "Native M495" not in text


def test_diagnostic_result_never_claims_a_recommendation(lcd_settings, lcd_result, monkeypatch):
    lcd_result.update(recommendation_valid=False, best_shaper=None, best_shaper_freq=None)
    labels = []
    monkeypatch.setattr(screens, "_text", lambda draw, xy, text, *args, **kwargs: labels.append(str(text)))
    screens.render_screen(lcd_settings, "X", lcd_result)
    assert "DIAGNOSTICS" in labels
    assert "COMPLETE" not in labels and "MZV" not in labels


@pytest.mark.parametrize("power", [[0, 1], [1, -1, 1], [1, float("nan"), 1], [0, 0, 0]])
def test_invalid_spectra_are_not_drawn_as_measured_data(power):
    image = Image.new("RGB", (96, 96), screens.BLACK)
    assert not screens._spectrum(ImageDraw.Draw(image), (0, 0, 95, 95),
                                 {"freqs": [20, 30, 40], "psd_original": power}, (20, 40))
    assert screens.CYAN not in [color for count, color in image.getcolors(96 * 96)]


def test_graph_excludes_power_outside_the_test_band():
    image = Image.new("RGB", (100, 100), screens.BLACK)
    result = {"freqs": [0, 20, 30, 40, 60], "psd_original": [1e6, 1, 10, 1, 1e6]}
    assert screens._spectrum(ImageDraw.Draw(image), (0, 0, 99, 99), result, (20, 40))
    assert image.getpixel((50, 2)) == screens.CYAN


@pytest.mark.parametrize("size,axis", [((272, 480), "X"), ((240, 320), "Z"), ((240, 98), "X")])
def test_unsupported_lcd_geometry_and_axis_are_rejected(lcd_settings, size, axis):
    with pytest.raises(ValueError):
        screens.render_screen(lcd_settings, axis, size=size)
