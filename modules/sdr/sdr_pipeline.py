"""
sdr_pipeline.py  —  ADALM-Pluto SDR + FMCW radar pipeline.

WHAT THIS DOES:
  1. Connects to the ADALM-Pluto SDR via libiio / pyadi-iio.
  2. Generates a linear FMCW (Frequency Modulated Continuous Wave) chirp
     on the TX port.
  3. Captures the received IQ samples on the RX port.
  4. Computes the beat frequency via FFT.
     beat_freq = (2 * BW * range) / (c * chirp_duration)
     → range_m = (beat_freq * c * chirp_duration) / (2 * BW)
  5. If the beat peak exceeds the detection threshold, publishes a
     "sdr.detection" event on the event bus.

DEPENDENCIES:
  pip install pyadi-iio adi numpy scipy

INTEGRATION:
  This module runs as a subprocess (via multiprocessing) so it doesn't
  block the main event loop.  It uses QueueBridge to pass events back.

SIMULATION MODE:
  If pyadi-iio is not installed or no Pluto is connected, the class falls
  back to generating synthetic sine beats so the rest of the pipeline can
  be tested on a laptop.
"""

import time
import logging
import numpy as np
from scipy.signal import windows
from typing import Optional

logger = logging.getLogger(__name__)

# Try to import the Pluto driver.  Fall back gracefully for dev machines.
try:
    import adi
    _HAS_ADI = True
except ImportError:
    logger.warning("[SDR] pyadi-iio not found — running in SIMULATION mode")
    _HAS_ADI = False

# Speed of light (m/s)
_C = 3e8


class SDRPipeline:
    """
    FMCW radar using ADALM-Pluto SDR.

    Args:
        cfg: The 'sdr' section from system_config.yaml.
        bus: EventBus or QueueBridge for publishing events.
    """

    def __init__(self, cfg: dict, bus):
        self._cfg = cfg
        self._bus = bus

        self._sample_rate  = cfg["sample_rate_hz"]
        self._bw           = cfg["bandwidth_hz"]
        self._tx_freq      = cfg["tx_freq_hz"]
        self._rx_freq      = cfg["rx_freq_hz"]
        self._tx_gain      = cfg["tx_gain_db"]
        self._rx_gain      = cfg["rx_gain_db"]
        self._chirp_dur    = cfg["chirp_duration_sec"]
        self._fft_size     = cfg["fft_size"]
        self._threshold    = cfg["range_detect_threshold"]
        self._max_range    = cfg["max_range_m"]

        # Number of IQ samples per chirp
        self._n_samples = int(self._sample_rate * self._chirp_dur)

        self._sdr: Optional[object] = None   # adi.Pluto instance or None

        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    def connect(self):
        """Open connection to the ADALM-Pluto.  Call before run()."""
        if not _HAS_ADI:
            logger.warning("[SDR] Simulation mode — no hardware connection")
            return

        uri = self._cfg["device_uri"]
        logger.info(f"[SDR] Connecting to Pluto at {uri} …")
        try:
            self._sdr = adi.Pluto(uri)

            # ── TX config ──
            self._sdr.tx_rf_bandwidth         = self._bw
            self._sdr.tx_lo                   = self._tx_freq
            self._sdr.tx_hardwaregain_chan0    = self._tx_gain  # negative = attenuation
            self._sdr.tx_cyclic_buffer         = True           # loop chirp continuously
            self._sdr.tx_buffer_size           = self._n_samples

            # ── RX config ──
            self._sdr.rx_rf_bandwidth          = self._bw
            self._sdr.rx_lo                    = self._rx_freq
            self._sdr.gain_control_mode_chan0   = "manual"
            self._sdr.rx_hardwaregain_chan0     = self._rx_gain
            self._sdr.rx_buffer_size            = self._n_samples
            self._sdr.sample_rate               = self._sample_rate

            logger.info("[SDR] ADALM-Pluto connected and configured")
        except Exception as exc:
            logger.error(f"[SDR] Connection failed: {exc} — falling back to simulation")
            self._sdr = None

    def run(self):
        """
        Blocking loop — call this in a dedicated process/thread.
        Continuously acquires RX samples, processes them and publishes events.
        """
        logger.info("[SDR] Pipeline started")
        self._running = True

        # Build and transmit the FMCW chirp once (cyclic buffer loops it)
        chirp_tx = self._build_chirp()
        if self._sdr is not None:
            self._sdr.tx(chirp_tx)

        while self._running:
            try:
                rx = self._acquire_samples()
                result = self._process_fmcw(rx)
                if result is not None:
                    range_m, rssi_db = result
                    logger.info(
                        f"[SDR] Detection — range={range_m:.1f} m  RSSI={rssi_db:.1f} dB"
                    )
                    self._bus.publish("sdr.detection", {
                        "range_m":  range_m,
                        "rssi_db":  rssi_db,
                        "freq_hz":  self._tx_freq,
                        "ts":       time.monotonic(),
                    })
            except Exception as exc:
                logger.error(f"[SDR] Processing error: {exc}")
                time.sleep(0.1)

    def stop(self):
        """Signal the run loop to exit."""
        self._running = False
        if self._sdr is not None:
            try:
                self._sdr.tx_destroy_buffer()
            except Exception:
                pass

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_chirp(self) -> np.ndarray:
        """
        Generate a complex linear chirp signal for FMCW transmission.

        A linear chirp sweeps from -BW/2 to +BW/2 over chirp_duration_sec.
        The IQ samples are scaled to fit the DAC's integer range (int16).
        """
        t = np.linspace(0, self._chirp_dur, self._n_samples)
        # Instantaneous frequency: starts at -BW/2, ends at +BW/2
        f_inst = np.linspace(-self._bw / 2, self._bw / 2, self._n_samples)
        # Integrate frequency to get phase
        phase = 2 * np.pi * np.cumsum(f_inst) / self._sample_rate
        chirp = np.exp(1j * phase)
        # Scale to int16 range expected by the Pluto TX buffer
        chirp_i16 = (chirp * 2**14).astype(np.int16)
        logger.debug(f"[SDR] Chirp built — {self._n_samples} samples")
        return chirp_i16

    def _acquire_samples(self) -> np.ndarray:
        """
        Receive one buffer of IQ samples from the Pluto.
        In simulation mode, generate a synthetic beat tone.

        Returns:
            numpy complex array of length n_samples.
        """
        if self._sdr is not None:
            raw = self._sdr.rx()
            # Pluto returns interleaved int16; adi already gives us complex float
            return raw.astype(np.complex64)
        else:
            # ── SIMULATION: synthetic beat at a fixed range ──────────────────
            # Pretend a drone is at 150 m.
            # beat_freq = (2 * BW * range) / (c * chirp_dur)
            sim_range = 150.0  # metres
            beat_freq = (2 * self._bw * sim_range) / (_C * self._chirp_dur)
            t = np.linspace(0, self._chirp_dur, self._n_samples)
            noise = (np.random.randn(self._n_samples) +
                     1j * np.random.randn(self._n_samples)) * 0.05
            signal = 0.4 * np.exp(1j * 2 * np.pi * beat_freq * t) + noise
            time.sleep(self._chirp_dur)  # mimic real acquisition time
            return signal.astype(np.complex64)

    def _process_fmcw(self, rx: np.ndarray):
        """
        Compute the beat-frequency FFT and convert the peak to range.

        Steps:
          1. Apply a Hanning window to reduce spectral leakage.
          2. Take the FFT and find the magnitude spectrum.
          3. Locate the peak bin above the detection threshold.
          4. Convert peak bin → beat frequency → range in metres.

        Returns:
            (range_m, rssi_db) tuple if a target was detected, else None.
        """
        # Apply Hanning window — reduces spectral leakage from finite signal
        win = windows.hann(len(rx))
        rx_windowed = rx * win

        # FFT — we only look at positive frequencies (real range values)
        spectrum = np.fft.fft(rx_windowed, n=self._fft_size)
        magnitude = np.abs(spectrum[:self._fft_size // 2])

        # Normalise so the threshold is independent of absolute gain
        mag_norm = magnitude / (np.max(magnitude) + 1e-12)

        peak_idx = int(np.argmax(mag_norm))
        peak_val = float(mag_norm[peak_idx])

        if peak_val < self._threshold:
            return None  # No target detected above threshold

        # Convert FFT bin index → beat frequency
        freq_resolution = self._sample_rate / self._fft_size
        beat_freq = peak_idx * freq_resolution

        # beat_freq = (2 * BW * range) / (c * chirp_dur)
        # → range = (beat_freq * c * chirp_dur) / (2 * BW)
        range_m = (beat_freq * _C * self._chirp_dur) / (2 * self._bw)

        if range_m > self._max_range:
            return None  # Beyond our detection limit — ignore

        # Approximate RSSI from peak magnitude (rough dB estimate)
        rssi_db = float(20 * np.log10(peak_val + 1e-12))

        return range_m, rssi_db
