import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt, welch, savgol_filter
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import os
import logging
import sys

MAX_BYTES_32 = 2_000_000_000           # ~ 2 gi b

class InputShapingAnalyzer:
    def __init__(self, save_dir, csv_path, damping=0.5, cutoff_freq=100, axis=None, logger = None):
        
        self._plugin_logger = logger or logging.getLogger("octoprint.plugins.Pinput_Shaping")
        self.csv_path = csv_path
        self.damping = damping
        self.cutoff_freq = cutoff_freq
        self.axis = axis.upper()
        self.result_dir = save_dir
        self.best_shaper = None
        self.base_freq = None
        self.shaper_results = {}
    

    def load_data(self):
        self._plugin_logger.info(f"Loading data from CSV file {self.csv_path} for axis {self.axis}")
        df = pd.read_csv(self.csv_path)
        df.columns = [c.strip().lower() for c in df.columns]     

        # Time
        df["time"] = pd.to_numeric(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])

        # selected axis
        axis_col = self.axis.lower()          # "x" o "y" o "z"
        if axis_col not in df.columns:
            raise ValueError(f"Column '{axis_col}' not found in CSV")

        df[axis_col] = pd.to_numeric(df[axis_col], errors="coerce")
        df = df.dropna(subset=[axis_col])

        self.time = df["time"].to_numpy(dtype=np.float64)
        self.raw  = df[axis_col].to_numpy(dtype=np.float64)

        self.sampling_rate = 1.0 / np.mean(np.diff(self.time))


    def lowpass_filter(self, data, order=4):
        nyq = 0.5 * self.sampling_rate
        norm_cutoff = self.cutoff_freq / nyq
        b, a = butter(order, norm_cutoff, btype='low')
        return filtfilt(b, a, data)

    def generate_shapers(self, freq):
        t = 1 / freq
        K = np.exp(-self.damping * np.pi / np.sqrt(1 - self.damping**2))
        shapers = {}

        # Zero Vibration (ZV)
        shapers["ZV"] = [
            (0, 1 / (1 + K)),
            (t, K / (1 + K))
        ]

        # Modified ZV (MZV)
        shapers["MZV"] = [
            (0, 1 / (1 + K + K**2)),
            (t, K / (1 + K + K**2)),
            (2 * t, K**2 / (1 + K + K**2))
        ]

        # Extra Insensitive (EI)
        shapers["EI"] = [
            (0, 1 / (1 + 3*K + 3*K**2 + K**3)),
            (t, 3*K / (1 + 3*K + 3*K**2 + K**3)),
            (2 * t, 3*K**2 / (1 + 3*K + 3*K**2 + K**3)),
            (3 * t, K**3 / (1 + 3*K + 3*K**2 + K**3))
        ]

        # 2-Hump EI
        shapers["2HUMP_EI"] = [
            (0,     1 / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
            (t,     4*K / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
            (2*t,   6*K**2 / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
            (3*t,   4*K**3 / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
            (4*t,   K**4 / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
        ]

        # 3-Hump EI
        shapers["3HUMP_EI"] = [
            (0,     1 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (t,     6*K / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (2*t,   15*K**2 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (3*t,   20*K**3 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (4*t,   15*K**4 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (5*t,   6*K**5 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (6*t,   K**6 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
        ]

        return shapers
    def apply_shaper(self, signal, time, shaper):
        dt = np.mean(np.diff(time))
        n = len(signal)
        shaped = np.zeros(n)
        for delay, amp in shaper:
            shift = int(np.round(delay / dt))
            if shift < n:
                shaped[shift:] += amp * signal[:n - shift]
        return shaped

    def compute_psd(self, signal: np.ndarray):
        """Welch adaptativo que garantiza no exceder el límite de 2 GiB."""
        sig = signal.astype(np.float32, copy=False)

        # starting point
        nperseg = min(4096, len(sig) // 8)
        if nperseg < 256:
            nperseg = 256

        while True:
            n_win   = len(sig) - nperseg + 1
            est_mem = n_win * nperseg * sig.itemsize
            if est_mem < MAX_BYTES_32 or nperseg <= 256:
                break
            nperseg //= 2                  # reduces half and try again

        self._plugin_logger.debug(
            f"Welch: nperseg={nperseg}, windows={n_win}, "
            f"est_mem={est_mem/1e6:.1f} MB, len={len(sig)}")

        return welch(sig, fs=self.sampling_rate, nperseg=nperseg)
    
    def analyze(self):
        self.load_data()
        self.filtered = self.lowpass_filter(self.raw)
        self.freqs, self.psd = self.compute_psd(self.filtered)

        freq_range = (self.freqs > 20) & (self.freqs < 80)
        self.base_freq = self.freqs[freq_range][np.argmax(self.psd[freq_range])]
        shapers = self.generate_shapers(self.base_freq)

        for name, shaper in shapers.items():
            shaped = self.apply_shaper(self.filtered, self.time, shaper)
            _, shaped_psd = self.compute_psd(shaped)
            vibr = np.sum(shaped_psd)
            accel = max(np.abs(np.gradient(shaped, np.mean(np.diff(self.time)))))
            self.shaper_results[name] = {
                "psd": shaped_psd,
                "vibr": vibr,
                "accel": accel
            }

        self.best_shaper = min(self.shaper_results, key=lambda s: self.shaper_results[s]["vibr"])
        return self.best_shaper

    def generate_graphs(self):
        # get the date from csv file which format is Raw_accel_values_AXIS_X_20250416T133919.csv 
        date = os.path.basename(self.csv_path).split("_")[-1].split(".")[0]
        
        # Signal Graph
        signal_path = os.path.join(self.result_dir, f"{self.axis}_signal_{date}.png")
        plt.figure(figsize=(14, 5))
        plt.plot(self.time[::50], self.raw[::50], label="Original", alpha=0.4, color="#007bff")
        plt.plot(self.time[::50], self.filtered[::50], label="Filtered", linewidth=2.0, color="#ff7f0e")
        plt.title(f"Signal - Axis {self.axis}", fontsize=14)
        plt.xlabel("Time (s)")
        plt.ylabel("Acceleration")
        plt.grid(True, linestyle="--", alpha=0.4)
        plt.legend()
        plt.tight_layout()
        plt.savefig(signal_path, dpi=150)
        plt.close()

        # PSD Graph
        psd_path = os.path.join(self.result_dir, f"{self.axis}_psd_{date}.png")
        fig, ax = plt.subplots(figsize=(14, 6))
        ax.plot(self.freqs, self.psd, label="Original", color="black", linewidth=1.5)

        for name, result in self.shaper_results.items():
            label = (
                f"{name} ({self.base_freq:.1f} Hz)  "
                f"vibr={result['vibr']:.2e}  "
                f"accel={result['accel']:.1f}"
            )
            ax.plot(self.freqs, result["psd"], linestyle="--", linewidth=1.2, label=label)

        ax.set_title(f"PSD with Input Shapers - Axis {self.axis}", fontsize=14)
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("Power Spectral Density (PSD)")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.set_xlim(0, 200)
        ax.set_ylim(0, np.max(self.psd) * 1.1)
        ax.legend(loc="upper right", fontsize=8)
        # Adjust lower space
        plt.subplots_adjust(bottom=0.35)
        # Recommended text
        recommendation_text = (
            f"Recommended: {self.best_shaper} ({self.base_freq:.1f} Hz)\n"
            f"Marlin CMD: M593 F{self.base_freq:.1f} D{self.damping} S{self.best_shaper}"
        )

        # Add the box behind the text
        fig.text(0.5, 0.08, recommendation_text, ha="right", va="bottom", fontsize=10, zorder=2,
                bbox=dict(facecolor="white", edgecolor="gray", boxstyle="round,pad=0.5"))

        plt.tight_layout(rect=[0, 0.03, 1, 1])  # Leaves space for text at bottom
        plt.savefig(psd_path, dpi=150)
        plt.close()

        return signal_path, psd_path, self.shaper_results, self.best_shaper, self.base_freq

    def get_recommendation(self):
        return f"M593 F{self.base_freq:.1f} D{self.damping} S{self.best_shaper}"
