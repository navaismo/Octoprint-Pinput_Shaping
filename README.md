# OctoPrint-PInput-Shaping

![Build](https://img.shields.io/badge/status-stable-brightgreen?style=flat-square)
![Python](https://img.shields.io/badge/python-3.7+-blue?style=flat-square)
![OctoPrint](https://img.shields.io/badge/OctoPrint-Compatible-orange?style=flat-square)

**Pinput Shaping** is a responsive plugin for OctoPrint that enables  **Input Shaping** workflows for Marlin firmware users.

It lets you run automated resonance tests, analyze acceleration data with PSD + filtering, and generate ready-to-send `M593` commands—all within the OctoPrint interface.

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
- [Markin Setup](#marlin-setup)
- [Plugin Installation](#plugin-installation)
- [Plugin Configuration](#plugin-configuration)
- [Plugin Usage](#plugin-usage)
  - [Hardware Checks](#hardware-checks-section)
  - [Running Input Shaping Tests](#run-input-shaping-test-section)
  - [Results](#results-section)

---

## Features

- 🧪 **Automated Resonance Testing**
  - Run ADXL345-based tests for X and Y axes.
  - 3×3 XY grid + Z selector for precise probing.

- 📊 **Built-in Data Analysis**
  - Welch-based PSD calculation and signal filtering.
  - Compares 5 shaper types: `ZV`, `MZV`, `EI`, `2HUMP_EI`, `3HUMP_EI`.

- 🎨 **Interactive Graphs**
  - Filtered Signal + PSD overlaid with shaper responses.
  - Full-screen zoom, download, and modal viewer.

- 🔧 **Marlin Command Generator**
  - Automatically suggests `M593` commands (Freq + Damping).
  - Sends G-code directly to the printer with one click.

- 💻 **Modern UI**
  - Dark mode Bootstrap layout
  - Tooltips, hover effects, responsive layout

---

## Shaper Algorithms Supported

| Shaper      | Description            |
|-------------|------------------------|
| ZV          | Zero Vibration         |
| MZV         | Modified ZV            |
| EI          | Extra Insensitive      |
| 2HUMP_EI    | 5-Impulse EI           |
| 3HUMP_EI    | 7-Impulse EI           |

Each is ranked based on:
- PSD area (vibration residuals)
- Max acceleration
- Frequency response

---

## Hardware Setup
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
<a href=""><img src="https://i.imgur.com/bEIpB2E.png align="center"  ></a>
</div>

<br>
6.- To Hold the sensor into the Head and Bed you can use your preffered 3D Models, for my Ender 3 V3 SE I use this models:

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




# Automatic Dependencies installation
SSH into your Raspberry Pi save and run the script located in the bash folder:

```bash
sudo bash Octo-deps-install.sh
```

If you installed from the script skip to plugin configuration section.


# Manual Dependencies Installation:
## Enable SPI support in your Raspberry-Pi

SSH into your Raspberry Pi and run:

```bash
sudo raspi-config
```
> - [X] Select Interface Options and press Enter


<div align="center">
<a href=""><img src="https://i.imgur.com/09sAjHJ.png" align="center"  ></a>
</div>

> - [X] Select SPI and press Enter

<div align="center">
<a href=""><img src="https://i.imgur.com/pvpe6p0.png" align="center"  ></a>
</div>


> - [X] Click on yes.

<div align="center">
<a href=""><img src="https://i.imgur.com/X7MktXA.png" align="center"  ></a>
</div>

Reboot your system
```bash
sudo reboot
```

## Linux System PreRequistes Installation
Before installing the plugin you  **_MUST_** login into your RPi through SSH and run the following:

1.- Update the system.
```sh
sudo apt-get update
```
2.- Install Dependencies,
```bash
sudo apt-get install build-essential make gcc git pigpio python3-pigpio libopenblas-dev
```
3.- Create tmp Dir
```bash
mkdir ADXLTool
cd ADXLTool
```
4.- The Plugin uses the ADXL345SPI Tool created by [nagimov](https://github.com/nagimov), I made a fork to adapt it for the plugin so you need to clone my fork.
```bash
git clone https://github.com/navaismo/adxl345spi.git
```

5.- Install the ADXL utility.
```bash
cd adxl345spi

sudo make

sudo make install
```

6.- Make Octoprint able to run the tool without prompting the password.
```bash
echo -e "octoprint ALL=(ALL) NOPASSWD: $(which adxl345spi) \n$(whoami) ALL=(ALL) NOPASSWD: $(which adxl345spi)" | sudo tee /etc/sudoers.d/octoprint_adxl

```

7.- Veryfy that yo can read values from the Sensor
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
Done
```


# Marlin setup

1.- Your printer must support M117 commands since it are used to control the flow of the Resonance Test.

2.- For the Ender3 V3 SE you can use the [community firmware here](https://github.com/navaismo/Ender-3V3-SE)

3.- If you are using Octoprint along my [E3v3SE Plugin](https://github.com/navaismo/OctoPrint-E3v3seprintjobdetails), please update the plugin to the version >0.2.4 so the M117 commands for the shaping plugin are allowed.


# Plugin Installation

1. Download the `.zip` from [Releases](https://github.com/navaismo/Octoprint-Pinput_Shaping/releases)
2. Go to OctoPrint → Plugin Manager → Install from file

---

## Plugin Configuration

Open Settings → **Pinput Shaping** and configure:

- Bed size (X, Y, Z)
- Acceleration range (min/max)
- Frequency sweep (start/end)
- Damping ratio

![Config](https://i.imgur.com/OTCzcUP.png)

---

## Plugin Usage

### Hardware Checks Section

Use this to validate hardware:

- `Sensor Values` – read ADXL sensor (should be X≈0, Y≈0, Z≈1)
- `Test X / Y Axis` – move axes to verify motion
- `Emergency Stop` – halt printer instantly

![Hardware](https://i.imgur.com/FsXNMop.png)

![SensorValues](https://i.imgur.com/P9mg3Uz.png)

An expected values when the sensor is laying normal are:
    
    X values near 0

    Y values near 0
    
    Z values near 1

If you tilt the sensor to the corresponding AXIS then the corresponding AXIS must show values near 1.

---

### Run Input Shaping Test Section

Select probe point from 3×3 grid and Z height slider:

- Use `Resonance X` if sensor is on Head
- Use `Resonance Y` if sensor is on Bed

![Shaping](https://i.imgur.com/L0X4jNU.png)

---

### Results Section

Displays calculated results:

- Recommended shaper
- `Set Frequency` and `Set Damping` buttons
- Signal and PSD graphs (click to view fullscreen)

![ResultsPNG](https://i.imgur.com/RO1ANKL.png)


- Or explore with Plotly:

![ResultsPlotly](https://i.imgur.com/IOc6M17.png)

---

## License

GPLv3 © [@navaismo](https://github.com/navaismo)

---

## Acknowledgments

- Based on [`adxl345spi`](https://github.com/nagimov/adxl345spi) by [@nagimov](https://github.com/nagimov)
- Inspired by Klipper's input shaping approach
