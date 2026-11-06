"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
function observable(initial) {
    let value = initial;
    return function (next) {
        if (arguments.length) { value = next; return this; }
        return value;
    };
}
const requests = [];
const settingsSaves = [];
const ftm = {state: "ready", protocol: "ftm", label: "FTM v2 (M493)",
    allowed_shapers: ["ZV", "MZV", "EI"], message: "Select a fitted shaper."};
const legacy = {state: "ready", protocol: "legacy", label: "Legacy ZV (M593)",
    allowed_shapers: ["ZV"], message: "ZV only."};
let currentState = {busy: false, modes_ready: true, firmware: ftm};
function response(value) {
    return {done(fn) { fn(value); return this; }, fail() { return this; }, always(fn) { fn(); return this; }};
}
const context = {
    $: fn => fn(), ko: process.env.PINPUT_KNOCKOUT_PATH ? require(process.env.PINPUT_KNOCKOUT_PATH) :
        {observable, observableArray: observable, pureComputed: fn => fn},
    OCTOPRINT_VIEWMODELS: [], BASEURL: "/octoprint/", console,
    confirm: message => { confirmations.push(message); return true; }, setInterval() {},
    OctoPrint: {
        settings: {save: values => { settingsSaves.push(values); return response({}); }},
        simpleApiGet: () => response(currentState),
        simpleApiCommand: (plugin, command, data) => {
            requests.push({plugin, command, data});
            return response({success: true});
        },
        control: {sendGcode: command => { requests.push({gcode: command}); return response({}); }}
    }
};
const confirmations = [];
vm.createContext(context);
vm.runInContext(fs.readFileSync("octoprint_Pinput_Shaping/static/js/pinput_shaping.js", "utf8"), context);
const settings = {};
for (const key of ["accelMin", "accelMax", "freqStart", "freqEnd", "sweepDuration", "dampingRatio", "sensorType", "sensorAxis", "sensorPort", "adxlRate", "lis2dwRate", "lis2dwRange",
    "excitationBackend", "nativeStart", "nativeEnd", "nativeOctave", "nativeAccel", "nativePrepareTimeout", "nativeMaxCapture", "nativeCancelTimeout"]) {
    settings[key] = observable(1);
}
settings.lis2dwRate(1600);
settings.lis2dwRange(16);
const nativeDefaults = {excitationBackend: "software", nativeStart: 20, nativeEnd: 60, nativeOctave: 20,
    nativeAccel: 40, nativePrepareTimeout: 180, nativeMaxCapture: 300, nativeCancelTimeout: 200};
for (const [key, value] of Object.entries(nativeDefaults)) settings[key](value);
const Model = context.OCTOPRINT_VIEWMODELS[0].construct;
const model = new Model([{settings: {plugins: {Pinput_Shaping: settings}}},
    {isOperational: () => true, isPrinting: () => false}]);
model.onStartupComplete();
assert.equal(model.view(), "setup", "The workspace starts with test preparation");
assert.equal(model.lis2dwRate(), 1600);
model.lis2dwRate(800);
model.onSettingsBeforeSave();
assert.equal(settings.lis2dwRate(), 800, "The selected sample rate is saved for the next session");
for (const sensor of ["adxlusb", "adxlspi"]) {
    model.sensorType(sensor);
    for (const rate of [800, 1600, 3200]) {
        model.adxlRate(String(rate));
        model.onSettingsBeforeSave();
        assert.equal(Number(settings.adxlRate()), rate, "ADXL rates persist for both drivers");
        assert.equal(settings.sensorType(), sensor);
        assert.equal(settings.lis2dwRate(), 800, "ADXL selection preserves the LIS2DW rate");
    }
}
assert.equal(model.lis2dwRange(), 16);
for (const range of [2, 4, 8, 16]) {
    model.lis2dwRange(String(range));
    model.onSettingsBeforeSave();
    assert.equal(Number(settings.lis2dwRange()), range, "The selected accelerometer range is saved");
}
model.onServerReconnect();
model.refresh();
assert.equal(requests.length, 0, "Startup, reconnect and status refresh must not request firmware detection");
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result: {base_freq: 50}});
assert.equal(model.canApply(), false, "Incomplete results must not be applicable");
const result = {session_id: "a".repeat(32), axis: "X", base_freq: 50, best_shaper: "ZV",
    best_shaper_freq: 51, damping: 0.05, firmware_mode: "ftm", command: "M493 X C1 A51 I0.05", warnings: [],
    files: {csv: "capture.csv", signal: "signal.png", psd: "psd.png"},
    shapers: {ZV: {shaper_freq: 51, vibr: 0.1, shaper_duration_ms: 10, suggested_accel: 2000,
        command: "M493 X C1 A51 I0.05"},
    MZV: {shaper_freq: 63, vibr: 0.15, shaper_duration_ms: 12, suggested_accel: 1800,
        command: "M493 X C8 A63 I0.05"}}};
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result});
assert.equal(model.view(), "results", "Completed analysis opens the results workspace");
model.showSetup();
assert.equal(model.view(), "setup");
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result});
assert.equal(model.view(), "setup", "A repeated result does not override the user's navigation");
model.showResults();
assert.equal(model.view(), "results");
model.selectChartTab("signal");
assert.equal(model.chartTab(), "signal");
model.selectChartTab("unknown");
assert.equal(model.chartTab(), "signal");
model.selectChartTab("overview");
assert.equal(model.canApply(), true, "Valid results work without Plotly");
assert.equal(model.selectedShaper(), "ZV", "New results default to the recommendation");
assert.equal(model.canSelectShaper(), true);
model.selectedShaper("MZV");
model.selectShaper("not-fitted");
assert.equal(model.selectedShaper(), "MZV", "Table selection only accepts saved fitted shapers");
assert.equal(model.canApply(), true, "FTM can apply a non-recommended fitted shaper");
assert.equal(model.selectedCandidate().shaper_freq, 63);
assert.equal(model.selectedCommand(), "M493 X C8 A63 I0.05");
assert.equal(model.result().best_shaper, "ZV", "Manual selection does not overwrite the recommendation");
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result});
assert.equal(model.selectedShaper(), "MZV", "Refreshing the same result preserves the selection");
model.useRecommendation();
assert.equal(model.selectedShaper(), "ZV");
assert.equal(model.fileURL("csv"), "/octoprint/plugin/Pinput_Shaping/files/" + "a".repeat(32) + "/capture.csv");
model.firmware(legacy);
assert.equal(model.canApply(), false, "An FTM result cannot be applied to legacy firmware");
const legacyResult = {...result, firmware_mode: "legacy", command: "M593 X F51 D0.05",
    shapers: {ZV: {...result.shapers.ZV, command: "M593 X F51 D0.05"}}};
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result: legacyResult});
assert.equal(model.canApply(), true, "Legacy firmware accepts its recommended ZV");
assert.equal(model.canSelectShaper(), false, "Manual selection stays disabled for legacy");
assert.deepEqual(Array.from(model.shaperChoices(), choice => choice.name), ["ZV"]);
assert.match(model.applyLabel(), /recommended ZV/);
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready",
    result: {...legacyResult, best_shaper: "MZV", shapers: {MZV: {...result.shapers.ZV}}}});
assert.equal(model.canApply(), false, "Advanced shapers remain disabled for legacy firmware");
model.applyResult();
assert.equal(requests.length, 0, "Incompatible results never send an API request");
for (const state of ["unknown", "detecting", "unsupported"]) {
    model.firmware({...ftm, state});
    model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result});
    assert.equal(model.canApply(), false, state + " firmware cannot apply a result");
    assert.equal(model.canMove(), false, state + " firmware cannot start calibration motion");
}
model.firmware(ftm);
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result});
assert.equal(model.canMove(), true);
model.selectedShaper("EI");
assert.equal(model.canApply(), false, "A supported shaper without a saved fit cannot be applied");
model.selectedShaper("MZV");
model.busy(true);
assert.equal(model.canApply(), false);
model.applyResult();
assert.equal(requests.length, 0);
model.busy(false);
model.applyResult();
assert.equal(requests[0].command, "apply_result");
assert.equal(requests[0].data.session_id, result.session_id);
assert.equal(requests[0].data.shaper, "MZV");
assert.deepEqual(Object.keys(requests[0].data).sort(), ["session_id", "shaper"]);
assert.match(confirmations.at(-1), /MZV at 63 Hz/);
assert.match(confirmations.at(-1), /M493 X C8 A63 I0.05/);
assert.ok(requests.every(item => !item.gcode), "Application must go through server validation");
model.busy(false);
model.runResonance("Y");
assert.equal(model.result(), null, "Starting a test invalidates the previous result immediately");
assert.equal(model.view(), "setup");
assert.equal(model.canApply(), false);
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready",
    result: {...result, session_id: "b".repeat(32)}});
assert.equal(model.selectedShaper(), "ZV", "A new measurement resets the choice to its recommendation");
model.emergencyStop();
assert.equal(requests.at(-1).gcode, "M112");
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", busy: false, firmware: {state: "unknown",
    protocol: null, allowed_shapers: [], label: "Not detected", message: "Disconnected"}});
assert.equal(model.result(), null);
assert.equal(model.canApply(), false, "Connection invalidation immediately disables application");
model.detectFirmware();
assert.equal(requests.at(-1).command, "detect_firmware");
assert.equal(settingsSaves.length, 1);
assert.deepEqual(Object.keys(settingsSaves[0]), ["plugins"]);
assert.deepEqual(Object.keys(settingsSaves[0].plugins), ["Pinput_Shaping"]);
assert.equal(settingsSaves[0].plugins.Pinput_Shaping.lis2dwRate, model.lis2dwRate());
assert.equal(settingsSaves[0].plugins.Pinput_Shaping.lis2dwRange, model.lis2dwRange());
model.busy(false);
model.firmware(ftm);
model.modesReady(true);
assert.equal(model.excitationBackend(), "software");
model.excitationBackend("marlin_m495");
model.nativeEnd(65);
model.onSettingsBeforeSave();
assert.equal(settings.excitationBackend(), "marlin_m495");
assert.equal(settings.nativeEnd(), 65);
assert.equal(settings.lis2dwRate(), 800, "Backend selection never changes the sensor rate");
assert.equal(Number(settings.lis2dwRange()), 16, "Backend selection never changes the sensor range");
model.nativeEnd(60);
assert.equal(model.canCalibrate(), false, "Native calibration requires its own confirmed capability");
const beforeUnsupported = requests.length;
model.runResonance("X");
assert.equal(requests.length, beforeUnsupported, "There is no silent software fallback");
model.detectM495();
assert.equal(requests.at(-1).command, "detect_m495");
assert.equal(settingsSaves.length, 2);
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "detection_saved",
    settings: {excitationBackend: "marlin_m495", detectedNative: {protocol: 2}}, message: "Settings saved"});
assert.equal(model.excitationBackend(), "marlin_m495");
assert.equal(settings.excitationBackend(), "marlin_m495");
assert.equal(model.summary(), "Settings saved");
model.busy(false);
model.nativeCapability({state: "supported", protocol: 1, message: "Confirmed"});
assert.equal(model.canCalibrate(), false, "PIS v1 lacks the required host-arming barrier");
model.nativeCapability({state: "supported", protocol: 2, message: "Confirmed"});
assert.equal(model.canCalibrate(), true);
model.modesReady(false);
assert.equal(model.canCalibrate(), true, "Coordinate preparation belongs to the calibration worker");
for (const [key, invalid] of [["nativeStart", 4], ["nativeEnd", 201], ["nativeOctave", 19],
    ["nativeAccel", 0], ["nativeMaxCapture", 30], ["nativeStart", 20.001]]) {
    const previous = model[key]();
    model[key](invalid);
    assert.ok(model.nativeValidation());
    assert.equal(model.canCalibrate(), false);
    model[key](previous);
}
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "progress", progress: 0.95,
    progress_source: "estimated from elapsed time", sensor_state: "capturing"});
assert.equal(model.progress(), 95);
assert.match(model.progressSource(), /estimated/);
assert.equal(model.sensorState(), "capturing");
model.runResonance("X");
assert.equal(requests.at(-1).command, "run_resonance_test");
assert.equal(requests.at(-1).data.data.axis, "X");
for (const coordinate of ["start_x", "start_y", "start_z"]) {
    assert.equal(coordinate in requests.at(-1).data.data, false, "Native M495 does not accept host-selected positions");
}
assert.match(confirmations.at(-1), /Marlin M495 will home/);
assert.equal(model.progressSource(), "", "New sessions clear old progress labels");
model.busy(false);
const diagnostic = {...result, backend: "marlin_m495", base_freq: null, best_shaper: null,
    best_shaper_freq: null, shapers: {}, recommendation_valid: false};
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result: diagnostic});
assert.equal(model.result().recommendation_valid, false, "A diagnostic response remains viewable");
assert.equal(model.canApply(), false);
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready",
    result: {...result, recommendation_valid: false}});
assert.equal(model.canApply(), false, "An invalid recommendation stays blocked even if it contains shapers");
currentState = {busy: false, modes_ready: false, firmware: ftm, native_capability: {state: "unknown"},
    diagnostics: {session_id: "c".repeat(32), files: {events: "protocol_events.json"}}};
const beforeReconnect = requests.length;
model.onServerReconnect();
assert.equal(model.canCalibrate(), false);
assert.equal(requests.length, beforeReconnect, "Reconnect invalidation never probes automatically");
assert.match(model.fileURL("events"), /cccccccccccccccccccccccccccccccc\/protocol_events.json$/);
assert.equal(model.prepareModes, undefined, "Coordinate modes no longer require a separate UI action");
const tab = fs.readFileSync("octoprint_Pinput_Shaping/templates/pinput_shaping_tab.jinja2", "utf8");
assert.match(tab, /click: detectM495/);
assert.doesNotMatch(tab, /prepareModes|Prepare coordinate modes/);
const save = context.OctoPrint.settings.save;
let saved, saveFailed;
context.OctoPrint.settings.save = values => ({done(fn) { saved = fn; return this; }, fail(fn) { saveFailed = fn; return this; }});
const beforeSave = requests.length;
model.detectM495();
assert.equal(model.busy(), true);
assert.equal(requests.length, beforeSave, "Detection waits for settings persistence");
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState, busy: false});
assert.equal(model.busy(), true, "An idle status message cannot unlock an outstanding settings save");
assert.equal(model.available(), false);
model.detectFirmware();
assert.equal(requests.length, beforeSave, "A second detection cannot overtake settings persistence");
saved();
assert.equal(requests.at(-1).command, "detect_m495");
model.busy(false);
model.detectFirmware();
const beforeFailedSave = requests.length;
saveFailed({responseJSON: {error: "Settings save failed"}});
assert.equal(requests.length, beforeFailedSave, "A failed save must not start a probe");
assert.equal(model.error(), "Settings save failed");
assert.equal(model.errorToastOpen(), true, "Request failures open an error popup");
model.dismissError();
assert.equal(model.errorToastOpen(), false);
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState, error: "Settings save failed"});
assert.equal(model.errorToastOpen(), false, "Repeated status updates do not reopen a dismissed error");
model.openError();
assert.equal(model.errorToastOpen(), true, "Dismissed errors remain accessible");
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState, error: "Sensor CLIPPING"});
assert.equal(model.errorToastOpen(), true, "A different failure opens a fresh notification");
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState, error: null});
assert.equal(model.errorToastOpen(), false);
model.operation("run_resonance_test");
model.progressPhase(0);
for (const [stage, phase] of [["waiting for Marlin homing / positioning", 1], ["starting capture at ARMED_WAIT", 2],
    ["arming native sweep", 2], ["measuring native resonance", 3], ["analyzing native resonance", 4]]) {
    model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState, busy: true, stage});
    assert.equal(model.progressPhase(), phase);
    assert.equal(model.progressSteps()[phase].current, true);
    assert.equal(model.progressSteps().some(step => step.failed), false);
}
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState, busy: true, stage: "preparing"});
assert.equal(model.progressPhase(), 4, "A delayed stage cannot rewind the visible progress");
model.progressPhase(1);
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState, busy: true,
    stage: "verifying printer settings", error: "Sensor startup failed"});
assert.equal(model.progressPhase(), 1, "Error cleanup must not mark unperformed excitation stages as complete");
assert.deepEqual(Array.from(model.progressSteps(), step => step.failed), [false, true, false, false, false],
    "Only the interrupted stage is marked as failed");
assert.equal(model.progressSteps()[0].done, true, "Completed stages keep their successful status");
assert.equal(model.progressSteps()[1].status, "Interrupted");
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState,
    busy: false, stage: "idle", error: "Sensor startup failed"});
assert.equal(model.progressSteps()[1].failed, true, "The failed marker survives the end of cleanup");
model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState, error: null});
assert.equal(model.progressSteps().some(step => step.failed), false, "Clearing an error resets failure markers");
for (const axis of ["X", "Y"]) {
    model.operation("run_axis_test");
    model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "completed", message: axis + " axis test completed."});
    assert.equal(model.summary(), axis + " axis test completed.", "Each axis completion remains visible to the user");
}
assert.doesNotMatch(tab, /Printer Connected|Test Configuration/);
assert.match(tab, /pinput-error-toast/);
assert.match(tab, /role="tablist"/);
context.OctoPrint.settings.save = save;

(async function () {
    const pending = [];
    context.window = {fetch: (url, options) => {
        assert.equal(options.credentials, "same-origin");
        return new Promise(resolve => pending.push({url, resolve}));
    }};
    model.busy(false);
    const nativeResult = {...result, backend: "marlin_m495", files: {...result.files, events: "protocol_events.json"}};
    model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result: nativeResult});
    assert.equal(pending.length, 1);
    assert.match(pending[0].url, /aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\/protocol_events.json$/);
    pending[0].resolve({ok: true, json: () => Promise.resolve([
        {host_time: 100, event: {kind: "HOMED"}},
        {host_time: 101.2, event: {kind: "ARMED_WAIT"}},
        {host_time: 102, event: {kind: "START"}},
        {host_time: 140, event: {kind: "COMPLETE"}}
    ])});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(model.protocolEvents().length, 4);
    assert.equal(model.protocolEvents()[0].elapsed, "0.00");
    assert.equal(model.protocolEvents()[2].elapsed, "2.00");
    assert.match(model.protocolEvents()[2].description, /excitation started/);
    assert.equal(model.protocolLoading(), false);
    model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result: nativeResult});
    assert.equal(pending.length, 1, "Repeated results do not refetch the protocol history");
    model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result: {...nativeResult, session_id: "d".repeat(32)}});
    model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result: {...nativeResult, session_id: "e".repeat(32)}});
    pending[2].resolve({ok: true, json: () => Promise.resolve([{host_time: 200, event: {kind: "ABORTED"}}])});
    await new Promise(resolve => setImmediate(resolve));
    pending[1].resolve({ok: true, json: () => Promise.resolve([{host_time: 100, event: {kind: "START"}}])});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(model.protocolEvents()[0].kind, "ABORTED", "Stale session downloads cannot replace current events");
    model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "results_ready", result: {...nativeResult, session_id: "f".repeat(32)}});
    model.onDataUpdaterPluginMessage("Pinput_Shaping", {type: "state", ...currentState, diagnostics: null});
    pending[3].resolve({ok: true, json: () => Promise.resolve([{host_time: 100, event: {kind: "START"}}])});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(model.protocolEvents().length, 0, "Disconnect or result invalidation clears pending history");
    const palette = {}, relayouts = [];
    let host = {background: "rgb(255, 255, 255)", text: "rgb(51, 51, 51)"};
    const page = {parentElement: null};
    const parent = {parentElement: page};
    const root = {parentElement: parent, style: {setProperty: (key, value) => { palette[key] = value; }}};
    const chart = {data: [{x: [1, 2], y: [3, 4]}]};
    context.document = {getElementById: id => id === "pinput_shaping_tab" ? root : chart};
    context.window.getComputedStyle = element => element === root ?
        {getPropertyValue: key => palette[key] || ""} :
        {color: host.text, backgroundColor: element === parent ? "rgba(0, 0, 0, 0)" : host.background};
    context.Plotly = {relayout: (element, patch) => { relayouts.push(patch); }};
    const beforeTheme = requests.length;
    for (const theme of [
        {background: "rgb(255, 255, 255)", text: "rgb(51, 51, 51)", scheme: "light"},
        {background: "rgb(20, 20, 20)", text: "rgb(232, 232, 232)", scheme: "dark"},
        {background: "rgb(244, 239, 231)", text: "rgb(61, 48, 39)", scheme: "light"},
        {background: "rgb(41, 36, 52)", text: "rgb(236, 231, 244)", scheme: "dark"}
    ]) {
        host = theme;
        model.syncTheme();
        assert.equal(palette["--pinput-background"], theme.background, "Use the nearest painted OctoPrint ancestor");
        assert.equal(palette["--pinput-text"], theme.text);
        assert.equal(root.style.colorScheme, theme.scheme);
        assert.equal(relayouts.at(-1)["font.color"], theme.text);
        assert.equal(relayouts.at(-1).plot_bgcolor, "rgba(0,0,0,0)", "Plots reveal the host theme's background");
        const count = relayouts.length;
        model.syncTheme();
        assert.equal(relayouts.length, count, "An unchanged theme does not redraw plots");
    }
    assert.equal(requests.length, beforeTheme, "Theme changes do not send printer commands");
    assert.deepEqual(chart.data, [{x: [1, 2], y: [3, 4]}], "Theming preserves plotted measurements");
    let observeTheme, stylesheetLoaded;
    root.contains = target => target === root;
    context.document.documentElement = {};
    context.document.head = {contains: () => false};
    context.document.addEventListener = (event, callback) => { if (event === "load") stylesheetLoaded = callback; };
    context.MutationObserver = function (callback) { observeTheme = callback; this.observe = () => {}; };
    context.window.requestAnimationFrame = callback => callback();
    currentState = {busy: false, firmware: ftm};
    model.onStartupComplete();
    host = {background: "rgb(0, 0, 0)", text: "rgb(255, 255, 255)"};
    observeTheme([{type: "attributes", target: page}]);
    assert.equal(palette["--pinput-background"], host.background, "A host theme change updates automatically");
    const themedCount = relayouts.length;
    observeTheme([{type: "attributes", target: root}]);
    assert.equal(relayouts.length, themedCount, "Plugin style changes cannot trigger a redraw loop");
    host = {background: "rgb(255, 255, 255)", text: "rgb(51, 51, 51)"};
    stylesheetLoaded({target: {tagName: "LINK"}});
    assert.equal(palette["--pinput-text"], host.text, "Newly loaded theme stylesheets are observed too");
    assert.equal(requests.length, beforeTheme);
    console.log("Host themes, plots, calibration views, error popups and existing frontend checks passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
