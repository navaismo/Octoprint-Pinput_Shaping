# OctoPrint-PInput-Shaping

![Build](https://img.shields.io/badge/status-under_review-orange?style=flat-square)
![Python](https://img.shields.io/badge/python-3.9_to_3.12-blue?style=flat-square)
![OctoPrint](https://img.shields.io/badge/OctoPrint-Compatible-orange?style=flat-square)

**Pinput Shaping** is a responsive plugin for OctoPrint that enables  **Input Shaping** workflows for Marlin firmware users.

It runs resonance tests, validates acceleration captures, fits firmware-compatible shapers and verifies their application through OctoPrint.

> The `plugin_optimization` branch contains reliability changes under review. Read the [firmware and installation contract](docs/firmware-contract.md) before physical testing. It targets c13 FTM v2 (`M493`) and retains legacy ZV (`M593`) support. Physical validation is still required.

<br>
<div align="center">
   <img src="https://i.imgur.com/l2Y03AS.png" width="60%" height="60%"/>      
</div>
<br>

---

## 📚 Table of Contents

- [Features](#features)
- [Shaper Algorithms Supported](#shaper-algorithms-supported)
- [Hardware Setup](#hardware-setup)
- [Dependencies Installation](#automatic-dependencies-installation)
- [Marlin Setup](#marlin-setup)
- [Plugin Installation](#plugin-installation)
- [Plugin Configuration](#plugin-configuration)
- [Plugin Usage](#plugin-usage)
  - [Hardware Checks](#hardware-checks)
  - [Running Input Shaping Tests](#running-input-shaping-tests)
  - [Results](#results)

---


## Acknowledgments

- Thanks to [lsellens](https://github.com/lsellens) for the contributions of general clean up, improving and testing the plugin.
- Based on [`adxl345spi`](https://github.com/nagimov/adxl345spi) by [@nagimov](https://github.com/nagimov).
- Thansk to [@thosoo](https://github.com/thosoo/) for ceating the Firmware for the RPI2040 and the wrapper to work with the varianst of the USB ADXL345. 
- Inspired by Klipper's input shaping approach.

---


## Features

- 🧪 **Automated Resonance Testing**
  - Run ADXL345-based tests for X and Y axes.
  - 3×3 XY grid + Z selector for precise probing.

- 📊 **Built-in Data Analysis**
  - Welch-based PSD calculation and signal filtering.
  - Fits eight c13 FTM shapers, including ZV, ZVD, MZV and the EI family.
  - Rejects invalid clocks, weak resonance measurements and distorted sweep timing.

- 🎨 **Interactive Graphs**
  - Filtered Signal + PSD overlaid with shaper responses.
  - Full-screen zoom, download, and modal viewer.

- 🔧 **Marlin Command Generator**
  - Generates axis-specific `M493` commands for c13 FTM v2, or `M593` for legacy ZV.
  - Applies complete results through backend validation and firmware readback.

- 💻 **Modern UI**
  - Theme-compatible Bootstrap controls
  - Explicit calibration stages, progress and recovery status

---

## Shaper Algorithms Supported

| Shaper      | Description            |
|-------------|------------------------|
| ZV          | Zero Vibration         |
| ZVD         | Zero Vibration and Derivative |
| ZVDD        | Zero Vibration, Derivative and Double Derivative |
| ZVDDD       | Zero Vibration and three derivatives |
| MZV         | Modified ZV            |
| EI          | Extra Insensitive      |
| 2HUMP_EI    | 4-Impulse EI           |
| 3HUMP_EI    | 5-Impulse EI           |

Candidates use the same raw PSD reference and are ranked by predicted residual vibration and smoothing displacement. Acceleration limits are theoretical estimates capped by the test limit, not measured mechanical capabilities.

---

## Accelerometer Supported
| Brand       | Model                  |
|-------------|------------------------|
| Creality    | [ADXL345 SPI](https://es.aliexpress.com/item/1005007493583778.html)            |
| BigTreeTech | [BTT-ADXL345](https://es.aliexpress.com/item/1005007518562508.html)            |
| BigTreeTech | [BTT-S2DW](https://es.aliexpress.com/item/1005007518562508.html)      |
| Mellow Fly  | [Fly-ADXL345](https://es.aliexpress.com/item/1005008779305652.html)           |

<br><br>
# Hardware Setup

## ADXL345-SPI
1.- Get any ADXL345 model that you like. In my case I bought it in [AliExpress](https://es.aliexpress.com/item/1005007493583778.html) the one with USB C termination.

2.- The pinout of the sensor is:
<div align="center">
<a href=""><img src="https://i.imgur.com/i8i4WG7.png" align="center"></a>
</div>


USB-C:

    RED: SDO

    BLACK: SDA

    PURPLE: VCC (3.3V)

    YELLOW: GND

    ORANGE: SCL

    BLUE: CS

Source: https://www.reddit.com/r/klippers/comments/1e5ocz5/guide_hooking_up_your_creality_adxl345gsensor_to/?rdt=59734

3.- Build a cable to connect the sensor into the Raspberry Pi. In my case i cut in the half and solder the cables to Dupont jumpers to connect it into the board.

4.- The jumpers tha I used are 40cm long so I needed to attach another jumpers to make the cable longer and used a case to hold the jumpers to avoid disconnections, so at the end I have a cable around 90cm long make sure you make yours long enough to move safely with your printer..

<div align="center">
<a href=""><img src="https://i.imgur.com/iH7OwVs.jpeg" align="center" height="500" width="650" ></a>

<br>

<a href=""><img src="https://i.imgur.com/Rpd2biw.jpeg" align="center" height="500" width="650" ></a>
</div>


5.- Connect the Sensor to the Raspberri Pi following the Diagram below:
<div align="center">
<a href=""><img src="https://i.imgur.com/bEIpB2E.png" align="center"  ></a>
</div>

<br>
6.- To Hold the sensor into the Head and Bed you can use your preffered 3D Models, for my Ender 3 V3 SE I use these models:

* Head Bracket: https://www.printables.com/model/919687-g-sensor-bracket-for-ender3-v3-se/files

* Modified Cable Holder to fit 6-Dupont jumpers based on: https://www.printables.com/model/938904-creality-ender-3-v3-ke-se-g-sensor-cable-holder-v1

* Modified Case for 6-Dupont Jumpers based on: https://www.printables.com/model/904964-connector-housing-dupont-6pin-12mm/files

You can find the models in the STL folder along with the 3mf project:


<div align="center">
<a href=""><img src="https://i.imgur.com/ahHgnOK.png" align="center" height="600" width="950" ></a>
</div>



7.- Place the sensor in the Head.
<div align="center">
<a href=""><img src="https://i.imgur.com/EI6dC3k.jpeg" align="center" height="500" width="650" ></a>
</div>




8.- Place in the Bed.

<div align="center">
<a href=""><img src="https://i.imgur.com/euPkH41.jpeg" align="center" height="500" width="500" ></a>
</div>


<br>

## GY-291 ADXL345 Variant.

The [GY-291](https://es.aliexpress.com/item/1005008142481700.html) has:
```
* A 10kΩ resistor connected between CS and VCC (3.3V).
* That means that the CS pin is always HIGH by default → forced I2C mode.
* Even if in your sketch you lower CS to LOW, the resistor pulls to HIGH, that's why the ADXL345 never switches to SPI mode.

To make work the SPI mode you need to remove the resistor.
```

#### Source: @jusebago user reported the solution in the [Octoprint Forum](https://community.octoprint.org/t/octoprint-pinput-shaping-a-plugin-to-test-input-shaping-with-marlin/63089/10)

<br>

### Wrapper Automatic Dependencies installation
SSH into your Raspberry Pi save and run the script `Octo-deps-install-ADXLSPI.sh` located in the bash folder:

```bash
sudo bash bash/Octo-deps-install-ADXLSPI.sh
```

Veryfy that yo can read values from the Sensor
```bash
sudo adxl345spi -f 5
```
Output:
```bash
Press Q to stop
time = 0.000, x = -0.016, y = 0.039, z = 1.008
time = 0.200, x = 0.000, y = 0.039, z = 0.969
time = 0.400, x = -0.047, y = 0.055, z = 1.000
time = 0.600, x = 0.000, y = 0.039, z = 1.008
time = 0.801, x = -0.070, y = 0.094, z = 1.023
5 samples read in 1.04 seconds with sampling rate 4.8 Hz
```
<br>


## BTT-ADXL345-USB, Mellow Fly-ADXL345USB(and maybe others ADXL based RPi2040)

Thanks to user [@thosoo](https://github.com/thosoo/) for ceating the Firmware for the RPI2040 and the wrapper to work with the varianst of the USB ADXL345 connected to a RPi2040.
You can download [from his repo](https://github.com/thosoo/adxl345usb), or from the folder `FW_RPi2040` the uf2 file and do the following to install into your Device:

  - Hold the `BOOT` button from the board.
  - Connect the Board to your machine while keeping pressing the `BOOT` button.
  - Wait for your computer to show the new Storage Device and release the button.
  - Mount and Open the Device.
  - Copy the Downloaded `Firmware.uf2` file (Or your complied version `Firmware.uf2`) to the Device.
  - When it finish the copy the device will unmount automatically, means the RPi2040 is rebooting.
  - Disconnect the Device from your computer.
  - Connect the device to your Raspberry Pi Running Octoprint(or the device running Octorpint).

### Wrapper Automatic Dependencies installation
SSH into your Raspberry Pi save and run the script `Octo-deps-install-ADXLUSB.sh` located in the bash folder:

```bash
sudo bash bash/Octo-deps-install-ADXLUSB.sh
```

Veryfy that yo can read values from the Sensor
```bash
adxl345usb -f 5 -p /dev/ttyACM1
```
Output:
```bash
Press Q to stop
time = 0.000, x = -0.016, y = 0.039, z = 1.008
time = 0.200, x = 0.000, y = 0.039, z = 0.969
time = 0.400, x = -0.047, y = 0.055, z = 1.000
time = 0.600, x = 0.000, y = 0.039, z = 1.008
time = 0.801, x = -0.070, y = 0.094, z = 1.023
5 samples read in 1.04 seconds with sampling rate 4.8 Hz
```

<br>

## BTT-LIS2DW-USB

To Hold the sensor into the Head and Bed you can use your preffered 3D Models, for my Ender 3 V3 SE I use these models:

* Modified LIS2DW Case: https://www.printables.com/model/1049353-btt-s2dwadxl345-case/files

<div align="center">
<a href=""><img src="https://i.imgur.com/mTbA7Zi.jpeg" align="center" height="500" width="500" ></a>
</div>

<br>

* LIS2DW No Screw Cover: https://www.printables.com/model/1167157-no-screws-cover-for-btt-s2dwadxl345-case/files

<div align="center">
<a href=""><img src="https://i.imgur.com/YPdhww0.jpeg" align="center" height="500" width="500" ></a>
</div>
You can find the models in the STL folder along with the 3mf project:

<br>
Place the sensor in the Head.
<div align="center">
<a href=""><img src="https://i.imgur.com/8meDWeg.jpeg" align="center" height="500" width="500" ></a>
</div>


Place in the Bed.

<div align="center">
<a href=""><img src="https://i.imgur.com/M2C4flF.jpeg" align="center" height="500" width="500" ></a>
</div>

<br>

You can download from my [LIS2DWUSB repo](https://github.com/navaismo/lis2dwusb), or from the folder `FW_RPi2040` the uf2 file and do the following to install into your Device:

  - Hold the `BOOT` button from the board.
  - Connect the Board to your machine while keeping pressing the `BOOT` button.
  - Wait for your computer to show the new Storage Device and release the button.
  - Mount and Open the Device.
  - Copy the Downloaded `Firmware.uf2` file (Or your complied version `Firmware.uf2`) to the Device.
  - When it finish the copy the device will unmount automatically, means the RPi2040 is rebooting.
  - Disconnect the Device from your computer.
  - Connect the device to your Raspberry Pi Running Octoprint(or the device running Octorpint).

### Wrapper Automatic Dependencies installation
SSH into your Raspberry Pi save and run the script `Octo-deps-install-LIS2DW.sh` located in the bash folder:

```bash
sudo bash bash/Octo-deps-install-LIS2DW.sh
```

Veryfy that yo can read values from the Sensor
```bash
sudo lis2dwusb -f 200
```
Output:
```bash
Press Q to stop
time = 0.001, x = -0.029, y = -0.167, z = 1.073
time = 0.004, x = -0.034, y = -0.166, z = 1.064
time = 0.009, x = -0.030, y = -0.165, z = 1.069
...
time = 0.215, x = -0.029, y = -0.174, z = 1.068
time = 0.219, x = -0.031, y = -0.162, z = 1.051
time = 0.224, x = -0.032, y = -0.166, z = 1.025
time = 0.229, x = -0.029, y = -0.167, z = 1.029
Captured 47 samples in 0.23 s = 204.8 Hz
```

<br><br>



# Marlin Setup

The reviewed c13 build uses Fixed-Time Motion v2 with static shapers and a
trapezoidal trajectory. The plugin reads the current configuration and
preserves it around calibration. Legacy ZV builds use a separate M593 adapter.
Do not assume that a multi-hump recommendation can be applied with M593.

See [the full firmware contract](docs/firmware-contract.md) for the reviewed
source revision, shaper IDs, numerical validation, timing constraints and
recovery behavior. The c13 native M495 resonance command is not enabled and
is not used by this branch.

# Plugin Installation

Install into OctoPrint's Python environment:

```bash
python -m pip install -e '.[test]'
```

For a packaged installation, use OctoPrint's Plugin Manager and a source
archive. Restart OctoPrint after backend changes. Copy the entire bash/
directory when using a sensor installer, because the scripts share a helper.
Choose the installer matching your hardware; inspect it before running.
ADXL345 USB and SPI now install as separate executables.

## Plugin Configuration

Open Settings → Pinput Shaping and configure:

- Sensor type, USB serial device and sensor channels. Combined XYZ spectra
  reduce sensitivity to mounting orientation.
- Start and maximum acceleration, frequency range, frequency step and time
  per band. Travel limits always come from the active printer profile.
- Damping ratio. It is copied into the measurement result and does not change
  when settings are edited later.

The initial range is 20–70 Hz. Existing saved settings are retained; an old
132 Hz end frequency can exceed the host sweep's step/acceleration limit and
must be reviewed. The plugin rejects unreachable or unsafe requests rather
than changing limits silently. The pinned LIS2DW wrapper still needs an
acquisition-clock improvement before resonance calibration is supported.

## Plugin Usage

### Hardware Checks

1. Select the correct printer profile and sensor serial port.
   With the printer connected and idle, click **Detect firmware** to check
   input-shaping support. The tab shows **FTM v2 (M493)** or **Legacy ZV (M593)**.
   Detection runs only on request; startup, connection and returning to idle
   do not send probes or schedule retries. Repeat detection after reconnecting
   or an interrupted check. Unknown or incompatible firmware keeps calibration
   and result application disabled.
2. Click **Prepare coordinate modes** to explicitly select millimeters,
   absolute positioning, absolute extrusion and the preparation feed rate.
   This action does not command axis displacement.
3. Check **Sensor values**. USB sensor paths must differ from the printer port.
4. Use **Test X axis** or **Test Y axis** for a bounded, low-speed motion check.

**Cancel test** stops the calibration sequence and restores settings after
queued motion drains. **Stop printer (M112)** requests shutdown without a
confirmation dialog. It is not a substitute for a physical stop device.

### Running Input Shaping Tests

Select a grid point and a Z height of at least 10 mm. Secure the sensor to the
moving component for the tested axis, clear the travel area and select
**Calibrate X** or **Calibrate Y**. The plugin checks the sensor before homing,
disables existing shaping for measurement and records motion completion times.
Only one calibration operation can run at a time.

The test restores the original acceleration-sweep approach: center/high/center/low
motion with 5–1 mm excursions and increasing acceleration. The analysis range
selects vibration frequencies in the measured spectrum, independently of the
motor reversal rate. New installations default to 5–132 Hz and a 20-second target
excitation. Whole cycles can extend that duration. Existing saved settings are
preserved; change the analysis end frequency if it was previously reduced.

The former tiny-segment frequency sweep and its step/acceleration frequency cap
have been removed. Completion barriers and a minimum travel-time check remain;
the controller does not claim a precise excitation frequency from host timing.
Signal-quality checks still reject missing samples, clipping, flat captures and
unclear peaks. Measured frequency coverage and repeatability need hardware checks.

Diagnostics are written to `Pinput_Shaping.log` in OctoPrint's configured log
folder (normally `~/.octoprint/logs/`). Per-run timing and sensor output remain
under the plugin data folder in `runs/<session>/excitation.json`,
`capture.csv.log` and `sensor_check.csv.log`.

### Results

A complete result contains the measured resonance, selected shaper and its
frequency, damping, predicted spectra, quality information and raw CSV.
PNG plots remain available offline; interactive Plotly charts are optional.

With FTM, select any fitted candidate under **Shaper to apply**. The recommendation
is selected initially and remains marked in the comparison table; **Use
recommendation** restores that selection. The command preview follows your
choice. **Apply and verify selected shaper** sends its saved frequency, damping
and applicable tolerance, then reads the setting back. Legacy firmware offers
only **Apply and verify recommended ZV**; advanced shapers cannot be sent.
The browser submits the session identifier and shaper name; the backend builds
the command from saved measurement data. Results are invalidated by a new test,
a reconnect or a change to the saved firmware configuration. EEPROM is not written. Verify
repeatability and print quality before saving settings manually.

If calibration is interrupted and cannot restore its settings, reset or
reconnect the same printer and use **Restore saved settings**. Further tests
remain blocked until recovery succeeds.

## Development and Validation

```bash
python -m pytest -q
node tests/test_frontend.js
PINPUT_FIRMWARE_ROOT=/path/to/Marlin_c13_octoprint python -m pytest -q tests/test_c13_contract.py
```

Tests use synthetic captures, a simulated serial transport and stored output
from the actual c13 C++ shaper functions. The optional live contract requires a
C++ compiler and the firmware checkout. With OctoPrint installed, additional
tests check real plugin registration, download routing and command tags without
opening a device. CI includes a dedicated OctoPrint 1.11.8 integration job.
These checks do not validate physical motion or replace a supervised hardware
test.

## License

GPLv3 © [@navaismo](https://github.com/navaismo)

