"""
mux_controller.py  —  Antenna MUX switching and lock-on controller.

WHAT THIS DOES:
  Controls a hardware RF multiplexer (e.g. HMC253, PE4259) connected to
  the ADALM-Pluto SDR.  Three antennas plug into the MUX; the MUX output
  goes to the SDR's RX port.

  ROUND-ROBIN MODE (default):
    Cycles through each antenna every `scan_interval_sec` seconds.
    On each switch, sets the correct GPIO pin HIGH and clears the others.

  LOCK-ON MODE:
    When the fusion engine or SDR pipeline emits a confirmed detection on
    a particular antenna, the MUX freezes on that antenna.  The SDR then
    sees a continuous signal instead of a 10-second window.

    Lock is released if:
      - No detection events arrive for `lock_drop_threshold_sec` seconds.
      - main.py explicitly calls mux.unlock().

HOW GPIO WORKS HERE:
  We use RPi.GPIO (Raspberry Pi) or a stub if not available (for testing
  on a laptop).  The GPIO pins are BCM-numbered and set in system_config.yaml.

INTEGRATION POINT:
  This module subscribes to  "sdr.detection"  and  "visual.detection"
  events from the event bus.  When a detection arrives it notes WHICH
  antenna was active at that moment (stored in self._current_antenna)
  and publishes a  "mux.lock"  event so other modules know.
"""

import time
import threading
import logging
from typing import Optional

logger = logging.getLogger(__name__)

from modules.shared.gpio_stub import gpio as _gpio, GPIO_AVAILABLE as _HAS_GPIO


class MUXController:
    """
    Controls the RF antenna multiplexer.

    Args:
        cfg: The 'mux' section from system_config.yaml.
        bus: The shared EventBus instance.
    """

    def __init__(self, cfg: dict, bus):
        self._cfg = cfg
        self._bus = bus

        # Map antenna name → GPIO pin number (from config)
        self._antenna_pins: dict = cfg["antennas"]
        # Ordered list of antenna names for round-robin cycling
        self._antenna_order = list(self._antenna_pins.keys())

        self._scan_interval = cfg["scan_interval_sec"]
        self._lock_drop_thresh = cfg["lock_drop_threshold_sec"]

        # Index into _antenna_order for the current round-robin position
        self._rr_index: int = 0
        # Currently selected antenna name
        self._current_antenna: str = self._antenna_order[0]

        # Lock state
        self._locked: bool = False
        self._locked_antenna: Optional[str] = None
        self._last_detection_ts: float = 0.0

        # Threading control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()

        # Set up GPIO output pins
        for name, pin in self._antenna_pins.items():
            _gpio.setup(pin, _gpio.OUT)
            logger.debug(f"[MUX] GPIO {pin} set as OUTPUT for antenna '{name}'")

        # Subscribe to detection events so we know when to lock
        self._bus.subscribe("sdr.detection",    self._on_detection)
        self._bus.subscribe("visual.detection", self._on_detection)
        self._bus.subscribe("fusion.confirmed", self._on_fusion_confirmed)

        logger.info(f"[MUX] Initialised with antennas: {self._antenna_order}")

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        """Start the background switching thread."""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="MUXController"
        )
        self._thread.start()
        logger.info("[MUX] Switching thread started")

    def stop(self):
        """Signal the thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        _gpio.cleanup()
        logger.info("[MUX] Stopped and GPIO cleaned up")

    def lock(self, antenna_name: str):
        """
        Freeze the MUX on the given antenna.
        Called automatically when a detection is confirmed, but can also
        be called manually from main.py.
        """
        with self._state_lock:
            if antenna_name not in self._antenna_pins:
                logger.error(f"[MUX] Unknown antenna '{antenna_name}' — cannot lock")
                return
            self._locked = True
            self._locked_antenna = antenna_name
            self._select_antenna(antenna_name)
        logger.info(f"[MUX] LOCKED on antenna '{antenna_name}'")
        self._bus.publish("mux.lock", {"antenna": antenna_name, "lock": True})

    def unlock(self):
        """Resume round-robin scanning."""
        with self._state_lock:
            self._locked = False
            self._locked_antenna = None
        logger.info("[MUX] UNLOCKED — resuming round-robin scan")
        self._bus.publish("mux.lock", {"antenna": None, "lock": False})

    @property
    def current_antenna(self) -> str:
        """Name of the currently active antenna."""
        return self._current_antenna

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_loop(self):
        """
        Main switching loop running in a background thread.

        When UNLOCKED: cycles antennas every scan_interval_sec.
        When LOCKED:   holds the locked antenna but monitors for timeout
                       (if no detection for lock_drop_threshold_sec → unlock).
        """
        while not self._stop_event.is_set():
            with self._state_lock:
                locked = self._locked
                locked_antenna = self._locked_antenna

            if locked:
                # Check if detections have gone quiet → time to unlock
                silence = time.monotonic() - self._last_detection_ts
                if silence > self._lock_drop_thresh and self._last_detection_ts > 0:
                    logger.info(
                        f"[MUX] No detection for {silence:.1f}s — unlocking"
                    )
                    self.unlock()
                # Even when locked, sleep briefly to avoid busy-wait
                time.sleep(0.5)

            else:
                # Round-robin: select next antenna
                name = self._antenna_order[self._rr_index]
                self._select_antenna(name)
                self._rr_index = (self._rr_index + 1) % len(self._antenna_order)
                # Sleep for the full scan window
                time.sleep(self._scan_interval)

    def _select_antenna(self, name: str):
        """
        Set GPIO to activate the named antenna and deactivate the rest.
        This is the low-level MUX switch.
        """
        self._current_antenna = name
        for ant_name, pin in self._antenna_pins.items():
            # HIGH = selected, LOW = deselected
            _gpio.output(pin, ant_name == name)
        logger.debug(f"[MUX] Antenna switched to '{name}'")

    def _on_detection(self, topic: str, payload: dict):
        """
        Called whenever SDR or visual pipeline fires a detection event.
        Updates the last-detection timestamp so lock-on stays active.
        If we are in round-robin mode, lock onto the current antenna.
        """
        self._last_detection_ts = time.monotonic()

        with self._state_lock:
            if not self._locked:
                # A new detection arrived while scanning — lock now
                logger.info(
                    f"[MUX] Detection on '{self._current_antenna}' — locking"
                )
                # Call lock() but we're already holding _state_lock so do it inline
                self._locked = True
                self._locked_antenna = self._current_antenna
                self._select_antenna(self._current_antenna)
                self._bus.publish(
                    "mux.lock",
                    {"antenna": self._current_antenna, "lock": True}
                )

    def _on_fusion_confirmed(self, topic: str, payload: dict):
        """
        Fusion engine has confirmed the drone is physically present.
        Ensure we stay locked (redundant safety in case lock_on missed it).
        """
        self._last_detection_ts = time.monotonic()
        logger.debug("[MUX] Fusion confirmed — refreshing lock timestamp")
