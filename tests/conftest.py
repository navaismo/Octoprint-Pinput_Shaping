"""OctoPrint interfaces for offline tests; no printer connection is opened."""

from copy import deepcopy
import sys
import types

import pytest


try:
    import octoprint.plugin
except ModuleNotFoundError:
    names = ("octoprint", "octoprint.plugin", "octoprint.access", "octoprint.access.permissions",
             "octoprint.server", "octoprint.server.util", "octoprint.server.util.flask")
    for name in names:
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
        if "." in name:
            parent, child = name.rsplit(".", 1)
            setattr(sys.modules[parent], child, module)
    plugin = sys.modules["octoprint.plugin"]
    for name in ("StartupPlugin", "ShutdownPlugin", "EventHandlerPlugin", "SimpleApiPlugin",
                 "SettingsPlugin", "AssetPlugin", "TemplatePlugin", "BlueprintPlugin"):
        setattr(plugin, name, type(name, (), {}))
    plugin.BlueprintPlugin.route = staticmethod(lambda *a, **k: lambda fn: fn)
    permission = types.SimpleNamespace(can=lambda: True)
    sys.modules["octoprint.access.permissions"].Permissions = types.SimpleNamespace(STATUS=permission, CONTROL=permission)
    sys.modules["octoprint.server.util.flask"].restricted_access = lambda fn: fn


C13_REPORT = """echo:  M92 X80.00 Y80.00 Z400.00 E715.12
echo:  M201 X5000.00 Y5000.00 Z5000.00 E5000.00
echo:  M203 X500.00 Y500.00 Z20.00 E30.00
echo:  M204 P3000.00 R3000.00 T3000.00
echo:  M205 B20000.00 S0.00 T0.00 J0.10
echo:  M493 S1 H1
echo:  M493 X C0 A37.00 I0.10 Q0.05
echo:  M493 Y C7 A42.00 I0.10 Q0.05
FR:85%
X:110.00 Y:110.00 Z:10.00 E:0.00 Count X:8800 Y:8800 Z:4000
FTM V2
  Trajectory: TRAPEZOIDAL(0)
"""


@pytest.fixture
def snapshot():
    from octoprint_Pinput_Shaping.firmware import FirmwareSnapshot
    value = FirmwareSnapshot()
    for line in C13_REPORT.splitlines():
        value.feed(line)
    value.validate("X")
    return value


@pytest.fixture
def profile():
    return {"id": "c13", "volume": {"width": 220, "depth": 220, "height": 250,
            "formFactor": "rectangular", "origin": "lowerleft", "custom_box": False}}


@pytest.fixture
def settings():
    return {"accelMin": 300, "accelMax": 2500, "freqStart": 5, "freqEnd": 132,
            "sweepDuration": 20, "dampingRatio": "0.05",
            "sensorType": "adxlspi", "sensorAxis": "sum"}
