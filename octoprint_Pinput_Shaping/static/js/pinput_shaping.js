$(function () {
    "use strict";

    function PinputShapingViewModel(parameters) {
        const self = this;
        self.settingsViewModel = parameters[0];
        self.printerState = parameters[1];
        self.busy = ko.observable(false);
        self.savingSettings = ko.observable(false);
        self.stage = ko.observable("idle");
        self.error = ko.observable("");
        self.errorToastOpen = ko.observable(false);
        self.view = ko.observable("setup");
        self.chartTab = ko.observable("overview");
        self.operation = ko.observable("");
        self.progressPhase = ko.observable(-1);
        self.protocolEvents = ko.observableArray([]);
        self.protocolLoading = ko.observable(false);
        self.fullscreenAvailable = typeof document !== "undefined" && !!document.documentElement.requestFullscreen;
        self.summary = ko.observable("");
        self.recoveryRequired = ko.observable(false);
        self.modesReady = ko.observable(false);
        self.firmware = ko.observable({state: "unknown", protocol: null, label: "Not detected",
            allowed_shapers: [], message: "Click Detect firmware while the printer is connected and idle."});
        self.result = ko.observable(null);
        self.selectedShaper = ko.observable(null);
        self.samples = ko.observableArray([]);
        self.progress = ko.observable(0);
        self.progressSource = ko.observable("");
        self.sensorState = ko.observable("");
        self.nativeCapability = ko.observable({state: "unknown", protocol: null,
            message: "Click Detect M495 Support for this printer connection."});
        self.diagnostics = ko.observable(null);
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
            "sweepDuration", "dampingRatio", "sensorType", "sensorAxis", "sensorPort", "adxlRate", "lis2dwRate", "lis2dwRange",
            "excitationBackend", "nativeStart", "nativeEnd", "nativeOctave", "nativeAccel",
            "nativePrepareTimeout", "nativeMaxCapture", "nativeCancelTimeout"];
        settingKeys.forEach(key => { self[key] = ko.observable(); });

        self.number = function (value, digits) {
            return Number.isFinite(value) ? value.toFixed(digits === undefined ? 1 : digits) : "—";
        };
        self.backendLabel = ko.pureComputed(() => self.nativeSelected() ? "Marlin M495" : "Software excitation");
        self.sensorLabel = ko.pureComputed(() => {
            if (self.sensorType() === "lis2dw") return "LIS2DW ±" + self.lis2dwRange() + " g";
            return self.sensorType() === "adxlusb" ? "ADXL345 USB" : "ADXL345 SPI";
        });
        self.qualityInfo = ko.pureComputed(() => self.result() && self.result().quality || {});
        self.resultRange = ko.pureComputed(() => {
            const sensor = self.qualityInfo().accelerometer;
            return sensor && Number.isFinite(sensor.range_g) ? "±" + sensor.range_g + " g" : "Not reported";
        });
        self.resultBackend = ko.pureComputed(() => self.result() && self.result().backend === "marlin_m495" ?
            "Marlin M495" : "Software excitation");
        self.qualityJSON = ko.pureComputed(() => JSON.stringify(self.qualityInfo(), null, 2));
        self.progressSteps = ko.pureComputed(() => ["Prepare", "Position", "Acquire", "Measure", "Validate & analyze"]
            .map((label, index) => ({label: label, done: index < self.progressPhase(),
                current: index === self.progressPhase(), failed: index === self.progressPhase() && !!self.error(),
                status: index < self.progressPhase() ? "Complete" :
                    index === self.progressPhase() ? (self.error() ? "Interrupted" : "In progress") : "Pending"})));

        let lastError = "";
        function setError(message) {
            message = message || "";
            self.error(message);
            if (message && message !== lastError) self.errorToastOpen(true);
            if (!message) self.errorToastOpen(false);
            lastError = message;
        }
        self.dismissError = function () { self.errorToastOpen(false); };
        self.openError = function () { if (self.error()) self.errorToastOpen(true); };
        self.showSetup = function () { self.view("setup"); };
        self.showResults = function () {
            if (self.result()) {
                self.view("results");
                resizeCharts();
            }
        };
        self.selectShaper = function (name) {
            if (self.canSelectShaper() && self.shaperChoices().some(choice => choice.name === name)) self.selectedShaper(name);
        };
        self.selectChartTab = function (tab) {
            if (!["overview", "signal", "frequency", "diagnostics"].includes(tab)) return;
            self.chartTab(tab);
            resizeCharts();
        };
        self.chartTabKey = function (data, event) {
            const tabs = ["overview", "signal", "frequency", "diagnostics"];
            const index = tabs.indexOf(self.chartTab());
            const next = {ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length,
                Home: 0, End: tabs.length - 1}[event.key];
            if (next === undefined) return true;
            event.preventDefault();
            self.selectChartTab(tabs[next]);
            if (typeof document !== "undefined") document.getElementById("pinput-tab-" + tabs[next]).focus();
            return false;
        };
        function resizeCharts() {
            if (typeof window === "undefined" || typeof Plotly === "undefined" || !Plotly.Plots) return;
            window.requestAnimationFrame(function () {
                ["pinput_signal", "pinput_psd"].forEach(id => {
                    const element = document.getElementById(id);
                    if (element && element.offsetWidth && element.data) Plotly.Plots.resize(element);
                });
            });
        }
        self.toggleFullscreen = function () {
            if (!self.fullscreenAvailable) return;
            const task = document.fullscreenElement ? document.exitFullscreen() :
                document.getElementById("pinput-analysis").requestFullscreen();
            if (task && task.catch) task.catch(error => console.warn("Fullscreen unavailable", error));
        };
        if (typeof document !== "undefined") document.addEventListener("fullscreenchange", resizeCharts);

        function observeStage(stage) {
            if (self.operation() !== "run_resonance_test" || self.error()) return;
            let phase = -1;
            if (/prepar|checking sensor/.test(stage)) phase = 0;
            if (/homing|positioning/.test(stage)) phase = 1;
            if (/ARMED_WAIT|arming native/.test(stage)) phase = 2;
            if (/^measuring/.test(stage)) phase = 3;
            if (/stopping capture|validating capture|analyzing|restoring|verifying printer settings/.test(stage)) phase = 4;
            self.progressPhase(Math.max(self.progressPhase(), phase));
        }

        let protocolSession = "", protocolRequest = 0;
        function clearProtocolEvents() {
            self.protocolEvents([]);
            self.protocolLoading(false);
            protocolSession = "";
            ++protocolRequest;
        }
        function loadProtocolEvents() {
            const source = self.result() && self.result().files && self.result().files.events ? self.result() : self.diagnostics();
            if (!source || !source.files || !source.files.events || self.busy() ||
                    typeof window === "undefined" || !window.fetch || source.session_id === protocolSession) return;
            const session = source.session_id, requestId = ++protocolRequest;
            protocolSession = session;
            self.protocolEvents([]);
            self.protocolLoading(true);
            const descriptions = {HOMED: "Homing and positioning completed", ARMED_WAIT: "Prepared; waiting for host ARM",
                ARM_ACK: "Host ARM accepted", START: "Native excitation started", COMPLETE: "Native movement completed",
                ABORTED: "Native test aborted", ERROR: "Firmware error", ARM_REJECTED: "Host ARM rejected"};
            window.fetch(self.fileURL("events"), {credentials: "same-origin"}).then(response => {
                if (!response.ok) throw Error("Protocol events unavailable");
                return response.json();
            }).then(events => {
                if (requestId !== protocolRequest || !Array.isArray(events)) return;
                const selected = events.filter(item => item.event && descriptions[item.event.kind] && Number.isFinite(item.host_time));
                const origin = selected.length ? selected[0].host_time : 0;
                self.protocolEvents(selected.map(item => ({kind: item.event.kind,
                    elapsed: (item.host_time - origin).toFixed(2),
                    description: descriptions[item.event.kind] + (item.event.reason ? ": " + item.event.reason : ""),
                    failed: ["ERROR", "ABORTED", "ARM_REJECTED"].includes(item.event.kind)})));
            }).catch(error => {
                if (requestId === protocolRequest) console.warn("Use the protocol events download for diagnostics.", error);
            }).finally(() => { if (requestId === protocolRequest) self.protocolLoading(false); });
        }

        self.available = ko.pureComputed(() =>
            self.printerState.isOperational() && !self.printerState.isPrinting() &&
            !self.busy() && !self.savingSettings() && !self.recoveryRequired());
        self.firmwareReady = ko.pureComputed(() => self.firmware().state === "ready");
        self.canMove = ko.pureComputed(() => self.available() && self.firmwareReady());
        self.nativeSelected = ko.pureComputed(() => self.excitationBackend() === "marlin_m495");
        self.nativeValidation = ko.pureComputed(() => {
            if (!self.nativeSelected()) return "";
            const start = Number(self.nativeStart()), end = Number(self.nativeEnd());
            const octave = Number(self.nativeOctave()), accel = Number(self.nativeAccel());
            const prep = Number(self.nativePrepareTimeout()), cap = Number(self.nativeMaxCapture());
            const cancel = Number(self.nativeCancelTimeout());
            if (![start, end, octave, accel, prep, cap, cancel].every(Number.isFinite) || start < 5 ||
                end <= start || end > 200 || octave < 20 || octave > 60 || accel < 0.0001 ||
                accel > 32767 * 4 * Math.PI * Math.PI / 8 - 1 || prep < 30 || prep > 240 ||
                cap < 30 || cap > 480 || cancel < 10 || cancel > 300 ||
                [start, end, octave].some(value => Math.abs(value * 100 - Math.round(value * 100)) > 1e-6)) {
                return "Native parameters are invalid. F: 5–199.99 Hz; T: greater than F, up to 200 Hz; O: 20–60 seconds.";
            }
            if (25 + octave * Math.log2(end / start) * 1.15 + 5 > cap) {
                return "Maximum capture must cover sensor startup, sweep and tail.";
            }
            return "";
        });
        self.canCalibrate = ko.pureComputed(() => self.canMove() &&
            (!self.nativeSelected() || (self.nativeCapability().state === "supported" &&
                self.nativeCapability().protocol === 2 && !self.nativeValidation())));
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
                result.recommendation_valid !== false &&
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
            if (!result || !result.shapers) return [];
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
            const result = self.result() && self.result().files[kind] ? self.result() : self.diagnostics();
            if (!result || !result.files[kind]) return "";
            const root = typeof BASEURL === "string" ? BASEURL : "/";
            return root + "plugin/Pinput_Shaping/files/" +
                encodeURIComponent(result.session_id) + "/" +
                encodeURIComponent(result.files[kind]);
        };

        function showResult(result) {
            if (!result || !result.shapers || (!Number.isFinite(result.best_shaper_freq) &&
                    !(result.backend === "marlin_m495" && result.recommendation_valid === false))) {
                self.result(null);
                clearProtocolEvents();
                return;
            }
            const previous = self.result();
            const resetSelection = !previous || previous.session_id !== result.session_id ||
                !Object.prototype.hasOwnProperty.call(result.shapers, self.selectedShaper());
            self.result(result);
            if (resetSelection) {
                self.selectedShaper(result.best_shaper);
                self.chartTab("overview");
                self.view("results");
            }
            self.progressPhase(5);
            loadProtocolEvents();
            if (typeof Plotly === "undefined") return;
            try {
                Plotly.react("pinput_signal", [
                    {x: result.time, y: result.raw, name: "Raw sensor data", mode: "lines"},
                    {x: result.time, y: result.filtered, name: "Preview filter", mode: "lines"}
                ], chartLayout({title: "Time Signal (" + result.axis + " Axis)", xaxis: {title: "Time (s)"},
                    yaxis: {title: "Acceleration (sensor units)"}}), {responsive: true, displaylogo: false});
                const traces = [{x: result.freqs, y: result.psd_original,
                    name: result.backend === "marlin_m495" ? "Relative fundamental response" : "Unshaped reference", mode: "lines"}];
                Object.entries(result.shapers).forEach(([name, shaper]) => {
                    traces.push({x: result.freqs, y: shaper.psd, mode: "lines",
                        name: name + " @ " + shaper.shaper_freq.toFixed(1) + " Hz (predicted)"});
                });
                Plotly.react("pinput_psd", traces, chartLayout({
                    title: Number.isFinite(result.base_freq) ? "Measured resonance: " + result.base_freq.toFixed(1) + " Hz" : "Native sweep response",
                    xaxis: {title: "Frequency (Hz)", range: [
                        result.native_parameters ? result.native_parameters.start : Math.max(5, Number(self.freqStart())),
                        result.native_parameters ? result.native_parameters.end : Number(self.freqEnd())]},
                    yaxis: {title: result.spectrum_label || "PSD (sensor units²/Hz)"}
                }), {responsive: true, displaylogo: false});
            } catch (error) {
                console.warn("Interactive charts unavailable; PNG plots remain available.", error);
            }
        }

        function chartLayout(layout) {
            const theme = chartTheme();
            const axis = {gridcolor: theme.line, zerolinecolor: theme.line, automargin: true};
            return Object.assign({paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", font: {color: theme.text},
                margin: {l: 58, r: 20, t: 50, b: 90}, legend: {orientation: "h", y: -0.25}, colorway: theme.colors,
                modebar: {bgcolor: "rgba(0,0,0,0)", color: theme.text, activecolor: theme.accent}},
                layout, {xaxis: Object.assign({}, axis, layout.xaxis), yaxis: Object.assign({}, axis, layout.yaxis)});
        }

        function chartTheme() {
            const root = typeof document !== "undefined" && document.getElementById("pinput_shaping_tab");
            const style = root && window.getComputedStyle(root);
            const dark = root && root.style.colorScheme === "dark";
            return {text: style && style.getPropertyValue("--pinput-text").trim() || "#333333",
                line: style && style.getPropertyValue("--pinput-line").trim() || "rgba(0,0,0,0.2)",
                accent: style && style.getPropertyValue("--pinput-accent").trim() || "#1769aa",
                colors: dark ? ["#39a7ff", "#ff982f", "#50cc7f", "#d98bff", "#ff6485", "#b8d9ed", "#e4cc62", "#5bd4db"] :
                    ["#1478b8", "#b75b0d", "#267b42", "#8545b8", "#bb3758", "#557488", "#907318", "#187b83"]};
        }

        let themeSignature = "", themeObserver = null;
        self.syncTheme = function () {
            if (typeof document === "undefined" || typeof window === "undefined" || !window.getComputedStyle) return;
            const root = document.getElementById("pinput_shaping_tab");
            if (!root || !root.parentElement) return;
            const text = window.getComputedStyle(root.parentElement).color;
            let background = "rgb(255, 255, 255)";
            for (let element = root.parentElement; element; element = element.parentElement) {
                const color = window.getComputedStyle(element).backgroundColor;
                if (color && color !== "transparent" && !/^rgba\([^)]*,\s*0\s*\)$/.test(color)) {
                    background = color;
                    break;
                }
            }
            const signature = background + "|" + text;
            if (signature === themeSignature) return;
            themeSignature = signature;
            const rgb = background.match(/[\d.]+/g) || [255, 255, 255];
            const dark = rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722 < 128;
            const foreground = text.match(/[\d.]+/g) || [51, 51, 51];
            const accent = dark ? "112,186,255" : "23,105,170";
            const success = dark ? "128,212,155" : "38,114,59";
            const warning = dark ? "232,189,98" : "128,91,18";
            const values = {background: background, text: text,
                muted: "rgba(" + foreground.slice(0, 3).join(",") + ",0.82)",
                line: "rgba(" + foreground.slice(0, 3).join(",") + ",0.25)",
                accent: "rgb(" + accent + ")", "accent-soft": "rgba(" + accent + ",0.08)",
                success: "rgb(" + success + ")", "success-soft": "rgba(" + success + ",0.09)",
                warning: "rgb(" + warning + ")", "warning-soft": "rgba(" + warning + ",0.08)",
                error: dark ? "#ff9ca2" : "#ad2637"};
            Object.entries(values).forEach(([name, value]) => root.style.setProperty("--pinput-" + name, value));
            root.style.colorScheme = dark ? "dark" : "light";
            if (typeof Plotly !== "undefined" && Plotly.relayout) {
                const theme = chartTheme();
                ["pinput_signal", "pinput_psd"].forEach(id => {
                    const element = document.getElementById(id);
                    if (element && element.data) Promise.resolve(Plotly.relayout(element, {"paper_bgcolor": "rgba(0,0,0,0)",
                        "plot_bgcolor": "rgba(0,0,0,0)", "font.color": theme.text, "colorway": theme.colors,
                        "modebar.bgcolor": "rgba(0,0,0,0)", "modebar.color": theme.text, "modebar.activecolor": theme.accent,
                        "xaxis.gridcolor": theme.line, "yaxis.gridcolor": theme.line,
                        "xaxis.zerolinecolor": theme.line, "yaxis.zerolinecolor": theme.line}))
                        .catch(error => console.warn("Could not update chart theme", error));
                });
            }
        };

        function watchTheme() {
            self.syncTheme();
            if (themeObserver || typeof MutationObserver === "undefined") return;
            let scheduled = false;
            function scheduleTheme() {
                if (scheduled) return;
                scheduled = true;
                window.requestAnimationFrame(function () { scheduled = false; self.syncTheme(); });
            }
            themeObserver = new MutationObserver(function (records) {
                const root = document.getElementById("pinput_shaping_tab");
                if (records.some(record => root && !root.contains(record.target) &&
                        (record.type === "attributes" || document.head.contains(record.target)))) scheduleTheme();
            });
            themeObserver.observe(document.documentElement, {subtree: true, childList: true, attributes: true,
                attributeFilter: ["class", "style", "data-theme", "href", "disabled"]});
            document.addEventListener("load", function (event) {
                if (event.target.tagName === "LINK") scheduleTheme();
            }, true);
        }

        if (typeof window !== "undefined") {
            window.addEventListener("pinputPlotlyReady", function () {
                self.interactiveAvailable(typeof Plotly !== "undefined");
                if (self.result()) showResult(self.result());
            });
        }

        function updateState(data) {
            self.busy(!!data.busy || self.savingSettings());
            self.stage(self.savingSettings() ? "saving plugin settings" : data.stage || "idle");
            setError(data.error);
            observeStage(self.stage());
            self.recoveryRequired(!!data.recovery_required);
            self.modesReady(!!data.modes_ready);
            self.diagnostics(data.diagnostics || null);
            self.nativeCapability(data.native_capability || {state: "unknown", protocol: null,
                message: "Click Detect M495 Support for this printer connection."});
            self.firmware(data.firmware || {state: "unknown", protocol: null, label: "Not detected",
                allowed_shapers: [], message: "Firmware detection is required before calibration."});
            if (data.result) {
                if (!self.result() || self.result().session_id !== data.result.session_id) {
                    showResult(data.result);
                }
            } else {
                if (self.result()) clearProtocolEvents();
                self.result(null);
            }
            if (data.volume) {
                self.sizeX(data.volume.width);
                self.sizeY(data.volume.depth);
                self.sizeZ(data.volume.height);
            }
            loadProtocolEvents();
        }

        self.refresh = function () {
            return OctoPrint.simpleApiGet("Pinput_Shaping").done(updateState);
        };

        function failure(response) {
            const message = response.responseJSON && response.responseJSON.error ||
                response.responseText || "The request failed.";
            self.busy(false);
            self.refresh().always(function () { setError(message); });
        }

        function request(command, payload, clearResult) {
            if (self.busy()) return;
            setError("");
            self.summary("");
            self.progress(0);
            self.progressSource("");
            self.sensorState("");
            self.busy(true);
            self.operation(command);
            self.stage("starting request");
            if (clearResult) {
                self.result(null);
                self.view("setup");
                self.progressPhase(command === "run_resonance_test" ? 0 : -1);
                clearProtocolEvents();
            }
            return OctoPrint.simpleApiCommand("Pinput_Shaping", command, payload || {})
                .fail(failure);
        }

        function position(axis) {
            return {data: {axis: axis, start_x: self.calculatedX(),
                start_y: self.calculatedY(), start_z: Number(self.selectedZ())}};
        }

        self.runAccTest = function () {
            if (self.available()) request("run_accelerometer_test", {}, true);
        };
        function applySavedSettings(values) {
            const settings = self.settingsViewModel.settings.plugins.Pinput_Shaping;
            Object.entries(values).forEach(([key, value]) => {
                if (settingKeys.includes(key)) self[key](value);
                if (typeof settings[key] === "function") settings[key](value);
            });
        }

        function detectAndSave(command) {
            if (!self.available()) return;
            const values = {};
            settingKeys.forEach(key => { values[key] = self[key](); });
            self.busy(true);
            self.savingSettings(true);
            self.stage("saving plugin settings");
            setError("");
            // Save only this plugin's displayed settings before freezing a
            // detection session; unrelated settings in the dialog stay untouched.
            return OctoPrint.settings.save({plugins: {Pinput_Shaping: values}}).done(function () {
                applySavedSettings(values);
                self.savingSettings(false);
                self.busy(false);
                request(command, {}, false);
            }).fail(function (response) {
                self.savingSettings(false);
                failure(response);
            });
        }
        self.detectFirmware = function () { return detectAndSave("detect_firmware"); };
        self.detectM495 = function () { return detectAndSave("detect_m495"); };
        self.runAxisTest = function (axis) {
            if (self.canMove()) request("run_axis_test", position(axis), true);
        };
        self.runResonance = function (axis) {
            if (!self.canCalibrate()) return;
            const movement = self.nativeSelected() ? "Marlin M495 will home and move to its internal test position." :
                "The printer will home and move at the selected position.";
            if (confirm("Start " + axis + " resonance calibration? " + movement + " Check sensor mounting and clear the travel area.")) {
                const payload = self.nativeSelected() ? {data: {axis: axis}} : position(axis);
                payload.data.backend = self.excitationBackend();
                if (self.nativeSelected()) {
                    payload.data.native_settings = {};
                    ["nativeStart", "nativeEnd", "nativeOctave", "nativeAccel", "nativePrepareTimeout",
                        "nativeMaxCapture", "nativeCancelTimeout"].forEach(key => { payload.data.native_settings[key] = self[key](); });
                }
                request("run_resonance_test", payload, true);
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
            if (confirm("Recover the interrupted calibration? Confirm this is the same printer. For native M495 recovery after a disconnect or reset, first verify the printer is fully stopped or reset. Recovery sends M496 and drains its queued acknowledgment before verifying settings.")) {
                request("recover", {native_idle_confirmed: true}, false);
            }
        };

        self.onSettingsBeforeSave = function () {
            const settings = self.settingsViewModel.settings.plugins.Pinput_Shaping;
            settingKeys.forEach(key => { settings[key](self[key]()); });
        };
        self.onStartupComplete = function () {
            watchTheme();
            const settings = self.settingsViewModel.settings.plugins.Pinput_Shaping;
            settingKeys.forEach(key => { self[key](settings[key]()); });
            self.refresh();
        };
        self.onServerReconnect = self.refresh;
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "Pinput_Shaping") return;
            if (data.type === "state") updateState(data);
            else if (data.type === "detection_saved") {
                applySavedSettings(data.settings || {});
                self.summary(data.message || "Detection and settings saved.");
            } else if (data.type === "results_ready") showResult(data.result);
            else if (data.type === "progress") {
                self.progress(Math.round(data.progress * 100));
                self.progressSource(data.progress_source || "");
                self.sensorState(data.sensor_state || "");
            }
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

