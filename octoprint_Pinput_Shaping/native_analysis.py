"""Signal-based log-sweep isolation; host timestamps are never sample clocks."""

import json
import math
from pathlib import Path

import numpy as np
from scipy.ndimage import map_coordinates, median_filter
from scipy.signal import find_peaks, stft

from .firmware import finite_number
from .inputshaping_analyzer import InputShapingAnalyzer, MeasurementError
from .lis2dw import scale_metadata


def capture_metadata(path, sensor, rate, samples=None, requested_range=2, range_required=False):
    sidecar = Path(str(path) + ".meta.json")
    if not sidecar.exists():
        if sensor == "lis2dw":
            raise MeasurementError("Native LIS2DW capture requires valid protocol-2 metadata")
        return {"present": False}
    if sidecar.stat().st_size > 65536:
        raise MeasurementError("Sensor metadata exceeds its size limit")
    try:
        with sidecar.open(encoding="utf-8") as stream:
            metadata = json.load(stream)
    except (OSError, ValueError) as exc:
        raise MeasurementError("Sensor metadata is unreadable or invalid") from exc
    if not isinstance(metadata, dict):
        raise MeasurementError("Sensor metadata must be an object")
    if (metadata.get("protocol") != 2 or metadata.get("valid") is not True
            or metadata.get("requested_rate") != rate or metadata.get("units") != "g"
            or (sensor != "lis2dw" and metadata.get("counts_per_g") != 16384)
            or metadata.get("clock") != "rp2040_fifo_poll"):
        raise MeasurementError("Sensor protocol-2 metadata is invalid or incompatible")
    if sensor == "lis2dw":
        try:
            metadata.update(scale_metadata(metadata, requested_range, required=range_required))
        except ValueError as exc:
            raise MeasurementError(str(exc)) from exc
    count = metadata.get("samples")
    if isinstance(count, bool) or not isinstance(count, int) or count < 16 or (samples is not None and count != samples):
        raise MeasurementError("CSV sample count does not match sensor metadata")
    finite_number(metadata.get("effective_rate"), "metadata sampling rate", 1, rate * 1.1)
    finite_number(metadata.get("max_window_us"), "sensor observation window", 0, 1000000 / rate / 4)
    return metadata


class NativeSweepAnalyzer(InputShapingAnalyzer):
    """Relative fundamental sensitivity under a locally linear log sweep.

    Marlin's fixed shape has displacement proportional to 1/f; its fundamental
    acceleration is proportional to f. Divide fundamental output power by a
    unit-gain f-proportional chirp processed through the same STFT. This also
    corrects the finite-window frequency response of the moving chirp.
    Unknown constant gain (including Marlin C) cancels in the relative response.
    This is not a calibrated absolute transfer function or a raw output PSD.
    """
    def __init__(self, *args, native_parameters, host_interval, sensor_metadata=None, capture_interval=None,
                 lis2dw_range=None, **kwargs):
        self.parameters = native_parameters
        self.host_interval = host_interval
        self.sensor_metadata = sensor_metadata
        self.capture_interval = capture_interval
        self.lis2dw_range = lis2dw_range
        kwargs.update(min_freq=5, max_freq=max(21, native_parameters.end))
        super().__init__(*args, **kwargs)
        self.min_freq, self.max_freq = native_parameters.start, native_parameters.end
        self.reference_label = "Relative fundamental response"
        self.spectrum_label = "Relative vibration sensitivity (power)"
        self.recommendation_valid = False

    def analyze(self):
        self.load_data()
        if self.capture_interval is not None and self.quality["duration"] < self.capture_interval * 0.98 - 1:
            raise MeasurementError("Sensor duration is incomplete relative to observed capture readiness/stop")
        metadata = self.sensor_metadata
        if self.lis2dw_range is not None:
            try:
                scale = scale_metadata(metadata, self.lis2dw_range)
            except ValueError as exc:
                raise MeasurementError(str(exc)) from exc
            self.quality["accelerometer"] = {**scale,
                "range_negotiated": metadata.get("range_negotiated", scale["range_negotiated"])}
        if metadata and metadata.get("present") is not False:
            if metadata["samples"] != self.quality["samples"]:
                raise MeasurementError("CSV sample count does not match sensor metadata")
            if abs(metadata["effective_rate"] / self.sampling_rate - 1) > 0.005:
                raise MeasurementError("Metadata sampling rate does not match captured timestamps")
        p = self.parameters
        duration = p.duration
        if self.host_interval < duration * 0.85 or self.host_interval > duration * 1.15 + 3:
            raise MeasurementError("Native lifecycle interval is inconsistent with sweep coverage")
        if self.time[-1] < duration:
            raise MeasurementError("Capture does not cover the complete native sweep duration")
        channels = range(3) if self.sensor_axis == "sum" else ["xyz".index(self.sensor_axis)]
        window = max(256, min(2048, int(round(self.sampling_rate))))
        hop = max(1, window // 10)
        power = None
        for channel in channels:
            frequencies, times, transform = stft(self.raw_axes[:, channel], fs=self.sampling_rate,
                nperseg=window, noverlap=window - hop, boundary=None, detrend="constant")
            channel_power = abs(transform) ** 2
            if power is None:
                power = channel_power
            else:
                power += channel_power
            del transform, channel_power
        grid = np.geomspace(p.start, p.end, 160)
        relative = p.octave * np.log2(grid / p.start)
        fstep, tstep = frequencies[1] - frequencies[0], times[1] - times[0]
        # Search only in the device sample timebase. Lifecycle times constrain
        # duration, not an assumed host-to-sensor offset.
        offsets = np.arange(0, self.time[-1] - duration + 0.051, 0.05)
        if len(offsets) > 10000:
            raise MeasurementError("Capture is too long for bounded native alignment")
        floor = np.maximum(np.median(power[(frequencies >= p.start) & (frequencies <= p.end)], axis=0), 1e-20)
        # Persistent spectral lines are background, not evidence of a mechanical
        # response to the swept fundamental. A median across device times also
        # prevents a stationary tone from winning the alignment search.
        stationary = np.median(power, axis=1)
        stationary_track = np.interp(grid, frequencies, stationary)
        interior = (relative >= 0.75) & (relative <= duration - 0.75)
        if np.count_nonzero(interior) < 40:
            raise MeasurementError("Native sweep is too narrow for reliable frequency alignment")
        candidates = []
        for scale in (0.98, 0.99, 1.0, 1.01, 1.02):
            coordinates = (offsets[:, None] + scale * relative - times[0]) / tstep
            tracks = map_coordinates(power, [np.broadcast_to(grid / fstep, coordinates.shape), coordinates],
                                     order=1, mode="constant", cval=0)
            background = np.interp((offsets[:, None] + scale * relative).ravel(), times, floor).reshape(coordinates.shape)
            background = np.maximum(background, stationary_track)
            snr = tracks / np.maximum(background, 1e-20)
            scores = np.mean(np.log1p(snr[:, interior]), axis=1)
            for index in range(len(offsets)):
                candidates.append((float(scores[index]), float(offsets[index]), scale))
        candidates.sort(reverse=True)
        best_score, offset, scale = candidates[0]
        competitors = [item for item in candidates if abs(item[1] - offset) > max(2, duration * 0.10)]
        if competitors and competitors[0][0] >= best_score - 0.25:
            raise MeasurementError("Ambiguous native sweep alignment; multiple intervals fit")
        near = [item[1] for item in candidates if item[0] >= best_score - 0.05]
        uncertainty = max(window / self.sampling_rate / 2, (max(near) - min(near)) / 2)
        if uncertainty > 1.5:
            raise MeasurementError("Native sweep boundary uncertainty is too large")
        sample_times = offset + scale * relative
        coordinates = [grid / fstep, (sample_times - times[0]) / tstep]
        fundamental = map_coordinates(power, coordinates, order=1, mode="constant", cval=0)
        noise = np.maximum(np.interp(sample_times, times, floor), stationary_track)
        supported = fundamental > np.maximum(noise * 6, 1e-18)
        coverage = float(np.mean(supported[interior]))
        # Require both ends as well as overall tracking. This prevents a strong
        # harmonic covering only the upper portion from defining the alignment.
        if (coverage < 0.85 or np.mean(supported[5:25]) < 0.7
                or np.mean(supported[-25:-5]) < 0.7):
            raise MeasurementError("Insufficient native fundamental coverage or signal-to-noise")
        selected = (self.time >= offset + uncertainty) & (self.time <= offset + scale * duration - uncertainty)
        if np.count_nonzero(selected) < 512:
            raise MeasurementError("Insufficient isolated excitation samples")
        excited = self.raw_axes[selected]
        block_size = max(16, int(self.sampling_rate))
        for block_start in range(0, len(excited) - block_size + 1, block_size):
            for column in excited[block_start:block_start + block_size].T:
                if np.std(column) > 0.05:
                    plateau = (column == np.max(column)) | (column == np.min(column))
                    edges = np.flatnonzero(np.diff(np.r_[False, plateau, False]))
                    if np.mean(plateau) > 0.20 and np.max(edges[1::2] - edges[::2]) >= 4:
                        raise MeasurementError("Native excitation contains clipped or stale acceleration samples")
        relative_time = (self.time - offset) / scale
        reference_frequency = p.start * 2 ** (relative_time / p.octave)
        reference_phase = (2 * np.pi * p.start * p.octave * scale / np.log(2)
                           * (2 ** (relative_time / p.octave) - 1))
        reference = np.where((relative_time >= 0) & (relative_time <= duration),
                             reference_frequency / p.start * np.sin(reference_phase), 0)
        _, _, reference_stft = stft(reference, fs=self.sampling_rate, nperseg=window,
            noverlap=window - hop, boundary=None, detrend="constant")
        input_power = map_coordinates(abs(reference_stft) ** 2, coordinates, order=1, mode="constant", cval=0)
        # No homing/positioning samples enter the time-domain result.
        self.time, self.raw_axes = self.time[selected], self.raw_axes[selected]
        self.time -= self.time[0]
        self.raw = self.raw_axes[:, "xyz".index(self.display_channel)]
        self.filtered = self.lowpass_filter(self.raw)
        trusted = ((relative >= uncertainty + window / self.sampling_rate / 2)
                   & (relative <= duration - uncertainty - window / self.sampling_rate / 2))
        grid, fundamental, noise = grid[trusted], fundamental[trusted], noise[trusted]
        self.freqs = grid
        response = np.where(supported[trusted], np.maximum(fundamental - noise, 0), 0) / np.maximum(input_power[trusted], 1e-20)
        self.psd = median_filter(response, size=5)
        self.psd /= max(float(np.median(self.psd)), 1e-20)
        self.quality.update(alignment={"method": "log-frequency fundamental tracking",
            "sensor_start": offset, "sensor_end": offset + scale * duration,
            "uncertainty_seconds": uncertainty, "clock_scale": scale,
            "host_sample_mapping": "not assumed"}, coverage=coverage,
            representation="relative fundamental sensitivity power; not output PSD",
            normalization="f-proportional fundamental input power with identical STFT window",
            background="maximum of local spectral median and persistent per-frequency median")
        band = (grid >= max(20, p.start)) & (grid <= p.end)
        f, response = grid[band], self.psd[band]
        peaks, properties = find_peaks(response, prominence=max(2, float(np.max(response, initial=0)) * 0.1))
        peaks = [int(i) for i in peaks if 3 <= i < len(f) - 3 and response[i] >= 3]
        self.quality["resonances"] = [float(f[i]) for i in peaks]
        self.warnings.append("Native response assumes locally linear mechanics and frequency-proportional input acceleration; absolute gain is not calibrated.")
        self.warnings.append("Predicted residual is relative sensitivity within the measured band, not a measured whole-machine vibration reduction.")
        strongest = int(np.argmax(response)) if len(response) else 0
        if len(response) and response[strongest] >= 3 and (strongest < 3 or strongest >= len(response) - 3):
            self.warnings.append("Strong response at the measured boundary; widen the sweep. Recommendations are disabled.")
            return None
        if not peaks:
            self.warnings.append("No confident interior resonance; recommendations are disabled.")
            return None
        dominant = max(peaks, key=lambda i: response[i])
        self.base_freq = float(f[dominant])
        self.quality["resonance_candidates"] = [{"frequency": float(f[i]), "relative_power": float(response[i]),
                                                 "uncertainty_hz": max(fstep, float(f[i]) * math.log(2) / p.octave * uncertainty),
                                                 "confidence": "signal and coverage gates passed"} for i in peaks]
        self.quality["optimizer_band"] = [float(f[0]), float(f[-1])]
        self.fit_spectrum(f, response, search_min=max(20, math.ceil(f[0])), search_max=f[-1])
        self.recommendation_valid = True
        return self.best_shaper
