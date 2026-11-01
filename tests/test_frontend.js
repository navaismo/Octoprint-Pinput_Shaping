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
for (const key of ["accelMin", "accelMax", "freqStart", "freqEnd", "freqStep", "bandDuration", "dampingRatio", "sensorType", "sensorAxis", "sensorPort"]) {
    settings[key] = observable(1);
}
const Model = context.OCTOPRINT_VIEWMODELS[0].construct;
const model = new Model([{settings: {plugins: {Pinput_Shaping: settings}}},
    {isOperational: () => true, isPrinting: () => false}]);
model.onStartupComplete();
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
assert.equal(model.canApply(), true, "Valid results work without Plotly");
assert.equal(model.selectedShaper(), "ZV", "New results default to the recommendation");
assert.equal(model.canSelectShaper(), true);
model.selectedShaper("MZV");
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
console.log("Firmware gating, prefix-aware downloads, atomic results and server-side application checks passed.");
