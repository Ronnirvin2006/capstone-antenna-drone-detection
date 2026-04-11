"""
jammer_controller.py  —  Action decision and jammer safety controller.

WHAT THIS DOES:
  Implements the final decision layer.  Listens to:
    - "fusion.confirmed"  — physical drone presence confirmed by SDR + visual
    - "identity.result"   — drone classified as FRIENDLY / NON_FRIENDLY / UNKNOWN

  Decision rule (both must be true simultaneously to jam):
    1. fusion.confirmed event arrived within the last fusion_window seconds.
    2. identity.result is NON_FRIENDLY (or UNKNOWN if unknown_is_threat=True).

  If both conditions are met → activate jammer GPIO.
  If the drone later becomes FRIENDLY or fusion drops → deactivate jammer.

SAFETY INTERLOCKS:
  - max_active_sec: jammer automatically turns off after N seconds (circuit breaker).
  - cooldown_sec:   minimum time between consecutive activations.
  - FRIENDLY drones NEVER trigger the jammer, even if fusion confirms them.

GPIO CONTROL:
  The jammer hardware (e.g. a relay or RF switch) is connected to a GPIO pin.
  HIGH = jammer active.  LOW = jammer off.
  Same GPIO stub pattern as mux_controller.py is used.

DEPENDENCIES:
  RPi.GPIO (on Raspberry Pi) or the stub is used automatically.
"""

import time
import threading
import logging
from typing import Optional

logger = logging.getLogger(__name__)

from modules.shared.gpio_stub import gpio as _gpio, GPIO_AVAILABLE as _HAS_GPIO


class JammerController:
    """
    Safety-gated jammer activation controller.

    Args:
        cfg_jammer:   The 'jammer' section from system_config.yaml.
        cfg_identity: The 'identity' section (for unknown_is_threat flag).
        bus:          Shared EventBus instance.
    """

    def __init__(self, cfg_jammer: dict, cfg_identity: dict, bus):
        self._bus = bus

        self._control_pin    = cfg_jammer["control_pin"]
        self._cooldown       = cfg_jammer["cooldown_sec"]
        self._max_active     = cfg_jammer["max_active_sec"]
        self._unknown_threat = cfg_identity.get("unknown_is_threat", True)

        # Time window to consider a fusion.confirmed event still valid
        # (use the fusion window from config; here we hardcode 3s as a safe default)
        self._fusion_valid_window = 3.0

        # State variables (protected by _lock)
        self._lock               = threading.Lock()
        self._jammer_active      = False
        self._last_activation_ts = 0.0    # monotonic time of last activation
        self._activation_start   = 0.0    # monotonic time jammer turned ON
        self._last_fusion_ts     = 0.0    # monotonic time of last fusion.confirmed
        self._last_identity: Optional[dict] = None   # most recent identity result

        # Safety shutoff thread
        self._shutoff_thread: Optional[threading.Thread] = None

        # Set up jammer GPIO pin
        _gpio.setup(self._control_pin, _gpio.OUT)
        _gpio.output(self._control_pin, False)  # ensure jammer is OFF at startup

        # Subscribe to both decision inputs
        self._bus.subscribe("fusion.confirmed", self._on_fusion_confirmed)
        self._bus.subscribe("identity.result",  self._on_identity_result)

        logger.info(
            f"[Jammer] Initialised — pin={self._control_pin}  "
            f"cooldown={self._cooldown}s  max_active={self._max_active}s"
        )

    # ── Event callbacks ───────────────────────────────────────────────────────

    def _on_fusion_confirmed(self, topic: str, payload: dict):
        """
        Physical drone presence confirmed by fusion engine.
        Update the fusion timestamp and re-evaluate the jam decision.
        """
        with self._lock:
            self._last_fusion_ts = time.monotonic()
        logger.debug(f"[Jammer] Fusion confirmed at range={payload.get('range_m'):.1f}m")
        self._evaluate()

    def _on_identity_result(self, topic: str, payload: dict):
        """
        Identity classifier published a result.
        Store it and re-evaluate the jam decision.
        """
        with self._lock:
            self._last_identity = payload
        label = payload.get("label", "UNKNOWN")
        logger.debug(f"[Jammer] Identity result: {label}  drone_id='{payload.get('drone_id')}'")
        self._evaluate()

    # ── Decision logic ────────────────────────────────────────────────────────

    def _evaluate(self):
        """
        The core decision function.  Called whenever either input changes.

        JAM if:
          (A) A fusion.confirmed event arrived within the last fusion_valid_window sec
              AND
          (B) The latest identity result is NON_FRIENDLY
              (or UNKNOWN, if unknown_is_threat=True in config)
              AND
          (C) The jammer is not in a cooldown period
              AND
          (D) The jammer is not already active

        STOP JAM if:
          The latest identity result is FRIENDLY (override — always safe)
        """
        now = time.monotonic()

        with self._lock:
            fusion_fresh  = (now - self._last_fusion_ts) <= self._fusion_valid_window
            identity      = self._last_identity
            active        = self._jammer_active
            in_cooldown   = (now - self._last_activation_ts) < self._cooldown

        # ── FRIENDLY override — always deactivate immediately ─────────────
        if identity is not None and identity.get("label") == "FRIENDLY":
            if active:
                logger.info("[Jammer] Identity=FRIENDLY — deactivating jammer")
                self._deactivate("Identity changed to FRIENDLY")
            return

        # ── Determine if this identity label warrants jamming ─────────────
        should_jam = False
        if identity is not None:
            label = identity.get("label", "UNKNOWN")
            if label == "NON_FRIENDLY":
                should_jam = True
            elif label == "UNKNOWN" and self._unknown_threat:
                should_jam = True

        # ── Jam gate: all three conditions must be true ───────────────────
        if should_jam and fusion_fresh and not active and not in_cooldown:
            freq_hz = identity.get("raw", {}).get("freq_hz") if identity else None
            self._activate(
                reason=f"label={identity['label']}  drone_id='{identity.get('drone_id')}'",
                freq_hz=freq_hz,
            )
        elif not fusion_fresh and active:
            # Fusion dropped out — no longer confirmed → stop jamming
            logger.info("[Jammer] Fusion no longer fresh — deactivating")
            self._deactivate("Fusion confirmation expired")

    # ── Activation / deactivation ─────────────────────────────────────────────

    def _activate(self, reason: str, freq_hz=None):
        """Turn the jammer ON and start the safety shutoff timer."""
        with self._lock:
            self._jammer_active   = True
            self._last_activation_ts = time.monotonic()
            self._activation_start   = self._last_activation_ts

        _gpio.output(self._control_pin, True)   # GPIO HIGH = jammer ON

        logger.warning(
            f"[Jammer] *** ACTIVATED ***  reason='{reason}'  "
            f"freq={freq_hz}Hz  max_active={self._max_active}s"
        )

        # Publish event so dashboard/logs know jammer fired
        self._bus.publish("jammer.trigger", {
            "activate": True,
            "reason":   reason,
            "freq_hz":  freq_hz,
            "ts":       time.monotonic(),
        })

        # Safety shutoff thread: auto-turn-off after max_active_sec
        self._shutoff_thread = threading.Thread(
            target=self._auto_shutoff, daemon=True, name="JammerShutoff"
        )
        self._shutoff_thread.start()

    def _deactivate(self, reason: str):
        """Turn the jammer OFF."""
        with self._lock:
            if not self._jammer_active:
                return   # Already off — no-op
            self._jammer_active = False

        _gpio.output(self._control_pin, False)   # GPIO LOW = jammer OFF

        logger.info(f"[Jammer] Deactivated — reason='{reason}'")

        self._bus.publish("jammer.trigger", {
            "activate": False,
            "reason":   reason,
            "ts":       time.monotonic(),
        })

    def _auto_shutoff(self):
        """
        Runs in a daemon thread after activation.
        Turns the jammer off after max_active_sec regardless of other state.
        This is the final hardware safety interlock.
        """
        time.sleep(self._max_active)
        with self._lock:
            still_active = self._jammer_active
        if still_active:
            logger.warning(
                f"[Jammer] Auto-shutoff triggered after {self._max_active}s"
            )
            self._deactivate("Auto-shutoff (max_active_sec reached)")

    def cleanup(self):
        """Ensure jammer is OFF and GPIO is released on shutdown."""
        self._deactivate("System shutdown")
        _gpio.cleanup()
