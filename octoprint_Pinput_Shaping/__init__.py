# Coding=utf 8
from __future__ import absolute_import
from PIL import Image
from scipy.fft import fft
from .inputshaping_analyzer import InputShapingAnalyzer
from octoprint.server.util.flask import restricted_access, make_response
from octoprint.logging.handlers import CleaningTimedRotatingFileHandler
import io
import os
import re
import json
import time
import inspect
import logging
import threading
import subprocess
import pexpect
import numpy as np
import flask
import math
import csv
import octoprint.plugin
import octoprint.filemanager
import octoprint.filemanager.util



class PinputShapingPlugin(octoprint.plugin.StartupPlugin,
                          octoprint.filemanager.util.LineProcessorStream,
                          octoprint.plugin.EventHandlerPlugin,
                          octoprint.plugin.ProgressPlugin,
                          octoprint.plugin.SimpleApiPlugin,
                          octoprint.plugin.SettingsPlugin,
                          octoprint.plugin.AssetPlugin,
                          octoprint.plugin.TemplatePlugin
                          ):

    def __init__(self):  # init global vars
        self.plugin_data_folder = None
        self.metadata_dir = None
        self.X_AXIS = "X"         # Axis to test (X or Y)
        self.START_POS = 100      # Center position of oscillations (mm)
        self.AMPLITUDE = 1        # Oscillation amplitude (mm)
        self.FREQ_START = 5       # Start frequency (Hz)
        self.FREQ_END = 132       # End frequency (Hz)
        self.DURATION = 20        # Sweep duration (seconds)
        self.ACCELERATION = 2500  # mm/s² (set via M204)
        self.csv_filename = None
        self.adxl_process = None
        self.adxl_capture_active = False
        self.last_command_sent = ""
        self._adchild = None
        self._adchild_logfile = None
        self._adchild_logfilename = None
        self.currentAxis = None
        self.shapers = None
        self.getM593 = False

        self._plugin_logger = logging.getLogger(
            "octoprint.plugins.Pinput_Shaping")
        
        
   
    def configure_logger(self):
        # Get the base path for logs from the settings
        log_base_path = os.path.expanduser("~/.octoprint/logs")

        # Create the directory if it doesn't exist
        if not os.path.exists(log_base_path):
            os.makedirs(log_base_path, exist_ok=True)
            os.chmod(log_base_path, 0o775)

        log_file_path = os.path.join(log_base_path, "Pinput_Shaping.log")
        handler = CleaningTimedRotatingFileHandler(
            log_file_path, when="D", backupCount=3)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s"))
        self._plugin_logger.addHandler(handler)
        self._plugin_logger.setLevel(logging.INFO)
        self._plugin_logger.propagate = False

    def get_current_function_name(self):
        return inspect.getframeinfo(inspect.currentframe().f_back).function
    
    
               
    def get_settings_defaults(self):
        printer_profile = self._printer_profile_manager.get_current_or_default()
        width = printer_profile['volume']['width']
        depth = printer_profile['volume']['depth']
        height = printer_profile['volume']['height']
        return {
            "sizeX": width,
            "sizeY": depth,
            "sizeZ": height,
            "accelMin": 300,
            "accelMax": 2500,
            "freqStart": 5,
            "freqEnd": 132,
            "dampingRatio": "0.05",
            "sensorType": "adxlspi"  
        }    


    def get_template_configs(self):
        return [
            dict(type="settings", template="settings_pinput_shaping_settings.jinja2", name="Pinput Shaping", custom_bindings=True),
            dict(type="tab", template="pinput_shaping_tab.jinja2", name="Input Shaping", custom_bindings=True)
        ]

    def get_assets(self):
        return {
            "js": ["js/pinput_shaping.js"],  # JS file
        }
        
    def get_assets_folder(self):
        return os.path.join(os.path.dirname(os.path.realpath(__file__)), "static")
    
        
   
    

    def on_after_startup(self):
        self.configure_logger()
        self._plugin_logger.info(">>>>>> PInput-Shaping Loaded <<<<<<")
        self._plugin_logger.info(f"Plugin identifier is: {self._identifier}")
        self._plugin_logger.info(f"Plugin version is: {self._plugin_version}")
        self._plugin_logger.info(f"X size: {self._settings.get(['sizeX'])}")
        self._plugin_logger.info(f"Y size: {self._settings.get(['sizeY'])}")
        self._plugin_logger.info(f"Z size: {self._settings.get(['sizeZ'])}")
        self._plugin_logger.info(f"Acceleration min: {self._settings.get(['accelMin'])}")
        self._plugin_logger.info(f"Acceleration max: {self._settings.get(['accelMax'])}")
        self._plugin_logger.info(f"Frequency start: {self._settings.get(['freqStart'])}")
        self._plugin_logger.info(f"Frequency end: {self._settings.get(['freqEnd'])}")
        self._plugin_logger.info(f"Damping ratio: {self._settings.get(['dampingRatio'])}")
        self._plugin_logger.info(f"Sensor type: {self._settings.get(['sensorType'])}")
        
        self._plugin_manager.send_plugin_message(self._identifier, {"msg": "Pinput Shaping Plugin loaded"})

        # Get the plugin's data folder (OctoPrint manages this)
        data_folder = self.get_plugin_data_folder()

        # Define the metadata subdirectory
        self.metadata_dir = os.path.join(data_folder, "metadata")
        self.graphs_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)),"static","metadata")


        # Create the directory if it doesn't exist
        os.makedirs(self.metadata_dir, exist_ok=True)
        os.chmod(self.metadata_dir, 0o775)
        os.makedirs(self.graphs_dir, exist_ok=True)
        os.chmod(self.graphs_dir, 0o775)

        self._plugin_logger.info(
            f">>>>>> PInput-Shaping Metadata directory initialized: {self.metadata_dir}")
        self._plugin_logger.info(
            f">>>>>> PInput-Shaping Graphs directory initialized: {self.graphs_dir}")

    # API Commands

    def get_api_commands(self):
        return dict(run_axis_test=[],
                    run_adxl_test=[],
                    run_resonance_test=[],)

    def on_api_command(self, command, data):
        self._plugin_logger.info(
            f">>>>>> PInput-Shaping API Command: {command} with data: {data}")
        if command == "run_adxl_test":
            self._plugin_manager.send_plugin_message(self._identifier, {"type":"popup", "message":"Running Test for ADXL345 Sensor..."})
            result = self._run_adxl_test()
            return flask.jsonify(result)

        elif command == "run_axis_test":
            axis = data["data"]["axis"]
            self._plugin_logger.info(f"Triggering axis {axis} test")
            self._plugin_manager.send_plugin_message(self._identifier, {"type":"popup", "message":f"Running Test for {axis} Axis..."})
            result = self._run_axis_test(axis)
            return flask.jsonify(result)
        elif command == "run_resonance_test":
            axis = data["data"]["axis"]
            x = data["data"]["start_x"]
            y = data["data"]["start_y"]
            z = data["data"]["start_z"]
            self._plugin_logger.info(
                f"Triggering resonance test for axis {axis}")
            self._plugin_manager.send_plugin_message(self._identifier, {"type":"popup", "message":f"Running Resonance Test for {axis} Axis..."})
            result = self._run_resonance_test(axis, x, y, z)
            return flask.jsonify(result)

        else:
            self._plugin_logger.warning(f"Unknown API command: {command}")
            return flask.jsonify({"success": False, "error": "Unknown command"})



    def _run_adxl_test(self):
        self._plugin_logger.info(">>>>>>> Running ADXL345 test with settings:")
        self._plugin_logger.info(f"X size: {self._settings.get(['sizeX'])}")
        self._plugin_logger.info(f"Y size: {self._settings.get(['sizeY'])}")
        self._plugin_logger.info(f"Z size: {self._settings.get(['sizeZ'])}")
        self._plugin_logger.info(f"Acceleration min: {self._settings.get(['accelMin'])}")
        self._plugin_logger.info(f"Acceleration max: {self._settings.get(['accelMax'])}")
        self._plugin_logger.info(f"Frequency start: {self._settings.get(['freqStart'])}")
        self._plugin_logger.info(f"Frequency end: {self._settings.get(['freqEnd'])}")
        self._plugin_logger.info(f"Damping ratio: {self._settings.get(['dampingRatio'])}")
        self._plugin_logger.info(f"Sensor type: {self._settings.get(['sensorType'])}")
        
        try:
            self._plugin_logger.info("Backing up current shaper values...")
            self._printer.commands("M593")
            time.sleep(2)
            self.csv_filename = os.path.join(self.metadata_dir, "adxl_test_capture.csv")
            log_filename = os.path.join(self.metadata_dir, "adxl_output.log")

            self._start_adxl_capture(5)

            time.sleep(2)

            self._stop_adxl_capture()

            if not os.path.exists(self.csv_filename):
                self._plugin_logger.error("CSV data file not found")
                self._plugin_logger.error(f"ADXL345 test failed. No data captured.")
                my_err="CSV data file not found"
                self._plugin_manager.send_plugin_message(self._identifier, {"type":"error_popup", "message":my_err})
                return {"success": False, "error": my_err}

            samples = []
            with open(self.csv_filename, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    samples.append({
                        "time": row["time"],
                        "x": row["x"],
                        "y": row["y"],
                        "z": row["z"]
                    })

            summary_line = "No summary available"
            if os.path.exists(log_filename):
                with open(log_filename, "r") as logf:
                    lines = logf.read().strip().splitlines()
                    for line in reversed(lines):
                        if "samples" in line and "Hz" in line:
                            summary_line = line
                            break
                self._plugin_logger.warning("Output log not found")

            self._plugin_logger.info(f"ADXL345 test completed. Summary: {summary_line}")
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
            self.restore_shapers()
            self._plugin_logger.info("Restored shaper values to printer.")
            return {
                "success": True,
                "summary": summary_line,
                "samples": samples,
                "stdout_preview": "\n".join([f"{s['time']} {s['x']} {s['y']} {s['z']}" for s in samples[-5:]])  # last few samples
            }

        except Exception as e:
            self._plugin_logger.error(f"ADXL345 test failed: {e}")
            self._plugin_manager.send_plugin_message(self._identifier, {"type":"error_popup", "message":str(e)})
            return {
                "success": False,
                "error": str(e)
            }

    # Run the test head movements
    def _run_axis_test(self, axis):
        self._plugin_logger.info(f">>>>>> Running Sweeping {axis} test")
        #create variable with the value of datetime in iso format 
        dt= time.strftime("%Y%m%dT%H%M%S")        
        self.csv_filename = os.path.join(self.metadata_dir, f"Raw_accel_values_AXIS_{axis}_{dt}.csv")
        
           
        printer_status = self._printer.get_state_id()

        if printer_status == "OPERATIONAL":
            self._plugin_logger.info(
                "Printer is idle. Proceeding with Axis test.")
            self._plugin_logger.info(
                "Sending precomputed commands to printer...")
            x = float(self._settings.get(["sizeX"])) / 2
            y = float(self._settings.get(["sizeY"])) / 2
            z = 10  # Default Z height for parking
            self.home_and_park(x, y, z)
            self._printer.commands(self.test_sweep(axis))
            return {
                "success": True,
                "summary": f"Test for {axis} triggered successfully."
            }
        else:
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
            time.sleep(1)
            message = f"Printer is not in an idle state. Current state: {printer_status}. Cannot run test on AXIS {axis}."
            self._plugin_manager.send_plugin_message(self._identifier, {"type":"error_popup", "message":message})
            self._plugin_logger.warning(message)
            return {
                "success": False,
                "error": message
            }

    # Run the resonance test
    def _run_resonance_test(self, axis, x, y, z):
        self._plugin_logger.info(f"Running resonance test for {axis} axis at position ({x}, {y}, {z})")
         #create variable with the value of datetime in iso format 
        dt= time.strftime("%Y%m%dT%H%M%S")        
        self.csv_filename = os.path.join(self.metadata_dir, f"Raw_accel_values_AXIS_{axis}_{dt}.csv")
      
        printer_status = self._printer.get_state_id()

        if printer_status == "OPERATIONAL":
            self._plugin_logger.info("Printer is idle. Proceeding with resonance test.")
            self.adxl_capture_active = True
            self._plugin_logger.info("Backing up current shaper values...")
            self._printer.commands("M593")
            time.sleep(2)
            self._plugin_logger.info("Sending resonance test commands to printer...")
            self.home_and_park(x, y, z)
            self._printer.commands(self.precompute_sweep(axis, x, y))
            return {
                "success": True,
                "summary": f"Resonance test for {axis} triggered successfully."
            }
        else:
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
            time.sleep(1)
            message = f"Printer is not in an idle state. Current state: {printer_status}. Cannot run resonance test."
            self._plugin_manager.send_plugin_message(self._identifier, {"type":"error_popup", "message":message})
            self._plugin_logger.warning(message)
            return {
                "success": False,
                "error": message
            }

    # Precompute the sweep commands
    def test_sweep(self, axis):
        
        self.currentAxis = axis
        self._plugin_logger.info(f"Precomputing sweep commands for Axis {axis}...")
        t = np.linspace(0, self.DURATION, num=2000)  # 2000 points over 20 seconds
        freqs = self.FREQ_START + (self.FREQ_END -self.FREQ_START) *t /self.DURATION
        positions = self.START_POS + self.AMPLITUDE *np.sin(2 *np.pi *freqs *t)
        commands = []
        commands.append(f"M117 Testing Sweep on {axis}-Axis")
        for pos in positions:
            commands.append("G0 {}{} F{}".format(axis, pos, 60 *self.ACCELERATION))
        
        commands.append(f"M117 Finish Test Sweep on {axis}-Axis")

        return commands

    # Precompute the resonance test commands
    def precompute_sweep(self, axis, x, y):
        num_cycles = 800
        steps_per_cycle = 4

        amplitude = 5
        min_amp = 1
        self.currentAxis = axis

        accel_min = int(self._settings.get(["accelMin"]))
        accel_max = int(self._settings.get(["accelMax"]))
        freq_start = float(self._settings.get(["freqStart"]))
        freq_end = float(self._settings.get(["freqEnd"]))

        freqs = np.linspace(freq_start, freq_end, num_cycles)
        amplitudes = np.linspace(amplitude, min_amp, num_cycles)
        accelerations = np.linspace(accel_min, accel_max, num_cycles)
        feedrates = np.clip(100 * accelerations, 2000, 15000)

        commands = []
        commands.append("M117 Starting resonance test")
        commands.append("M117 ADXL|ON")
        commands.append("M593 F0")
        commands.append(f"M117 Resonance Test on {axis}-Axis")

        current_accel = int(accelerations[0])
        commands.append(f"M204 S{current_accel}")

        for i in range(num_cycles):
            freq = freqs[i]
            amp = amplitudes[i]
            accel = int(accelerations[i])
            feed = int(feedrates[i])

            #if int(freq) % 10 == 0:
            #    commands.append(f"M117 {axis} {int(freq)}Hz /{accel}mm/s²")

            if abs(accel - current_accel) > 100:
                commands.append(f"M204 S{accel}")
                current_accel = accel

            for j in range(steps_per_cycle):
                phase = 2 * np.pi * j / steps_per_cycle
                offset = amp * np.sin(phase)

                if axis == "X":
                    commands.append(f"G0 X{x + offset:.3f} Y{y:.3f} F{feed}")
                elif axis == "Y":
                    commands.append(f"G0 X{x:.3f} Y{y + offset:.3f} F{feed}")

        commands.append(f"M117 Resonance Test complete")
        commands.append("M204 P1500 R500 T1500") # restoring original accel
        commands.append("M400")  # Wait for all moves to complete

        return commands

    def home_and_park(self, x, y, z):
        self._plugin_logger.info("Homing and parking printer...")
        start_pos = f"X{x} Y{y} Z{z}"
        self._printer.commands("G28")
        self._printer.commands(f"G0 {start_pos} F1500")
        self._printer.commands("G4 P1000")
        return
        

    # def gcode_sending_handler(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
    #     #self._plugin_logger.info(f"Intercepted G-code: {cmd}")
        
    #     if cmd.startswith("O9000"):
    #         if cmd == "O9000 ADXL|ON":
    #             self._plugin_logger.info("Detected command to Start ADXL345 capture")
    #             threading.Thread(target=self._start_adxl_capture(3200)).start()
    #             self.adxl_capture_active = True
    #             return [cmd]
            
                            
    #     # Return the cmd
    #     return [cmd]
    
    
    def gcode_received_handler(self, comm, line, *args, **kwargs):
        
        
        
        if "Input Shaping:" in line:
            self._plugin_logger.info("Detected Input Shaping message")
            self.getM593 = True
            self.shapers = {}
            
        # Extract the shaper values from the line
        match_x = re.match(r".*M593 X F([\d.]+) D([\d.]+)", line)
        match_y = re.match(r".*M593 Y F([\d.]+) D([\d.]+)", line)

        if match_x and self.getM593:
            self._plugin_logger.info("Detected M593 X value")
            self.shapers["X"] = {
                "F": float(match_x.group(1)),
                "D": float(match_x.group(2))
            }
        if match_y and self.getM593:
            self._plugin_logger.info("Detected M593 Y value")
            self.shapers["Y"] = {
                "F": float(match_y.group(1)),
                "D": float(match_y.group(2))
            }
            # Save to file
            shaper_bck_path = os.path.join(self.metadata_dir, "current_shaper_values.json")
            with open(shaper_bck_path, "w") as f:
                json.dump(self.shapers, f)
            self._plugin_logger.info(f"Shaper backup saved: {self.shapers}")
            self.getM593 = False
                    
        
        elif "Resonance Test complete" in line:
            self._plugin_logger.info("Detected M117 Resonance Test complete message")
            self._plugin_logger.info(f"Resonance Test complete for {self.currentAxis} axis")
            self._plugin_logger.info("Stopping ADXL345 capture...")
            threading.Thread(target=self._stop_adxl_capture).start()
            self._plugin_logger.info(f"Starting Input Shaping analysis...")
            self._plugin_manager.send_plugin_message(self._identifier, {"type":"popup", "message":"Starting Input Shaping analysis..."})
            self.adxl_capture_active = False
            time.sleep(3)
            self.get_input_shaping_results()
            
        
        elif "Finish Test Sweep" in line:
            self._plugin_logger.info(f"Finished Test Sweep for {self.currentAxis} axis") 
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
            
        elif "ADXL|ON" in line:
            self._plugin_logger.info("Detected command to Start ADXL345 capture")
            self._plugin_logger.info("ADXL345 capture started...")
            self.adxl_capture_active = True
            threading.Thread(target=self._start_adxl_capture(3200)).start()       

            
        return line    
    
    
    
    def restore_shapers(self):
        backup_path = os.path.join(self.metadata_dir, "current_shaper_values.json")
        if not os.path.exists(backup_path):
            self._plugin_logger.warning("No saved shaper settings found to restore.")
            return

        with open(backup_path, "r") as f:
            shapers = json.load(f)

        for axis, settings in shapers.items():
            freq = settings.get("F")
            damp = settings.get("D")
            if freq is not None and damp is not None:
                cmd = f"M593 {axis} F{freq:.2f} D{damp} "
                self._printer.commands(cmd)
                self._plugin_logger.info(f"Restored: {cmd}")
        
    
    def get_input_shaping_results(self):
        self._plugin_logger.info(f"Getting Input Shaping results for {self.currentAxis} Axis...")
        
        if self.adxl_capture_active:
            self._plugin_logger.warning("ADXL345 capture is still active. Stopping it first.")
            self._stop_adxl_capture()

        if not os.path.exists(self.csv_filename):
            self._plugin_logger.error("CSV data file not found")
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
            time.sleep(1)
            self._plugin_manager.send_plugin_message(self._identifier, {"type":"error_popup", "message":"CSV data file not found"})
            return {"success": False, "error": "CSV data file not found"}

        analyzer = InputShapingAnalyzer(self.graphs_dir, self.csv_filename, float(self._settings.get(["dampingRatio"])), 100, self.currentAxis, logger=self._plugin_logger)
        best_shaper = analyzer.analyze()
        signal_path, psd_path, shaper_results, best_shaper, base_freq = analyzer.generate_graphs()
        command = analyzer.get_recommendation()
        data_for_plotly = analyzer.get_plotly_data()
     
        self._plugin_logger.info(f"Best shaper for {self.currentAxis} axis: {best_shaper}")
        self._plugin_logger.info(f"Signal graph saved to: {signal_path}")
        self._plugin_logger.info(f"PSD graph saved to: {psd_path}")
        self._plugin_logger.info(f"Recommended command: {command}")
        self._plugin_logger.info(f"Input Shaping analysis completed.")
        self._plugin_manager.send_plugin_message(self._identifier, dict(type="close_popup"))
        self._printer.commands(f"M117 Freq for {self.currentAxis}:{base_freq:.2f} Damp:{self._settings.get(['dampingRatio'])}")
        self._plugin_manager.send_plugin_message(self._identifier, {
            "type": "results_ready",
            "msg": "Input Shaping analysis completed",
            "axis": self.currentAxis.upper(),
            "best_shaper": str(best_shaper),
            "signal_path": str(signal_path),
            "psd_path": str(psd_path),
            "command": str(command),
            "csv_path": str(self.csv_filename),
            "results": {
                k: {
                    "vibr": float(v["vibr"]),
                    "accel": float(v["accel"]),
                } for k, v in shaper_results.items()
            },
            "base_freq": float(base_freq)
            
        })
        
        data_for_plotly.update({
            "type": "plotly_data",
            "description": "Input Shaping Plotly Data",
            "axis": self.currentAxis.upper()
        })
        #self._plugin_logger.info(f"Sending plotly data to frontend: {json.dumps(data_for_plotly)}")
        self._plugin_manager.send_plugin_message(self._identifier, data_for_plotly)
        self.restore_shapers()
        self._plugin_logger.info("Restored shaper values to printer.")

        return    


    def _start_adxl_capture(self, freq=3200):
        wrapper = None
        sensor_type = self._settings.get(['sensorType'])
        plugin_dir = os.path.dirname(os.path.abspath(__file__))

        if sensor_type == "adxlusb":
            wrapper = os.path.join(plugin_dir, "adxl345usb.py")
            cmd = f"python3 {wrapper} -f {freq} -s {self.csv_filename}"
        elif sensor_type == "lis2dw":
            wrapper = "lis2dwusb"
            cmd = f"sudo {wrapper} -f {freq} -s \"{self.csv_filename}\""
        else:
            wrapper = "adxl345spi"
            cmd = f"sudo {wrapper} -f {freq} -s {self.csv_filename}"

        try:
            self._adchild = pexpect.spawn(cmd, timeout=600, encoding='utf-8')

            # Wait for ready message or error
            i = self._adchild.expect([
                "Press Q to stop",
                pexpect.EOF,
                pexpect.TIMEOUT,
                "Traceback",
                "Error",
            ], timeout=30)
            if i == 0:
                self._plugin_logger.info(f"{sensor_type} ready and capturing.")
            else:
                self._plugin_logger.error(f"{sensor_type} failed to start. Output: {self._adchild.before}")
                raise RuntimeError("Sensor process failed to start.")
        except Exception as e:
            self._plugin_logger.error(f"Unexpected error: {e}")
            raise
 

    def _stop_adxl_capture(self):
        self._plugin_logger.info("Stopping ADXL345 capture...")
        if self._adchild and self._adchild.isalive():
            try:
                self._adchild.sendline("Q")
                self._adchild.expect("Saved .* samples", timeout=30)
                self._plugin_logger.info("ADXL345 confirmed data saved.")
            except pexpect.TIMEOUT:
                self._plugin_logger.warning("No save confirmation. Terminating...")
                self._adchild.terminate(force=True)
            except pexpect.EOF:
                self._plugin_logger.info("Process already exited.")
            finally:
                if self._adchild.logfile:
                    self._adchild.logfile.close()
        else:
            self._plugin_logger.warning("Process not alive.")
            
            
            
            

    def get_update_information(self):
        return {
            "Pinput_Shaping": {
                "displayName": "Pinput_Shaping Plugin",
                "displayVersion": self._plugin_version,
                # version check: github repository
                "type": "github_release",
                "user": "navaismo",
                "repo": "OctoPrint-Pinput_Shaping",
                "current": self._plugin_version,
                # update method: pip
                "pip": "https://github.com/navaismo/OctoPrint-Pinput_Shaping/archive/{target_version}.zip",
            }
        }


__plugin_pythoncompat__ = ">=3,<4"  # Only Python 3
__plugin_version__ = "0.0.4.6"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_name__ = "Pinput_Shaping"
    __plugin_implementation__ = PinputShapingPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        #"octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.gcode_sending_handler,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.gcode_received_handler
    }
