"""Input Shaping Analyzer for OctoPrint Plugin Pinput_Shaping"""

import logging
import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt, welch, find_peaks

MAX_BYTES_32 = 2_000_000_000  # ~ 2 gi b


class InputShapingAnalyzer:
    """Class to analyze input shaping data from a CSV file.
    It loads the data, applies low-pass filtering, computes the Power Spectral Density (PSD),
    generates input shapers, applies them, and generates graphs.
    It also provides methods to get recommendations for input shaping commands.
    """

    def __init__(
        self, save_dir, csv_path, damping=0.15, cutoff_freq=100, axis="X", min_accel=300, max_accel=8000, min_freq=20, max_freq=132, logger=None
    ) -> None:
        """
        Initializes the InputShapingAnalyzer with the given parameters.
        :param save_dir: Directory to save the results.
        :param csv_path: Path to the CSV file containing the raw acceleration data.
        :param damping: Damping factor for the input shapers (default is 0.5).
        :param cutoff_freq: Cutoff frequency for the low-pass filter (default is 100 Hz).
        :param axis: Axis to analyze (e.g., "X", "Y", "Z"). If None, it will be set to "X".
        :param logger: Optional logger for logging messages. If None, a default logger will be used.
        """

        self._plugin_logger = logger or logging.getLogger(
            "octoprint.plugins.Pinput_Shaping"
        )
        self.csv_path = csv_path
        self.damping = float(damping)
        self.cutoff_freq = float(cutoff_freq)
        # Default to X if an axis is not provided
        self.axis = (axis or "X").upper()
        self.result_dir = save_dir
        self.best_shaper = None
        self.base_freq = None  # Dominant resonance frequency (f_n)
        self.shaper_results = {}
        self.time = None
        self.raw = None
        self.filtered = None
        self.sampling_rate = None
        self.freqs = None
        self.psd = None
        # Ensure numeric types (settings can come back as strings)
        self.min_accel = float(min_accel) if min_accel is not None else 300.0
        self.max_accel = float(max_accel) if max_accel is not None else 8000.0
        configured_value = float(min_freq) if min_freq is not None else 20.0 # Ensure at least 20 Hz to avoid the DC bias/drift
        self.min_freq = max(20.0, configured_value)
        self.max_freq = float(max_freq) if max_freq is not None else 132.0

    def load_data(self) -> None:
        """Loads the data from the CSV file and processes it."""

        self._plugin_logger.info(
            f"Loading data from CSV file {self.csv_path} for axis {self.axis}"
        )
        df = pd.read_csv(self.csv_path)
        df.columns = [c.strip().lower() for c in df.columns]

        # Time
        df["time"] = pd.to_numeric(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])

        # selected axis
        axis_col = self.axis.lower()  # "x" or "y" or "z"
        if axis_col not in df.columns:
            raise ValueError(f"Column '{axis_col}' not found in CSV")
        
        df[axis_col] = pd.to_numeric(df[axis_col], errors="coerce")
        df = df.dropna(subset=[axis_col])

        self.time = df["time"].to_numpy(dtype=np.float64)
        self.raw = df[axis_col].to_numpy(dtype=np.float64)

        self.sampling_rate = 1.0 / np.mean(np.diff(self.time))

    def lowpass_filter(self, data, order=4) -> np.ndarray:
        """Applies a low-pass Butterworth filter to the data.
        :param data: The input data to filter.
        :param order: The order of the Butterworth filter (default is 4).
        :return: The filtered data.
        """
        self._plugin_logger.info("Applying low-pass Butterworth filter")

        nyq = 0.5 * self.sampling_rate
        cutoff = min(self.cutoff_freq, nyq * 0.99)
        norm_cutoff = cutoff / nyq
        self._plugin_logger.info(
            f"lowpass_filter: cutoff={cutoff}, nyq={nyq}, norm_cutoff={norm_cutoff}, sampling_rate={self.sampling_rate}"
        )
        if not (0 < norm_cutoff < 1):
            self._plugin_logger.error(
                f"Invalid norm_cutoff: {norm_cutoff} (cutoff={cutoff}, nyq={nyq})"
            )
            raise ValueError(
                f"Digital filter critical frequencies must be 0 < Wn < 1 (got {norm_cutoff}, cutoff={cutoff}, nyq={nyq})"
            )
        b, a = butter(order, norm_cutoff, btype="low")
        return filtfilt(b, a, data)

    def generate_shapers(self, freq) -> dict:
        """Generates input shapers based on the given frequency.
        The frequency (freq) is the natural frequency (f_n) used to calculate
        the impulse time (t).
        """
        self._plugin_logger.info(f"Generating input shapers for frequency: {freq} Hz")

        # NOTE:
        # The classic shaper formulas assume the impulse spacing is based on the
        # *half period* of the (damped) oscillation. Using t=1/f makes the shaper
        # effectively twice as long (and pushes the optimizer to compensate by
        # selecting odd frequencies).
        zeta = float(self.damping)
        fn = float(freq)
        fd = fn * np.sqrt(max(1e-9, 1.0 - zeta**2))     # damped natural frequency
        t = 1.0 / (2.0 * fd)                            # half-period (seconds)

        # K is the ratio of amplitudes of successive damped oscillations
        denom = np.sqrt(max(1e-9, 1.0 - zeta**2))
        K = np.exp(-zeta * np.pi / denom)

        shapers = {}

        # Zero Vibration (ZV)
        shapers["ZV"] = [(0, 1 / (1 + K)), (t, K / (1 + K))]

        # Modified ZV (MFA)
        shapers["MZV"] = [
            (0,     1 / (1 + 2*K + K**2)),
            (t,     2*K / (1 + 2*K + K**2)),
            (2*t,   K**2 / (1 + 2*K + K**2)),
        ]

        # Extra-Insensitive (EI)
        shapers["EI"] = [
            (0,     1 / (1 + 3*K + 3*K**2 + K**3)),
            (t,     3*K / (1 + 3*K + 3*K**2 + K**3)),
            (2*t,   3*K**2 / (1 + 3*K + 3*K**2 + K**3)),
            (3*t,   K**3 / (1 + 3*K + 3*K**2 + K**3)),
        ]

        # 2-Hump EI
        shapers["2HUMP_EI"] = [
            (0,   1 / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
            (t,   4*K / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
            (2*t, 6*K**2 / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
            (3*t, 4*K**3 / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
            (4*t, K**4 / (1 + 4*K + 6*K**2 + 4*K**3 + K**4)),
        ]

        # 3-Hump EI
        shapers["3HUMP_EI"] = [
            (0,   1 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (t,   6*K / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (2*t, 15*K**2 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (3*t, 20*K**3 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (4*t, 15*K**4 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (5*t, 6*K**5 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
            (6*t, K**6 / (1 + 6*K + 15*K**2 + 20*K**3 + 15*K**4 + 6*K**5 + K**6)),
        ]

        return shapers


    def _calculate_residual_vibration(self, shaper, base_freq) -> float:
        """
        Calculates the theoretical Residual Vibration (RV) for a given shaper
        (list of (time, amplitude) tuples) and a base resonance frequency (f_n).

        RV is calculated using the magnitude of the complex frequency response at f_n.
        :param shaper: The input shaper impulse definition.
        :param base_freq: The dominant resonance frequency (f_n) of the system.
        :return: The Residual Vibration magnitude (0 to 1).
        """
        self._plugin_logger.info(f"Calculating residual vibration for base frequency: {base_freq} Hz")

        # Damped oscillator model:
        # - amplitude decays as exp(-zeta * wn * t)
        # - oscillates at wd = wn * sqrt(1 - zeta^2)
        zeta = float(self.damping)
        wn = 2.0 * np.pi * float(base_freq)
        wd = wn * np.sqrt(max(1e-9, 1.0 - zeta**2))

        real_part = 0.0
        imag_part = 0.0
        for ti, Ai in shaper:
            decay = np.exp(-zeta * wn * ti)
            real_part += Ai * decay * np.cos(wd * ti)
            imag_part += Ai * decay * np.sin(wd * ti)

        # RV is the magnitude of the complex vector: sqrt(real^2 + imag^2)
        residual = np.sqrt(real_part**2 + imag_part**2)
        return residual



    def apply_shaper(self, signal, time, shaper) -> np.ndarray:
        """Applies the input shaper to the signal.
        :param signal: The input signal to shape.
        :param time: The time vector corresponding to the signal.
        :param shaper: The input shaper to apply, defined as a list of (delay, amplitude) tuples.
        :return: The shaped signal.
        """

        self._plugin_logger.info("Applying input shaper to the signal")

        dt = np.mean(np.diff(time))
        n = len(signal)
        shaped = np.zeros(n)
        for delay, amp in shaper:
            shift = int(np.round(delay / dt))
            if shift < n:
                shaped[shift:] += amp * signal[: n - shift]
        return shaped

    def compute_psd(self, signal: np.ndarray) -> tuple:
        """Computes the Power Spectral Density (PSD) of the signal using Welch's method.
        :param signal: The input signal to analyze.
        :return: A tuple containing the frequencies and the corresponding PSD values.
        """

        self._plugin_logger.info("Computing Power Spectral Density (PSD) using Welch's method")
        # Remove DC drift to avoid overweighting very low frequencies in the PSD
        sig = np.asarray(signal, dtype=np.float64) - np.mean(signal)

        # Adaptive Welch that guarantees not exceeding the limit of 2 gi b.
        sig = sig.astype(np.float32, copy=False)

        # starting point
        nperseg = min(4096, len(sig) // 8)
        nperseg = max(nperseg, 256)

        while True:
            n_win = len(sig) - nperseg + 1
            est_mem = n_win * nperseg * sig.itemsize
            if est_mem < MAX_BYTES_32 or nperseg <= 256:
                break
            nperseg //= 2  # reduces half and try again

        self._plugin_logger.debug(
            f"Welch: nperseg={nperseg}, windows={n_win}, "
            f"est_mem={est_mem / 1e6:.1f} MB, len={len(sig)}"
        )

        return welch(sig, fs=self.sampling_rate, nperseg=nperseg)


    def _calculate_suggested_accel(self, shaper_duration: float, max_accel_base) -> int:
        """
        Calculates a suggested maximum acceleration based on shaper duration.
        This uses a heuristic where longer shaper durations severely penalize max acceleration.

        Shorter T_shaper (like ZV) leads to higher acceleration.
        Longer T_shaper (like 3HUMP_EI) leads to lower acceleration.

        :param shaper_duration: The total duration of the shaper impulse in seconds.
        :param max_accel_base: The theoretical maximum acceleration for a zero-duration shaper (e.g., 10000 mm/s^2).
        :return: Suggested maximum acceleration (integer mm/s^2).
        """
        self._plugin_logger.info(f"Calculating suggested max acceleration for shaper duration: {shaper_duration:.4f} s")
        if shaper_duration <= 0:
            return self.max_accel  # Return max base for zero duration

        # Heuristic function: A_max = A_base / (1 + factor * T_shaper^power)
        # Power=2 makes the penalty steep.
        # Factor=30 is a tuning constant to place typical results in the 5000-10000 range.

        penalty_factor = 30.0
        penalty = 1 + penalty_factor * (shaper_duration**2)
        # Make sure we operate on floats to avoid dtype issues from settings
        base = float(max_accel_base)
        suggested = base / penalty

        # Ensure minimum useful value and format as integer
        return int(max(self.min_accel, suggested))



    def analyze(self) -> str:
        """
        Analyzes the input shaping data.

        This method now iterates over a range of frequencies for each shaper type
        to find the optimal shaping frequency (f_s) that minimizes the theoretical
        residual vibration (RV) at the dominant base resonance frequency (f_n).
        """
        self._plugin_logger.info("Starting input shaping analysis")

        self.load_data()
        try:
            self.filtered = self.lowpass_filter(self.raw)
        except ValueError as e:
            self._plugin_logger.error(f"Lowpass filter failed: {e}")
            raise

        self.freqs, self.psd = self.compute_psd(self.raw) # get ...w signal, should be better than filtered for resonance detection

        # Identify the dominant resonance frequency (f_n)
        # Old logic used a raw argmax which is very sensitive to wide humps, noise, and low-frequency drift.
        # Here we use peak *prominence* to pick a stable physical resonance.
        freq_range = (self.freqs >= self.min_freq) & (self.freqs <= self.max_freq)  # Common range for 3D printers
        if not np.any(freq_range):
            self._plugin_logger.warning("No significant frequency range available. Using max PSD peak.")
            self.base_freq = self.freqs[np.argmax(self.psd)]
        else:
            f = self.freqs[freq_range]
            p = self.psd[freq_range]

            # Find candidate peaks with a relative prominence threshold (robust against humps)
            peaks, props = find_peaks(p, prominence=max(np.max(p) * 0.05, 1e-12))

            if len(peaks) == 0:
                self._plugin_logger.warning("No prominent peaks found. Using max PSD peak in range.")
                self.base_freq = float(f[np.argmax(p)])
            else:
                # Prefer typical printer resonance band for X/Y if available (adjust if needed)
                preferred = (f[peaks] >= 40.0) & (f[peaks] <= 70.0)
                cand = peaks[preferred] if np.any(preferred) else peaks

                prom = props["prominences"]
                prom_cand = prom[preferred] if np.any(preferred) else prom
                best_idx = cand[int(np.argmax(prom_cand))]
                self.base_freq = float(f[best_idx])

        self._plugin_logger.info(f"Dominant resonance frequency (base_freq): {self.base_freq:.2f} Hz")

        self.shaper_results = {}
        self.best_shaper = None

        self.time = np.asarray(self.time, dtype=np.float64)

        self._plugin_logger.info("Computing reference (unshaped) PSD")
        # Keep the original PSD as reference (unshaped)
        # NOTE: We'll compute shaped PSD per shaper below.

        best_overall_vibr = float('inf')
        self.best_shaper = None

        # Get shaper names by generating a dummy shaper (frequency doesn't matter yet)
        shaper_names = self.generate_shapers(1.0).keys()

        for name in shaper_names:
            best_freq = self.base_freq
            min_residual = float('inf')

            # Define the frequency search window for this shaper.
            # Using a narrow +/- window around base_freq prevents matching Klipper's behavior
            # for multi-hump shapers (which often land at much higher frequencies).
            if name in ("2HUMP_EI", "3HUMP_EI"):
                hi = min(200.0, 0.5 * self.sampling_rate * 0.95)
                lo = self.min_freq
            else:
                lo = self.min_freq
                hi = self.max_freq

            search_steps = 300  # higher resolution makes the fit more stable
            search_freqs = np.linspace(lo, hi, num=search_steps)

            # Find the optimal frequency (f_s) for this shaper type
            for test_freq in search_freqs:
                # Generate the shaper definition for the test frequency (f_s)
                test_shaper = self.generate_shapers(test_freq)[name]

                # Calculate the residual using the formula, referencing the base resonance (f_n)
                residual = self._calculate_residual_vibration(test_shaper, self.base_freq)

                if residual < min_residual:
                    min_residual = residual
                    best_freq = test_freq

            # Apply the BEST shaper (with the optimal frequency) to the filtered signal
            optimal_shaper_data = self.generate_shapers(best_freq)[name]

            # Calculate shaper duration for acceleration estimation
            if optimal_shaper_data:
                # Duration is the time difference between the last and first impulse
                shaper_duration = optimal_shaper_data[-1][0] - optimal_shaper_data[0][0]
            else:
                shaper_duration = 0.0

            shaped = self.apply_shaper(self.filtered, self.time, optimal_shaper_data)
            _, shaped_psd = self.compute_psd(shaped)

            # Calculate vibration and acceleration metrics for comparison
            band = (self.freqs >= self.min_freq) & (self.freqs <= self.max_freq)
            vibr = float(np.trapz(shaped_psd[band], self.freqs[band]))
            # 'accel' here represents the peak acceleration experienced after shaping,
            # serving as a proxy for the shaper's impact on the signal.
            accel = max(np.abs(np.gradient(shaped, np.mean(np.diff(self.time)))))

            suggested_accel = self._calculate_suggested_accel(shaper_duration, self.max_accel)

            # Store results
            self.shaper_results[name] = {
                "psd": shaped_psd,
                "vibr": vibr,
                "accel": accel,
                "shaper_freq": best_freq,       # Optimal shaping frequency (f_s)
                "residual_vibr": min_residual,
                "shaper_duration": shaper_duration, # Total duration of the shaper (sec)
                "suggested_accel": suggested_accel  # Suggested max acceleration (mm/s^2)
            }

            self._plugin_logger.info("Shaper: %s | f_s: %.2f Hz | RV: %.4f | Vibr: %.6f | Accel: %.1f | Duration: %.4f s | Suggested Accel: %d mm/s²",
                name,
                best_freq,
                min_residual,
                vibr,
                accel,
                shaper_duration,
                suggested_accel
            )

            # Update the best overall shaper based on the lowest actual vibration reduction
            if vibr < best_overall_vibr:
                best_overall_vibr = vibr
                self.best_shaper = name

        return self.best_shaper


    def generate_graphs(self) -> tuple:
        """Generates graphs for the original and filtered signals, and the PSD with input shapers.
        :return: A tuple containing the paths to the generated graphs and the shaper results.
        """

        self._plugin_logger.info("Generating graphs for signal and PSD")
        # get the date from csv file which format is Raw_accel_values_AXIS_X_20250416T133919.csv
        date = os.path.basename(self.csv_path).split("_")[-1].split(".")[0]

        # --- Signal Graph ---
        signal_path = os.path.join(self.result_dir, f"{self.axis}_signal_{date}.png")
        plt.figure(figsize=(14, 5))
        plt.plot(
            self.time[::50],
            self.raw[::50],
            label="Original",
            alpha=0.4,
            color="#007bff",
        )
        plt.plot(
            self.time[::50],
            self.filtered[::50],
            label="Filtered",
            linewidth=2.0,
            color="#ff7f0e",
        )
        plt.title(f"Signal - Axis {self.axis}", fontsize=14)
        plt.xlabel("Time (s)")
        plt.ylabel("Acceleration")
        plt.grid(True, linestyle="--", alpha=0.4)
        plt.legend()
        plt.tight_layout()
        plt.savefig(signal_path, dpi=150)
        plt.close()


        # --- PSD Graph ---
        psd_path = os.path.join(self.result_dir, f"{self.axis}_psd_{date}.png")

        fig, ax = plt.subplots(figsize=(14, 8))
        ax.plot(self.freqs, self.psd, label="Original", color="black", linewidth=1.5)

        for name, result in self.shaper_results.items():
            # Use the specific optimal frequency (f_s) and duration (T_shaper)
            shaper_freq = result.get("shaper_freq", self.base_freq)
            shaper_dur_ms = result.get("shaper_duration", 0.0) * 1000

            # get the suggested acceleration
            suggested_accel = result.get("suggested_accel", 0)

            label = (
                f"{name} ({shaper_freq:.1f} Hz / {shaper_dur_ms:.1f} ms)  "
                f"vibr={result['vibr']:.2e}  "
                f"Accel Max={suggested_accel}"
            )
            ax.plot(
                self.freqs, result["psd"], linestyle="--", linewidth=1.2, label=label
            )

        max_x_limit = self.max_freq + 5.0 # Extend x-axis a bit beyond max_freq setting
        resonance_range = self.freqs >= 15.0 # Focus on frequencies above 15 Hz

        # ... (Y-axis limit logic)
        if np.any(resonance_range):
            max_psd_in_range = np.max(self.psd[resonance_range])
            y_limit = max_psd_in_range * 1.25
        else:
            y_limit = np.max(self.psd) * 1.1

        ax.set_title(f"PSD with Input Shapers - Axis {self.axis} (Resonance at {self.base_freq:.1f} Hz)", fontsize=14)
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("Power Spectral Density (PSD)")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.set_xlim(0, max_x_limit) # Limit x-axis to focus on relevant range

        ax.set_ylim(0, y_limit) # Dynamic y-axis limit based on resonance range

        # Custom legend placement bottom center, outside plot area
        ax.legend(
            loc='best', # Position
            bbox_to_anchor=(0.5, -0.2), # Coordinate outside the plotting area (0.5=center X, -0.2=below X axis)
            # ncol=2, # Show in 2 columns for compactness
            fontsize=8,
            fancybox=True,
            shadow=True
        )


        plt.subplots_adjust(bottom=0.15)

        # Get the optimal frequency (f_s) for the BEST shaper
        best_freq = self.shaper_results[self.best_shaper]["shaper_freq"]
        best_dur_ms = self.shaper_results[self.best_shaper]["shaper_duration"] * 1000
        best_accel = self.shaper_results[self.best_shaper]["suggested_accel"]

        # The Shaper Duration (T_shaper) is the limiting factor for Max Acceleration.
        # Shaper Duration = T_shaper (in ms)
        recommendation_text = (
            f"Recommended Shaper: {self.best_shaper} ({best_freq:.1f} Hz)\n"
            f"Accel Max Suggested: {best_accel} mm/s²\n" # Incluimos la aceleración sugerida
            f"Shaper Duration (Accel Limit): {best_dur_ms:.1f} ms\n"
            f"Marlin CMD: M593 F{best_freq:.1f} D{self.damping} S{self.best_shaper}"
        )

        # Add the box behind the text
        ax.text(
            0.96, 0.96, # X, Y coordinates (Near the top right corner)
            recommendation_text,
            transform=ax.transAxes, # coordinates relative to the axis
            ha="right", # Horizontal alignment: right of the anchor point
            va="top",   # Vertical alignment: top of the anchor point
            fontsize=10,
            zorder=10,  # Ensures it stays above everything else
            bbox={"facecolor": 'white', "edgecolor": 'gray', "boxstyle": 'round,pad=0.5', "alpha": 0.9},
        )

        plt.tight_layout(rect=[0, 0.03, 1, 1])  # Leaves space for text at bottom
        plt.savefig(psd_path, dpi=150)
        plt.close()

        return (
            signal_path,
            psd_path,
            self.shaper_results,
            self.best_shaper,
            best_freq
        )

    def get_recommendation(self) -> str:
        """Generates a recommendation string for the best input shaper."""
        # Use the optimal frequency (f_s) for the best shaper

        self._plugin_logger.info("Generating recommendation for best input shaper")
        best_freq = self.shaper_results[self.best_shaper]["shaper_freq"]
        return f"M593 F{best_freq:.1f} D{self.damping} S{self.best_shaper}"

    def get_plotly_data(self) -> dict:
        """Generates data for Plotly visualization."""
        # Use the optimal frequency (f_s) for the best shaper

        self._plugin_logger.info("Generating Plotly data for visualization")
        best_freq = self.shaper_results[self.best_shaper]["shaper_freq"]

        return {
            "time": [float(t) for t in self.time[::5]],
            "raw": [float(r) for r in self.raw[::5]],
            "filtered": [float(f) for f in self.filtered[::5]],
            "freqs": [float(f) for f in self.freqs],
            "psd_original": [float(p) for p in self.psd],
            "shapers": {
                name: {
                    "psd": [float(p) for p in result["psd"]],
                    "vibr": round(float(result["vibr"]), 3),
                    "accel": round(float(result["accel"]), 2),
                    "shaper_freq": round(float(result["shaper_freq"]), 2),
                    "shaper_duration_ms": round(float(result["shaper_duration"]) * 1000, 1), # Duration in ms
                    "suggested_accel": int(result["suggested_accel"]),
                }
                for name, result in self.shaper_results.items()
            },
            "base_freq": round(float(self.base_freq), 2),
            "best_shaper": str(self.best_shaper),
            "best_shaper_freq": round(float(best_freq), 2)
        }
