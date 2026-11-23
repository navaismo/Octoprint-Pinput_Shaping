"""Host-rendered, exclusive 240x320 calibration screens."""

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


BLACK = (0, 0, 0)
WHITE = (240, 244, 240)
GREEN = (80, 216, 104)
CYAN = (64, 192, 216)
AMBER = (248, 192, 64)
GRID = (32, 48, 48)


@lru_cache(maxsize=12)
def _font(size, bold=False):
    from matplotlib import get_data_path
    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    return ImageFont.truetype(str(Path(get_data_path()) / "fonts" / "ttf" / name), size)


def _number(value, suffix=""):
    if isinstance(value, bool):
        return "--"
    try:
        value = float(value)
    except (ValueError, TypeError):
        return "--"
    return f"{value:g}{suffix}" if math.isfinite(value) else "--"


def _test_info(settings):
    native = settings.get("excitationBackend", "software") == "marlin_m495"
    start, end = (settings.get("nativeStart", 20), settings.get("nativeEnd", 60)) if native else (
        settings.get("freqStart", 5), settings.get("freqEnd", 132))
    kind = settings.get("sensorType", "adxlspi")
    sensor = {"lis2dw": "LIS2DW", "adxlusb": "ADXL345 USB", "adxlspi": "ADXL345 SPI"}.get(kind, "Unknown")
    rate = settings.get("lis2dwRate", 800) if kind == "lis2dw" else settings.get("adxlRate", 3200)
    full_scale = settings.get("lis2dwRange", 2) if kind == "lis2dw" else 16
    return f"{_number(start)}-{_number(end)} Hz", sensor, _number(rate, " Hz"), _number(full_scale, " g"), native


def _text(draw, xy, text, size=12, fill=WHITE, bold=False):
    draw.text(xy, str(text), font=_font(size, bold), fill=fill, stroke_width=0)


def _center(draw, y, text, size=12, fill=WHITE, bold=False):
    width = draw.textbbox((0, 0), str(text), font=_font(size, bold))[2]
    _text(draw, ((240 - width) // 2, y), text, size, fill, bold)


@lru_cache(maxsize=1)
def _logo_image():
    """Scale the bundled original artwork, retaining alpha and smooth edges."""
    path = Path(__file__).parent / "static" / "img" / "octoprint-logo.png"
    with Image.open(path) as source:
        logo = source.convert("RGBA")
    logo.thumbnail((64, 52), Image.Resampling.LANCZOS)
    background = Image.new("RGB", logo.size, BLACK)
    background.paste(logo, (0, 0), logo)
    return background


def _logo(image, draw):
    logo = _logo_image()
    image.paste(logo, ((240 - logo.width) // 2, 3))
    draw.line((12, 28, 80, 28), fill=GRID)
    draw.line((159, 28, 227, 28), fill=GRID)


def _excitation(draw, box):
    """Decorative test waveform, never represented as a measured spectrum."""
    x0, y0, x1, y1 = box
    for x in range(x0, x1 + 1, 20):
        draw.line((x, y0, x, y1), fill=GRID)
    middle = (y0 + y1) // 2
    draw.line((x0, middle, x1, middle), fill=GRID)
    points = []
    for x in range(x0, x1 + 1):
        t = (x - x0) / (x1 - x0)
        amplitude = math.exp(-((t - 0.5) / 0.24) ** 2) * (y1-y0) * 0.44
        points.append((x, round(middle - amplitude * math.sin(t * 18 * math.pi))))
    draw.line(points, fill=CYAN, width=1)


def _spectrum(draw, box, result, limits):
    x0, y0, x1, y1 = box
    frequencies, power = result.get("freqs", []), result.get("psd_original", [])
    if len(frequencies) != len(power) or len(power) < 2:
        _text(draw, (x0 + 5, y0 + 12), "No spectrum", 10)
        return False
    try:
        pairs = [(float(f), float(p)) for f, p in zip(frequencies, power)]
    except (TypeError, ValueError):
        return False
    if any(not math.isfinite(f) or not math.isfinite(p) or p < 0 for f, p in pairs):
        return False
    if any(b[0] <= a[0] for a, b in zip(pairs, pairs[1:])):
        return False
    indices = [i for i, (frequency, _) in enumerate(pairs) if limits[0] <= frequency <= limits[1]]
    pairs = [pairs[i] for i in indices]
    if len(pairs) < 2 or max(p for _, p in pairs) <= 0:
        return False
    lo, hi = limits
    top = max(p for _, p in pairs)
    for i in range(5):
        x = round(x0 + (x1 - x0) * i / 4)
        y = round(y0 + (y1 - y0) * i / 4)
        draw.line((x, y0, x, y1), fill=GRID)
        draw.line((x0, y, x1, y), fill=GRID)
    draw.line((x0, y0, x0, y1, x1, y1), fill=WHITE)

    def points(values):
        return [(round(x0 + (f - lo) / (hi - lo) * (x1 - x0)),
                 round(y1 - min(max(float(p) / top, 0), 1) * (y1 - y0 - 2)))
                for (f, _), p in zip(pairs, values)]

    draw.line(points([p for _, p in pairs]), fill=CYAN, width=1)
    candidate = result.get("shapers", {}).get(result.get("best_shaper"), {})
    predicted = candidate.get("psd", [])
    if len(predicted) == len(frequencies) and all(isinstance(p, (int, float)) and math.isfinite(p) and p >= 0 for p in predicted):
        draw.line(points([predicted[i] for i in indices]), fill=AMBER, width=1)
    return True


def render_screen(settings, axis, result=None, size=(240, 320)):
    """Compose the entire LCD; no native print/status elements are required."""
    if axis not in ("X", "Y") or size != (240, 320):
        raise ValueError("Unsupported calibration screen dimensions or axis")
    image = Image.new("RGB", size, BLACK)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"  # Keep text compact; the logo retains its antialiasing.
    band, sensor, rate, full_scale, native = _test_info(settings)
    limits = (float(settings.get("nativeStart", 20)), float(settings.get("nativeEnd", 60))) if native else (
        float(settings.get("freqStart", 5)), float(settings.get("freqEnd", 132)))
    mode = "Native M495" if native else "Software"
    valid = result is not None and result.get("recommendation_valid", True) and result.get("best_shaper")
    resonance = _number(result.get("base_freq"), " Hz") if result is not None else None
    shaper = str(result.get("best_shaper") or "No recommendation") if result is not None else None

    _logo(image, draw)
    _center(draw, 60, "Resonance Result" if result is not None else "Resonance Test", 16, WHITE, True)
    draw.rounded_rectangle((66, 87, 173, 116), radius=6, outline=CYAN)
    _center(draw, 89, f"AXIS {axis}", 18, CYAN, True)
    if result is None:
        draw.rounded_rectangle((10, 124, 229, 194), radius=6, outline=GRID)
        _excitation(draw, (18, 130, 221, 185))
        _center(draw, 200, "Test in progress", 14, AMBER, True)
        draw.rounded_rectangle((10, 225, 229, 310), radius=6, outline=CYAN)
        for y, label, value in ((232, "Range", band), (250, "Sensor", sensor), (268, "Mode", mode)):
            _text(draw, (18, y), label + ":", 11)
            _text(draw, (72, y), value, 11, CYAN)
        _center(draw, 291, f"{rate} / +/-{full_scale}", 10)
    else:
        _text(draw, (26, 124), "Relative response" if native else "PSD", 9)
        draw.rounded_rectangle((10, 121, 229, 224), radius=6, outline=GRID)
        if _spectrum(draw, (26, 142, 220, 204), result, limits):
            try:
                peak = float(result.get("base_freq"))
            except (ValueError, TypeError):
                peak = float("nan")
            if math.isfinite(peak) and limits[0] <= peak <= limits[1]:
                x = round(26 + (peak-limits[0]) / (limits[1]-limits[0]) * 194)
                for y in range(142, 204, 6):
                    draw.line((x, y, x, y+2), fill=GREEN)
                _text(draw, (130, 124), resonance, 9, CYAN)
        _text(draw, (26, 207), _number(limits[0], " Hz"), 9)
        end_label = _number(limits[1], " Hz")
        width = draw.textbbox((0, 0), end_label, font=_font(9))[2]
        _text(draw, (220 - width, 207), end_label, 9)
        _center(draw, 229, "Analysis complete" if valid else "DIAGNOSTICS", 13, GREEN if valid else AMBER, True)
        _text(draw, (16, 253), "Resonance:", 11)
        _text(draw, (114, 249), resonance, 16, CYAN, True)
        _text(draw, (16, 275), "Shaper:", 11)
        recommendation = f"{shaper} @{_number(result.get('best_shaper_freq'), ' Hz')}" if valid else "--"
        _text(draw, (82, 272), recommendation, 13, CYAN, True)
        _center(draw, 308, "NOT APPLIED", 9, AMBER)
    return image
