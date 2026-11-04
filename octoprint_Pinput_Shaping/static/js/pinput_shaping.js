$(function () {
    "use strict";

    function PinputShapingViewModel(parameters) {
        const self = this;
        self.settingsViewModel = parameters[0];
        self.printerState = parameters[1];
        self.busy = ko.observable(false);
        self.stage = ko.observable("idle");
        self.error = ko.observable("");
        self.summary = ko.observable("");
        self.recoveryRequired = ko.observable(false);
        self.modesReady = ko.observable(false);
        self.firmware = ko.observable({state: "unknown", protocol: null, label: "Not detected",
            allowed_shapers: [], message: "Click Detect firmware while the printer is connected and idle."});
        self.result = ko.observable(null);
        self.selectedShaper = ko.observable(null);
        self.samples = ko.observableArray([]);
        self.progress = ko.observable(0);
        self.interactiveAvailable = ko.observable(typeof Plotly !== "undefined");
        self.sizeX = ko.observable(220);
        self.sizeY = ko.observable(220);
        self.sizeZ = ko.observable(250);
        self.selectedZ = ko.observable(10);
        self.selectedCell = ko.observable({row: 1, col: 1});
        self.grid = Array.from({length: 3}, (_, row) =>
            Array.from({length: 3}, (_, col) => ({row: 2 - row, col: col})));
        self.calculatedX = ko.pureComputed(() =>
            Math.round((self.selectedCell().col + 0.5) * self.sizeX() / 3));
        self.calculatedY = ko.pureComputed(() =>
            Math.round((self.selectedCell().row + 0.5) * self.sizeY() / 3));

        const settingKeys = ["accelMin", "accelMax", "freqStart", "freqEnd",
            "sweepDuration", "dampingRatio", "sensorType", "sensorAxis", "sensorPort", "lis2dwRate"];
        settingKeys.forEach(key => { self[key] = ko.observable(); });

        self.available = ko.pureComputed(() =>
            self.printerState.isOperational() && !self.printerState.isPrinting() &&
            !self.busy() && !self.recoveryRequired());
        self.firmwareReady = ko.pureComputed(() => self.firmware().state === "ready");
        self.canMove = ko.pureComputed(() => self.available() && self.modesReady() && self.firmwareReady());
        self.applyLabel = ko.pureComputed(() => self.firmware().protocol === "legacy" ?
            "Apply and verify recommended ZV" : "Apply and verify selected shaper");
        self.shaperChoices = ko.pureComputed(() => {
            const result = self.result();
            const firmware = self.firmware();
            if (!result || !result.shapers || result.firmware_mode !== firmware.protocol) return [];
            return Object.keys(result.shapers).filter(name => firmware.allowed_shapers.includes(name))
                .map(name => ({name: name, label: name + (name === result.best_shaper ? " (recommended)" : "")}));
        });
        self.canSelectShaper = ko.pureComputed(() => self.available() && self.firmwareReady() &&
            self.firmware().protocol === "ftm" && self.shaperChoices().length > 0);
        self.selectedCandidate = ko.pureComputed(() => {
            const result = self.result();
            return result && result.shapers ? result.shapers[self.selectedShaper()] || null : null;
        });
        self.selectedCommand = ko.pureComputed(() => {
            const candidate = self.selectedCandidate();
            return candidate && candidate.command || "";
        });
        self.canApply = ko.pureComputed(() => {
            const result = self.result();
            const firmware = self.firmware();
            const candidate = self.selectedCandidate();
            return self.available() && self.firmwareReady() && !!result &&
                result.firmware_mode === firmware.protocol &&
                firmware.allowed_shapers.includes(self.selectedShaper()) &&
                !!candidate && !!self.selectedCommand() &&
                ["X", "Y"].includes(result.axis) &&
                Number.isFinite(candidate.shaper_freq) &&
                candidate.shaper_freq >= 20 && candidate.shaper_freq <= 200 &&
                Number.isFinite(result.damping) && !!result.session_id;
        });
        self.shapers = ko.pureComputed(() => {
            const result = self.result();
            if (!result) return [];
            return Object.entries(result.shapers).map(([name, value]) => ({
                name: name,
                frequency: Number(value.shaper_freq).toFixed(2),
                vibration: (100 * value.vibr).toFixed(1) + "%",
                duration: Number(value.shaper_duration_ms).toFixed(1),
                acceleration: value.suggested_accel,
                best: name === result.best_shaper
            }));
        });

        self.fileURL = function (kind) {
            const result = self.result();
            if (!result || !result.files[kind]) return "";
            const root = typeof BASEURL === "string" ? BASEURL : "/";
            return root + "plugin/Pinput_Shaping/files/" +
                encodeURIComponent(result.session_id) + "/" +
                encodeURIComponent(result.files[kind]);
        };

        function showResult(result) {
            if (!result || !result.shapers || !Number.isFinite(result.best_shaper_freq)) {
                self.result(null);
                return;
            }
            const previous = self.result();
            const resetSelection = !previous || previous.session_id !== result.session_id ||
                !Object.prototype.hasOwnProperty.call(result.shapers, self.selectedShaper());
            self.result(result);
            if (resetSelection) self.selectedShaper(result.best_shaper);
            if (typeof Plotly === "undefined") return;
            try {
                Plotly.react("pinput_signal", [
                    {x: result.time, y: result.raw, name: "Raw sensor data", mode: "lines"},
                    {x: result.time, y: result.filtered, name: "Preview filter", mode: "lines"}
                ], {title: "Axis " + result.axis, xaxis: {title: "Time (s)"},
                    yaxis: {title: "Acceleration (sensor units)"}}, {responsive: true});
                const traces = [{x: result.freqs, y: result.psd_original,
                    name: "Unshaped reference", mode: "lines"}];
                Object.entries(result.shapers).forEach(([name, shaper]) => {
                    traces.push({x: result.freqs, y: shaper.psd, mode: "lines",
                        name: name + " @ " + shaper.shaper_freq.toFixed(1) + " Hz (predicted)"});
                });
                Plotly.react("pinput_psd", traces, {
                    title: "Measured resonance: " + result.base_freq.toFixed(1) + " Hz",
                    xaxis: {title: "Frequency (Hz)", range: [
                        Math.max(5, Number(self.freqStart())), Number(self.freqEnd())]},
                    yaxis: {title: "PSD (sensor units²/Hz)"}
                }, {responsive: true});
            } catch (error) {
                console.warn("Interactive charts unavailable; PNG plots remain available.", error);
            }
        }

        if (typeof window !== "undefined") {
            window.addEventListener("pinputPlotlyReady", function () {
                self.interactiveAvailable(typeof Plotly !== "undefined");
                if (self.result()) showResult(self.result());
            });
        }

        function updateState(data) {
            self.busy(!!data.busy);
            self.stage(data.stage || "idle");
            self.error(data.error || "");
            self.recoveryRequired(!!data.recovery_required);
            self.modesReady(!!data.modes_ready);
            self.firmware(data.firmware || {state: "unknown", protocol: null, label: "Not detected",
                allowed_shapers: [], message: "Firmware detection is required before calibration."});
            if (data.result) {
                if (!self.result() || self.result().session_id !== data.result.session_id) {
                    showResult(data.result);
                }
            } else {
                self.result(null);
            }
            if (data.volume) {
                self.sizeX(data.volume.width);
                self.sizeY(data.volume.depth);
                self.sizeZ(data.volume.height);
            }
        }

        self.refresh = function () {
            return OctoPrint.simpleApiGet("Pinput_Shaping").done(updateState);
        };

        function failure(response) {
            const message = response.responseJSON && response.responseJSON.error ||
                response.responseText || "The request failed.";
            self.busy(false);
            self.refresh().always(function () { self.error(message); });
        }

        function request(command, payload, clearResult) {
            if (self.busy()) return;
            self.error("");
            self.summary("");
            self.progress(0);
            self.busy(true);
            if (clearResult) self.result(null);
            return OctoPrint.simpleApiCommand("Pinput_Shaping", command, payload || {})
                .fail(failure);
        }

        function position(axis) {
            return {data: {axis: axis, start_x: self.calculatedX(),
                start_y: self.calculatedY(), start_z: Number(self.selectedZ())}};
        }

        self.prepareModes = function () {
            if (!self.available()) return;
            if (confirm("Use millimeters, absolute positioning, absolute extrusion and a 1500 mm/min feed rate? No axis movement will be commanded.")) {
                request("prepare_modes", {}, true);
            }
        };
        self.runAccTest = function () {
            if (self.available()) request("run_accelerometer_test", {}, true);
        };
        self.detectFirmware = function () {
            if (self.available()) request("detect_firmware", {}, false);
        };
        self.runAxisTest = function (axis) {
            if (self.canMove()) request("run_axis_test", position(axis), true);
        };
        self.runResonance = function (axis) {
            if (!self.canMove()) return;
            if (confirm("Start " + axis + " resonance calibration? The printer will home and move at the selected position. Check sensor mounting and clear the travel area.")) {
                request("run_resonance_test", position(axis), true);
            }
        };
        self.cancel = function () {
            OctoPrint.simpleApiCommand("Pinput_Shaping", "cancel", {}).fail(failure);
        };
        self.emergencyStop = function () {
            OctoPrint.simpleApiCommand("Pinput_Shaping", "cancel", {});
            OctoPrint.control.sendGcode("M112").fail(failure);
        };
        self.applyResult = function () {
            if (!self.canApply()) return;
            const result = self.result();
            const shaper = self.selectedShaper();
            if (confirm("Apply and verify " + shaper + " at " + self.selectedCandidate().shaper_freq + " Hz?\n\n" + self.selectedCommand() +
                "\n\nEEPROM will not be written.")) {
                request("apply_result", {session_id: result.session_id, shaper: shaper}, false);
            }
        };
        self.useRecommendation = function () {
            if (self.canSelectShaper()) self.selectedShaper(self.result().best_shaper);
        };
        self.recover = function () {
            if (self.busy()) return;
            if (confirm("Restore the firmware settings saved before the interrupted calibration? Confirm this is the same printer.")) {
                request("recover", {}, false);
            }
        };

        self.onSettingsBeforeSave = function () {
            const settings = self.settingsViewModel.settings.plugins.Pinput_Shaping;
            settingKeys.forEach(key => { settings[key](self[key]()); });
        };
        self.onStartupComplete = function () {
            const settings = self.settingsViewModel.settings.plugins.Pinput_Shaping;
            settingKeys.forEach(key => { self[key](settings[key]()); });
            self.refresh();
        };
        self.onServerReconnect = self.refresh;
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "Pinput_Shaping") return;
            if (data.type === "state") updateState(data);
            else if (data.type === "results_ready") showResult(data.result);
            else if (data.type === "progress") self.progress(Math.round(data.progress * 100));
            else if (data.type === "sensor_values") {
                self.samples(data.samples);
                self.summary(data.summary);
            } else if (data.type === "applied") {
                self.result(null);
                self.summary(data.message);
            } else if (data.type === "completed") {
                self.summary(data.message);
            }
        };
        setInterval(function () { if (self.busy()) self.refresh(); }, 5000);
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: PinputShapingViewModel,
        dependencies: ["settingsViewModel", "printerStateViewModel"],
        elements: ["#pinput_shaping_tab", "#settings_plugin_Pinput_Shaping"]
    });
});

