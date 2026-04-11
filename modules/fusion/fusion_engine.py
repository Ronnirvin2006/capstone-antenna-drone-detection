"""
fusion_engine.py  —  Multi-sensor fusion for drone presence confirmation.

WHAT THIS DOES:
  Listens to both the SDR pipeline and the visual pipeline on the event bus.
  Within a sliding time window (default 2 seconds), it checks:
    - Did the SDR report a detection?
    - Did the visual pipeline also report a detection?

  If BOTH fired within the window (require_both=True in config), the fusion
  engine publishes a "fusion.confirmed" event with the best range estimate.

  If the two range estimates disagree by more than range_disagreement_threshold_m
  it logs a warning but still confirms (one sensor could be wrong — we flag it
  rather than block jamming).

WHY FUSION IS SEPARATE FROM THE CLASSIFIER:
  The identity classifier runs independently and continuously on BLE data.
  Fusion only cares about PHYSICAL PRESENCE — is there actually a drone in
  the sky, not just a BLE broadcast that could be replayed or spoofed.

  The jammer action decision requires BOTH:
    - fusion.confirmed  (physical presence verified)
    - identity.result   (non-friendly / unknown classification)

  Splitting these into two independent streams is a deliberate safety design.
"""

import time
import threading
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class FusionEngine:
    """
    Sensor fusion: correlates SDR and visual detections.

    Args:
        cfg: The 'fusion' section from system_config.yaml.
        bus: Shared EventBus instance.
    """

    def __init__(self, cfg: dict, bus):
        self._cfg = cfg
        self._bus = bus

        self._window     = cfg["window_sec"]
        self._req_both   = cfg.get("require_both", True)
        self._range_tol  = cfg.get("range_disagreement_threshold_m", 30)

        # Latest events from each sensor (None = no recent data)
        self._last_sdr:    Optional[dict] = None
        self._last_visual: Optional[dict] = None

        # Protect concurrent access from the SDR/visual callback threads
        self._lock = threading.Lock()

        # Subscribe to both detection streams
        self._bus.subscribe("sdr.detection",    self._on_sdr)
        self._bus.subscribe("visual.detection", self._on_visual)

        logger.info(
            f"[Fusion] Initialised — window={self._window}s  "
            f"require_both={self._req_both}  "
            f"range_tol={self._range_tol}m"
        )

    # ── Event callbacks ───────────────────────────────────────────────────────

    def _on_sdr(self, topic: str, payload: dict):
        """
        Receive an SDR detection event.
        Update the stored SDR reading and attempt correlation.
        """
        with self._lock:
            self._last_sdr = payload
        logger.debug(
            f"[Fusion] SDR event: range={payload.get('range_m'):.1f}m  "
            f"rssi={payload.get('rssi_db'):.1f}dB"
        )
        self._try_confirm()

    def _on_visual(self, topic: str, payload: dict):
        """
        Receive a visual detection event.
        Update the stored visual reading and attempt correlation.
        """
        with self._lock:
            self._last_visual = payload
        logger.debug(
            f"[Fusion] Visual event: range≈{payload.get('range_est_m'):.1f}m  "
            f"conf={payload.get('confidence'):.2f}"
        )
        self._try_confirm()

    # ── Fusion logic ──────────────────────────────────────────────────────────

    def _try_confirm(self):
        """
        Check if both sensors have fired within the sliding window.
        If so, publish a fusion.confirmed event.

        Called every time either sensor fires — O(1) complexity, always fast.
        """
        now = time.monotonic()

        with self._lock:
            sdr    = self._last_sdr
            visual = self._last_visual

        # ── Staleness check ───────────────────────────────────────────────
        # Discard any reading older than the window
        sdr_fresh    = (sdr    is not None and (now - sdr["ts"])    <= self._window)
        visual_fresh = (visual is not None and (now - visual["ts"]) <= self._window)

        # ── Gate on require_both ─────────────────────────────────────────
        if self._req_both:
            if not (sdr_fresh and visual_fresh):
                logger.debug(
                    f"[Fusion] Not confirmed — "
                    f"sdr_fresh={sdr_fresh}  visual_fresh={visual_fresh}"
                )
                return
        else:
            # Either sensor is sufficient (less strict mode)
            if not (sdr_fresh or visual_fresh):
                return

        # ── Both sensors agree — compute best range ───────────────────────
        sdr_range    = sdr["range_m"]       if sdr_fresh    else None
        visual_range = visual["range_est_m"] if visual_fresh else None

        if sdr_range is not None and visual_range is not None:
            disagreement = abs(sdr_range - visual_range)
            if disagreement > self._range_tol:
                logger.warning(
                    f"[Fusion] Range disagreement! "
                    f"SDR={sdr_range:.1f}m  Visual={visual_range:.1f}m  "
                    f"Δ={disagreement:.1f}m (>{self._range_tol}m) — "
                    f"trusting SDR, flagging"
                )
            # Primary source: SDR is more accurate; visual is a cross-check
            best_range = sdr_range
        elif sdr_range is not None:
            best_range = sdr_range
        else:
            best_range = visual_range

        logger.info(
            f"[Fusion] CONFIRMED — range={best_range:.1f}m  "
            f"sdr_fresh={sdr_fresh}  visual_fresh={visual_fresh}"
        )

        # ── Publish the confirmed event ───────────────────────────────────
        self._bus.publish("fusion.confirmed", {
            "range_m":       best_range,
            "sdr_fresh":     sdr_fresh,
            "visual_fresh":  visual_fresh,
            "sdr_range":     sdr_range,
            "visual_range":  visual_range,
            "ts":            now,
        })
