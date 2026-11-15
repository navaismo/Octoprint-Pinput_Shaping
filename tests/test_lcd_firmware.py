"""Replay full host-generated screens through the actual Marlin executor."""
import os
from pathlib import Path
import shutil
import struct
import subprocess
import zlib

from PIL import Image
import pytest

from octoprint_Pinput_Shaping.lcd import batches, packet
from octoprint_Pinput_Shaping.lcd_screens import render_screen


@pytest.mark.parametrize("result", [None, {
    "base_freq": 43.2, "best_shaper": "MZV", "best_shaper_freq": 47.5,
    "recommendation_valid": True, "freqs": [20, 30, 40, 50, 60],
    "psd_original": [1, 3, 10, 2, 1], "shapers": {"MZV": {"psd": [.2, .5, 1, .1, .1]}}
}])
def test_fullscreen_host_pixels_match_production_firmware(tmp_path, result):
    root = Path(os.environ.get("PINPUT_FIRMWARE_ROOT", "/nonexistent"))
    source = root / "Marlin/src/e3v3se/LcdDraw.cpp"
    compiler = shutil.which("g++")
    if not source.is_file() or not compiler:
        pytest.skip("Live full-screen Marlin checkout and C++ compiler required")
    if 'CAPS FULLSCREEN' not in source.read_text():
        pytest.skip("Live Marlin checkout lacks the full-screen extension")
    binary = tmp_path / "lcd"
    subprocess.run([compiler, "-std=c++17", "-O1", "-g", "-DLCD_DRAW_NATIVE_TEST",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    str(root / "test/c13_cleanup/harness.cpp"), str(source), "-o", str(binary)], check=True)
    settings = {"excitationBackend": "marlin_m495", "nativeStart": 20, "nativeEnd": 60,
                "sensorType": "lis2dw", "lis2dwRate": 800, "lis2dwRange": 16}
    image = render_screen(settings, "X", result)
    script = [packet("B", 123, b"\1")[6:]]
    checksum, sequence = 0, 0
    for data in batches(image):
        script += [packet("D", 123, struct.pack(">H", sequence)+data)[6:], "T 60"]
        checksum = zlib.crc32(data, checksum) & 0xFFFFFFFF
        sequence += 1
    script += [packet("E", 123, struct.pack(">HI", sequence, checksum))[6:], "T 150"]
    script = ["C "+line if not line.startswith("T ") else line for line in script]
    output = subprocess.check_output([str(binary)], input="\n".join(script)+"\n", text=True,
                                     env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                                          "UBSAN_OPTIONS": "halt_on_error=1"})
    assert f"DONE 0000007b {sequence:04x}" in output
    assert "ABORTED" not in output and "BOUNDS" not in output
    pixels, color = [0xFFFF]*(240*320), 0
    for line in output.splitlines():
        if not line.startswith("F "):
            continue
        frame = bytes.fromhex(line.split()[1])
        if frame[1] == 0x40:
            color = int.from_bytes(frame[2:4], "big")
        elif frame[1] == 0x5B:
            x0, y0, x1, y1 = struct.unpack(">HHHH", frame[2:10])
            assert 0 <= x0 <= x1 < 240 and 0 <= y0 <= y1 < 320
            for y in range(y0, y1+1):
                pixels[y*240+x0:y*240+x1+1] = [color]*(x1-x0+1)
    raw = image.convert("RGB").tobytes()
    expected = [((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                for r, g, b in zip(raw[::3], raw[1::3], raw[2::3])]
    assert pixels == expected
