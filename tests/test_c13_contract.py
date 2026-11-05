"""Compile the actual c13 shaping functions in a host-only C++ harness."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from octoprint_Pinput_Shaping.firmware import SHAPER_IDS
from octoprint_Pinput_Shaping.inputshaping_analyzer import InputShapingAnalyzer


HEADER = r"""
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <iomanip>
#define ENABLED(x) 1
#define ANY(...) 1
#define FT_MOTION 1
#define HAS_FTM_SHAPING 1
#define OPTARG(c,...) , __VA_ARGS__
#define TERN_(c,...) __VA_ARGS__
#define _MAX std::max
#define POW std::pow
#define SQRT std::sqrt
#define LROUND std::lround
#define FTM_FS 1000
template<typename T> T sq(T x) { return x*x; }
enum ftMotionShaper_t {ftMotionShaper_NONE, ftMotionShaper_ZV,
    ftMotionShaper_ZVD, ftMotionShaper_ZVDD, ftMotionShaper_ZVDDD,
    ftMotionShaper_EI, ftMotionShaper_2HEI, ftMotionShaper_3HEI, ftMotionShaper_MZV};
struct AxisShaping {
    uint32_t max_i = 0;
    float Ai[8] = {};
    int32_t Ni[8] = {};
    void set_axis_shaping_A(ftMotionShaper_t, float, float);
    void set_axis_shaping_N(ftMotionShaper_t, float, float);
};
"""

MAIN = r"""
int main() {
  std::cout << std::setprecision(10);
  for (int sh=1; sh<=8; ++sh)
    for (float f : {37.0f, 50.0f, 83.5f})
      for (float z : {0.05f, 0.1f, 0.15f})
        for (float q : {0.05f, 0.1f}) {
          AxisShaping a;
          a.set_axis_shaping_A(ftMotionShaper_t(sh), z, q);
          a.set_axis_shaping_N(ftMotionShaper_t(sh), f, z);
          for (unsigned i=0; i<=a.max_i; ++i)
            std::cout << sh << ' ' << f << ' ' << z << ' ' << q << ' '
                      << i << ' ' << a.Ai[i] << ' ' << a.Ni[i]-a.Ni[0] << '\n';
        }
}
"""


def compiled_cases(root, tmp_path):
    path = Path(root) / "Marlin/src/module/ft_motion/shaping.cpp"
    original = path.read_text()
    source = re.sub(r'^#include[^\n]*\n', '', original, flags=re.M)
    cpp, binary = tmp_path / "shapers.cpp", tmp_path / "shapers"
    cpp.write_text(HEADER + source + MAIN)
    subprocess.run(["c++", "-std=c++17", "-O2", str(cpp), "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    output = subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
    cases = {}
    names = {value: name for name, value in SHAPER_IDS.items()}
    for line in output.splitlines():
        identifier, freq, damping, tolerance, index, gain, delay = map(float, line.split())
        key = (names[int(identifier)], round(freq, 3), round(damping, 3), round(tolerance, 3))
        cases.setdefault(key, []).append([delay / 1000, gain])
    return {"source_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "ftm_fs": 1000,
            "cases": [{"name": k[0], "frequency": k[1], "damping": k[2], "tolerance": k[3],
                       "impulses": value} for k, value in cases.items()]}


def compare_cases(data, tmp_path):
    for case in data["cases"]:
        analyzer = InputShapingAnalyzer(tmp_path, tmp_path / "unused.csv",
            damping=case["damping"], tolerance=case["tolerance"], shaping_rate=data["ftm_fs"])
        actual = analyzer.generate_shapers(case["frequency"])[case["name"]]
        assert len(actual) == len(case["impulses"])
        for (time, amplitude), (wanted_time, wanted_amplitude) in zip(actual, case["impulses"]):
            assert time == pytest.approx(wanted_time, abs=1e-9)
            assert amplitude == pytest.approx(wanted_amplitude, abs=2e-6)


def test_stored_c13_contract(tmp_path):
    data = json.loads((Path(__file__).parent / "fixtures/c13_shapers.json").read_text())
    compare_cases(data, tmp_path)


def test_live_c13_source_contract(tmp_path):
    root = os.environ.get("PINPUT_FIRMWARE_ROOT")
    if not root or not shutil.which("c++"):
        pytest.skip("Set PINPUT_FIRMWARE_ROOT and install a C++ compiler for the live firmware contract")
    data = compiled_cases(root, tmp_path)
    compare_cases(data, tmp_path)
    stored = json.loads((Path(__file__).parent / "fixtures/c13_shapers.json").read_text())
    assert data["source_sha256"] == stored["source_sha256"], "Firmware shaping.cpp changed; review and regenerate the contract"


def test_live_c13_configuration_and_protocol():
    root = os.environ.get("PINPUT_FIRMWARE_ROOT")
    if not root:
        pytest.skip("Set PINPUT_FIRMWARE_ROOT to compare the reviewed c13 configuration")
    root = Path(root)
    advanced = (root / "Marlin/Configuration_adv.h").read_text()
    configuration = (root / "Marlin/Configuration.h").read_text()
    for name in ("FT_MOTION", "FTM_IS_DEFAULT_MOTION", "EMERGENCY_PARSER", "FTM_SHAPER_ZV",
                 "FTM_SHAPER_ZVD", "FTM_SHAPER_ZVDD", "FTM_SHAPER_ZVDDD", "FTM_SHAPER_EI",
                 "FTM_SHAPER_2HEI", "FTM_SHAPER_3HEI", "FTM_SHAPER_MZV"):
        assert re.search(r"^\s*#define\s+" + name + r"\b", advanced, re.M), name
    assert re.search(r"^\s*#define\s+FTM_FS\s+1000\b", advanced, re.M)
    assert re.search(r"^\s*#define\s+FTM_MIN_SHAPE_FREQ\s+20\b", advanced, re.M)
    # Native resonance is optional and independent of the shaping contract.
    # Updated c13 builds may enable it without changing shaper coefficients.
    assert re.search(r"^#define\s+BAUDRATE\s+115200\b", configuration, re.M)
    header = (root / "Marlin/src/module/ft_motion/shaping.h").read_text()
    aliases = {"2HUMP_EI": "2HEI", "3HUMP_EI": "3HEI"}
    for name, identifier in SHAPER_IDS.items():
        assert re.search(r"ftMotionShaper_" + aliases.get(name, name) + r"\s*=\s*" + str(identifier) + r"\b", header)
    protocol = (root / "Marlin/src/gcode/feature/ft_motion/M493.cpp").read_text()
    for field in ("C", "A", "I", "Q"):
        assert f"parser.seenval('{field}')" in protocol
