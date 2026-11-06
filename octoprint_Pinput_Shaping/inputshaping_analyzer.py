"""Quality-gated resonance analysis matching c13 OctoPrint FTM shapers.

Marlin/src/module/ft_motion/shaping.cpp defines coefficients and tap delays.
Predictions describe linear suppression, not measured print quality or a
certified mechanical acceleration limit.
"""

import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import butter, find_peaks, sosfiltfilt, welch

from .firmware import SHAPER_IDS, finite_number, shaping_command


class MeasurementError(ValueError):
    """The capture does not support a reliable recommendation."""


class InputShapingAnalyzer:
    def __init__(self, save_dir, csv_path, damping=0.05, cutoff_freq=None,
                 axis="X", min_accel=300, max_accel=2500, min_freq=20,
                 max_freq=70, logger=None, firmware_mode="ftm", tolerance=0.05,
                 shaping_rate=1000, sensor_axis="sum", max_smoothing=0.12):
        self.result_dir = Path(save_dir)
        self.csv_path = Path(csv_path)
        self.axis = axis
        self.damping = finite_number(damping, "damping ratio", 0.01, 0.25)
        self.min_accel = finite_number(min_accel, "minimum acceleration", 1)
        self.max_accel = finite_number(max_accel, "maximum acceleration", self.min_accel)
        self.min_freq = max(20, finite_number(min_freq, "analysis start frequency", 5, 199))
        self.max_freq = finite_number(max_freq, "analysis end frequency", self.min_freq + 1, 200)
        self.tolerance = finite_number(tolerance, "vibration tolerance", 0.001, 0.5)
        self.max_smoothing = finite_number(max_smoothing, "smoothing displacement", 0.01, 1)
        self.firmware_mode = firmware_mode
        self.shaping_rate = shaping_rate if firmware_mode == "ftm" else None
        if sensor_axis not in ("x", "y", "z", "sum"):
            raise ValueError("Sensor axis must be x, y, z, or sum")
        self.sensor_axis = sensor_axis
        self.cutoff_freq = cutoff_freq
        self._plugin_logger = logger or logging.getLogger(__name__)
        self.shaper_results = {}
        self.best_shaper = None
        self.base_freq = None
        self.quality = {}
        self.warnings = []

    def load_data(self):
        if self.csv_path.stat().st_size > 256 * 1024 * 1024:
            raise MeasurementError("Capture exceeds the 256 MiB processing limit")
        frame = pd.read_csv(self.csv_path)
        frame.columns = [str(c).strip().lower() for c in frame.columns]
        if not {"time", "x", "y", "z"}.issubset(frame.columns):
            raise MeasurementError("CSV must contain time,x,y,z columns")
        data = frame[["time", "x", "y", "z"]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
        if len(data) < 512 or len(data) > 2_000_000:
            raise MeasurementError("Capture must contain between 512 and 2,000,000 samples")
        if not np.all(np.isfinite(data)):
            raise MeasurementError("Capture contains missing, non-numeric or infinite samples")
        dt = np.diff(data[:, 0])
        if np.any(dt <= 0):
            raise MeasurementError("Sample times must increase strictly; duplicate/rounded timestamps are not reliable")
        duration = data[-1, 0] - data[0, 0]
        if duration < 2:
            raise MeasurementError("Capture is too short; at least two seconds are required")
        median = float(np.median(dt))
        jitter = float(np.std(dt) / median)
        if float(np.max(dt)) > 3 * median or jitter > 0.15:
            raise MeasurementError("Capture has excessive timestamp jitter or missing sample intervals")
        self.sampling_rate = (len(data) - 1) / duration
        if self.sampling_rate < 2.5 * self.max_freq:
            raise MeasurementError("Effective sampling rate is too low for the requested frequency band")
        self.time = data[:, 0] - data[0, 0]
        self.raw_axes = data[:, 1:]
        if jitter > 0.01:
            uniform = np.linspace(0, duration, len(data))
            self.raw_axes = np.column_stack([np.interp(uniform, self.time, self.raw_axes[:, i]) for i in range(3)])
            self.time = uniform
            self.warnings.append("Small acquisition jitter was resampled onto a uniform time grid.")
        dynamic = self.raw_axes - np.mean(self.raw_axes, axis=0)
        if np.max(np.std(dynamic, axis=0)) < 1e-7:
            raise MeasurementError("Capture contains no measurable dynamic acceleration")
        for column in dynamic.T:
            if np.std(column) > 0.05:
                plateau = (column == np.max(column)) | (column == np.min(column))
                edges = np.flatnonzero(np.diff(np.r_[False, plateau, False]))
                longest = np.max(edges[1::2] - edges[::2])
                if longest >= 4 and np.mean(plateau) > 0.20:
                    raise MeasurementError("Repeated acceleration extrema suggest sensor clipping or stale samples")
        channel = int(np.argmax(np.var(dynamic, axis=0))) if self.sensor_axis == "sum" else "xyz".index(self.sensor_axis)
        self.raw = self.raw_axes[:, channel]
        self.display_channel = "xyz"[channel]
        self.quality.update(samples=len(data), duration=duration, sampling_rate=self.sampling_rate,
                            timestamp_jitter=jitter, sensor_axis=self.sensor_axis)

    def compute_psd(self, signal):
        # One-second windows keep spectral resolution independent of total
        # capture duration, without a stride-one memory estimate.
        desired = 2 ** int(math.ceil(math.log2(self.sampling_rate)))
        available = 2 ** int(math.floor(math.log2(len(signal) // 2)))
        nperseg = max(64, min(desired, available, 8192))
        return welch(np.asarray(signal, dtype=np.float64), fs=self.sampling_rate,
                     nperseg=nperseg, noverlap=nperseg // 2, detrend="constant")

    def lowpass_filter(self, data, order=4):
        # Filtering affects only the time-domain preview. Candidate and
        # reference spectra both derive from the same raw samples.
        cutoff = min(self.cutoff_freq or self.max_freq * 1.5, self.sampling_rate * 0.4)
        return sosfiltfilt(butter(order, cutoff, fs=self.sampling_rate, output="sos"), data)

    def generate_shapers(self, freq):
        freq = finite_number(freq, "shaper frequency", 1)
        df = math.sqrt(1 - self.damping ** 2)
        k = math.exp(-self.damping * math.pi / df)
        km = k ** 0.75
        v = self.tolerance
        x = (v * v * (math.sqrt(1 - v * v) + 1)) ** (1 / 3)
        a2h = (3 * x * x + 2 * x + 3 * v * v) / (16 * x)
        a3h = (1 + 3 * v + 2 * math.sqrt(2 * (v + 1) * v)) / 16
        coefficients = {
            "ZV": [1, k],
            "ZVD": [1, 2 * k, k * k],
            "ZVDD": [1, 3 * k, 3 * k * k, k ** 3],
            "ZVDDD": [1, 4 * k, 6 * k * k, 4 * k ** 3, k ** 4],
            "EI": [(1 + v) / 4, (1 - v) * k / 2, (1 + v) * k * k / 4],
            "2HUMP_EI": [a2h, (0.5 - a2h) * k, (0.5 - a2h) * k * k, a2h * k ** 3],
            "3HUMP_EI": [a3h, (1 - v) * k / 4, ((1 + v) / 2 - 2 * a3h) * k * k,
                         (1 - v) * k ** 3 / 4, a3h * k ** 4],
            # Updated Marlin uses K^0.75 for impulses spaced 0.375 periods apart.
            "MZV": [1, math.sqrt(2) * km, km * km],
        }
        result = {}
        for name, gains in coefficients.items():
            spacing = (0.375 if name == "MZV" else 0.5) / (freq * df)
            if self.shaping_rate:
                # Marlin rounds tap spacing once, then multiplies that index.
                spacing = math.floor(spacing * self.shaping_rate + 0.5) / self.shaping_rate
            total = sum(gains)
            result[name] = [(i * spacing, gain / total) for i, gain in enumerate(gains)]
        return result

    def response(self, shaper, frequencies, damping=None):
        frequencies = np.atleast_1d(frequencies)
        times, gains = np.asarray(shaper, dtype=float).T
        zeta = self.damping if damping is None else damping
        omega = 2 * np.pi * frequencies[:, None]
        decay = np.exp(-zeta * omega * (times[-1] - times))
        phase = omega * math.sqrt(1 - zeta * zeta) * times
        return np.abs(np.sum(gains * decay * np.exp(1j * phase), axis=1))

    def _calculate_residual_vibration(self, shaper, base_freq):
        return float(self.response(shaper, [base_freq])[0])

    def _variance(self, shaper):
        times, gains = np.asarray(shaper).T
        centroid = np.sum(times * gains)
        return float(np.sum(gains * (times - centroid) ** 2))

    def analyze(self):
        self.load_data()
        self.filtered = self.lowpass_filter(self.raw)
        channels = range(3) if self.sensor_axis == "sum" else ["xyz".index(self.sensor_axis)]
        spectra = [self.compute_psd(self.raw_axes[:, i]) for i in channels]
        self.freqs = spectra[0][0]
        self.psd = np.sum([item[1] for item in spectra], axis=0)
        band = (self.freqs >= self.min_freq) & (self.freqs <= self.max_freq)
        f, p = self.freqs[band], self.psd[band]
        if len(f) < 8:
            raise MeasurementError("Insufficient spectral resolution in the requested band")
        noise = max(float(np.median(p)), np.finfo(float).tiny)
        strongest = int(np.argmax(p))
        if strongest < 2 or strongest >= len(f) - 2:
            raise MeasurementError("Strongest spectral energy lies at the sweep boundary; widen the measured range")
        peaks, props = find_peaks(p, prominence=max(noise * 4, float(np.max(p)) * 0.02))
        if len(peaks) == 0 or float(np.max(p)) / noise < 6:
            raise MeasurementError("No clear resonance above the spectral noise floor")
        dominant = peaks[int(np.argmax(props["prominences"]))]
        self.base_freq = float(f[dominant])
        self.quality.update(peak_to_noise=float(p[dominant] / noise), resolution=float(f[1] - f[0]),
                            resonances=[float(f[i]) for i in peaks])
        if dominant < 2 or dominant >= len(f) - 2:
            raise MeasurementError("Dominant resonance lies at the sweep boundary; widen the measured range")
        return self.fit_spectrum(f, p)

    def fit_spectrum(self, f, p, search_min=20, search_max=200):
        """Fit a validated nonnegative vibration-sensitivity power spectrum."""
        integrate = getattr(np, "trapezoid", None) or np.trapz
        reference = float(integrate(p, f))
        if reference <= 0 or not math.isfinite(reference):
            raise MeasurementError("Invalid reference vibration energy")
        names = SHAPER_IDS if self.firmware_mode == "ftm" else {"ZV": 1}
        tested_damping = (max(0.01, self.damping * 0.75), self.damping, min(0.25, self.damping * 1.5))
        search = np.arange(search_min, min(search_max, self.sampling_rate * 0.4) + 0.001, 0.5)
        if not len(search):
            raise MeasurementError("No supported shaper frequency inside the measured band")
        for name in names:
            best = None
            for frequency in search:
                shaper = self.generate_shapers(float(frequency))[name]
                response = np.maximum.reduce([self.response(shaper, f, damping=z) for z in tested_damping])
                remaining = float(integrate(p * response ** 2, f) / reference)
                variance = self._variance(shaper)
                smoothing = 0.5 * self.max_accel * variance
                score = remaining + 0.05 * smoothing / self.max_smoothing
                if best is None or score < best[0]:
                    best = (score, float(frequency), remaining, shaper, smoothing, variance)
            score, frequency, remaining, shaper, smoothing, variance = best
            suggested = min(self.max_accel, 2 * self.max_smoothing / max(variance, 1e-12))
            self.shaper_results[name] = {
                "psd": self.psd * self.response(shaper, self.freqs) ** 2,
                "vibr": remaining, "residual_vibr": remaining,
                "shaper_freq": frequency, "shaper_duration": shaper[-1][0],
                "shaper_duration_ms": shaper[-1][0] * 1000,
                "smoothing": smoothing, "suggested_accel": int(suggested), "score": score,
            }
        self.best_shaper = min(self.shaper_results, key=lambda name: self.shaper_results[name]["score"])
        if self.shaper_results[self.best_shaper]["smoothing"] > self.max_smoothing:
            self.warnings.append("Predicted smoothing exceeds the configured displacement limit.")
        self.warnings.append("Acceleration limits are theoretical smoothing estimates, capped by the test limit; verify print quality separately.")
        return self.best_shaper

    def get_recommendation(self):
        return shaping_command(self.firmware_mode, self.axis, self.best_shaper,
                               self.shaper_results[self.best_shaper]["shaper_freq"],
                               self.damping, self.tolerance)

    def generate_graphs(self):
        # Figure objects avoid pyplot's shared figure registry in a server.
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        self.result_dir.mkdir(parents=True, exist_ok=True)
        signal_path = self.result_dir / "signal.png"
        psd_path = self.result_dir / "psd.png"
        stride = max(1, len(self.time) // 3000)
        figure = Figure(figsize=(12, 4), constrained_layout=True)
        FigureCanvasAgg(figure)
        ax = figure.subplots()
        ax.plot(self.time[::stride], self.raw[::stride], alpha=0.5, label=f"Raw sensor {self.display_channel.upper()}")
        ax.plot(self.time[::stride], self.filtered[::stride], label="Preview filter")
        ax.set(xlabel="Time (s)", ylabel="Acceleration (sensor units)", title=f"{self.axis} excitation capture")
        ax.legend()
        figure.savefig(signal_path, dpi=120)
        figure = Figure(figsize=(12, 6), constrained_layout=True)
        FigureCanvasAgg(figure)
        ax = figure.subplots()
        ax.plot(self.freqs, self.psd, color="black", label=getattr(self, "reference_label", "Unshaped reference"))
        for name, item in self.shaper_results.items():
            ax.plot(self.freqs, item["psd"], label=f"{name} @ {item['shaper_freq']:.1f} Hz (predicted)")
        title = f"{self.axis}: measured resonance {self.base_freq:.1f} Hz" if self.base_freq is not None else f"{self.axis}: native sweep diagnostics"
        ax.set(xlabel="Frequency (Hz)", ylabel=getattr(self, "spectrum_label", "PSD (sensor units²/Hz)"),
               xlim=(self.min_freq, self.max_freq), title=title)
        ax.legend(fontsize=8)
        figure.savefig(psd_path, dpi=120)
        return str(signal_path), str(psd_path), self.shaper_results, self.best_shaper, self.base_freq

    def get_plotly_data(self):
        stride = max(1, len(self.time) // 2000)
        return {"time": self.time[::stride].tolist(), "raw": self.raw[::stride].tolist(),
                "filtered": self.filtered[::stride].tolist(), "freqs": self.freqs.tolist(),
                "psd_original": self.psd.tolist(),
                "shapers": {name: {**{k: v for k, v in item.items() if k != "psd"},
                                   "psd": item["psd"].tolist()} for name, item in self.shaper_results.items()},
                "base_freq": self.base_freq, "best_shaper": self.best_shaper,
                "best_shaper_freq": self.shaper_results[self.best_shaper]["shaper_freq"] if self.best_shaper else None,
                "spectrum_label": getattr(self, "spectrum_label", "PSD (sensor units²/Hz)")}
