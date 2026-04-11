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

REAL MODE (Raspberry Pi + ADALM-Pluto connected):
  - Install libiio system package first:
      sudo apt install libiio-utils libiio-dev
  - Then: pip install pyadi-iio numpy scipy
  - Set sdr.device_uri in system_config.yaml:
      USB:  "usb:"
      IP:   "ip:192.168.2.1"

SIMULATION MODE (Windows / dev laptop / no Pluto):
  - pyadi-iio does NOT need to be installed.
  - Run: python main.py --sim
  - A synthetic 150 m beat tone is generated instead of real IQ samples.
  - All downstream modules (fusion, jammer) work identically.

WHY LAZY IMPORT:
  pyadi-iio imports libiio (a C shared library) at module load time.
  On Windows, libiio.dll is not installed and the import raises a TypeError
  deep inside ctypes — BEFORE our try/except can catch it.
  Solution: import adi only inside connect(), not at the top of the file.

DEPENDENCIES (real mode only):
  sudo apt install libiio-utils libiio-dev   # Linux only
  pip install pyadi-iio numpy scipy
"""

import time
import logging
import numpy as np
from scipy.signal import windows
from typing import Optional

logger = logging.getLogger(__name__)

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

        self._sdr: Optional[object] = None   # adi.Pluto instance when connected
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    def connect(self):
        """
        Open connection to the ADALM-Pluto.  Call before run().

        The pyadi-iio import is done HERE (not at module level) because
        libiio.dll is not present on Windows, and an import at module level
        would crash the whole process before any try/except could catch it.
        """
        # Attempt the lazy import of pyadi-iio
        try:
            import adi as _adi
            _adi_module = _adi
        except Exception as exc:
            # libiio not installed, or on Windows without the DLL —
            # this is normal for sim mode; just log and continue.
            logger.warning(
                f"[SDR] pyadi-iio / libiio not available ({type(exc).__name__}: {exc}). "
                f"Running in SIMULATION mode — no real SDR hardware will be used."
            )
            self._sdr = None
            return

        # Try to actually connect to the Pluto hardware
        uri = self._cfg["device_uri"]
        logger.info(f"[SDR] Connecting to ADALM-Pluto at {uri} …")
        try:
            self._sdr = _adi_module.Pluto(uri)

            # ── TX config ──────────────────────────────────────────────────
            # tx_cyclic_buffer=True loops the chirp waveform continuously
            # so we only need to call tx() once.
            self._sdr.tx_rf_bandwidth         = self._bw
            self._sdr.tx_lo                   = self._tx_freq
            self._sdr.tx_hardwaregain_chan0    = self._tx_gain  # negative = attenuation
            self._sdr.tx_cyclic_buffer         = True
            self._sdr.tx_buffer_size           = self._n_samples

            # ── RX config ──────────────────────────────────────────────────
            self._sdr.rx_rf_bandwidth          = self._bw
            self._sdr.rx_lo                    = self._rx_freq
            self._sdr.gain_control_mode_chan0   = "manual"
            self._sdr.rx_hardwaregain_chan0     = self._rx_gain
            self._sdr.rx_buffer_size            = self._n_samples
            self._sdr.sample_rate               = self._sample_rate

            logger.info("[SDR] ADALM-Pluto connected and configured successfully")

        except Exception as exc:
            logger.error(
                f"[SDR] Hardware connection to '{uri}' failed: {exc}. "
                f"Falling back to SIMULATION mode."
            )
            self._sdr = None

    def run(self):
        """
        Blocking loop — call this in a dedicated thread.
        Continuously acquires RX samples, processes them and publishes events.

        REAL MODE:   reads IQ from Pluto → FMCW FFT → publish
        SIM MODE:    generates synthetic beat tone → same FFT path → publish
        Both paths publish identical "sdr.detection" events so downstream
        modules (fusion, jammer) behave identically in both modes.
        """
        mode = "REAL" if self._sdr is not None else "SIMULATION"
        logger.info(f"[SDR] Pipeline running in {mode} mode")
        self._running = True

        # Transmit the FMCW chirp (only meaningful in real mode)
        chirp_tx = self._build_chirp()
        if self._sdr is not None:
            self._sdr.tx(chirp_tx)
            logger.info("[SDR] Chirp transmitted on TX port (cyclic)")

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
        The IQ samples are scaled to int16 for the Pluto DAC.
        In sim mode this is still computed but never sent to hardware.
        """
        f_inst = np.linspace(-self._bw / 2, self._bw / 2, self._n_samples)
        phase = 2 * np.pi * np.cumsum(f_inst) / self._sample_rate
        chirp = np.exp(1j * phase)
        # Scale to int16 range expected by the Pluto TX buffer
        chirp_i16 = (chirp * 2**14).astype(np.int16)
        logger.debug(f"[SDR] Chirp built — {self._n_samples} samples, BW={self._bw/1e6:.0f} MHz")
        return chirp_i16

    def _acquire_samples(self) -> np.ndarray:
        """
        REAL MODE:  receive one RX buffer from the Pluto (blocking call).
        SIM MODE:   generate a synthetic beat tone at 150 m with added noise.

        Returns:
            numpy complex64 array of length n_samples.
        """
        if self._sdr is not None:
            # ── Real hardware path ─────────────────────────────────────────
            raw = self._sdr.rx()
            return raw.astype(np.complex64)

        else:
            # ── Simulation path ────────────────────────────────────────────
            # Synthesise a beat tone matching what a 150 m target would produce.
            # beat_freq = (2 * BW * range) / (c * chirp_dur)
            sim_range = 150.0
            beat_freq = (2 * self._bw * sim_range) / (_C * self._chirp_dur)
            t = np.linspace(0, self._chirp_dur, self._n_samples)
            noise = (np.random.randn(self._n_samples) +
                     1j * np.random.randn(self._n_samples)) * 0.05
            signal = 0.4 * np.exp(1j * 2 * np.pi * beat_freq * t) + noise
            # Sleep to mimic the real acquisition cadence
            time.sleep(self._chirp_dur)
            return signal.astype(np.complex64)

    def _process_fmcw(self, rx: np.ndarray):
        """
        Compute the beat-frequency FFT and convert the peak to range.

        Steps:
          1. Apply Hanning window → reduces spectral leakage.
          2. FFT → magnitude spectrum (positive freqs only).
          3. Find peak bin above threshold.
          4. peak_bin → beat_freq → range_m.

        Returns:
            (range_m, rssi_db) if target detected above threshold, else None.
        """
        win = windows.hann(len(rx))
        spectrum = np.fft.fft(rx * win, n=self._fft_size)
        magnitude = np.abs(spectrum[:self._fft_size // 2])

        # Normalise: threshold is relative to the strongest bin
        mag_norm = magnitude / (np.max(magnitude) + 1e-12)
        peak_idx = int(np.argmax(mag_norm))
        peak_val = float(mag_norm[peak_idx])

        if peak_val < self._threshold:
            return None

        freq_resolution = self._sample_rate / self._fft_size
        beat_freq = peak_idx * freq_resolution
        range_m = (beat_freq * _C * self._chirp_dur) / (2 * self._bw)

        if range_m > self._max_range:
            return None

        rssi_db = float(20 * np.log10(peak_val + 1e-12))
        return range_m, rssi_db
