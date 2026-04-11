"""
event_bus.py  —  Central pub/sub message queue for the anti-drone system.

WHY THIS EXISTS:
  Every module (SDR, visual, identity, fusion, jammer) runs in its own
  process or thread.  They must NOT import each other directly — that
  creates tight coupling and makes it hard to swap out one module later.

  Instead, every module:
    1. Publishes events by calling  bus.publish(topic, payload)
    2. Subscribes to events by calling  bus.subscribe(topic, callback)

  The bus is a simple wrapper around Python's multiprocessing.Queue so
  it is safe to use across process boundaries.

TOPICS USED IN THIS PROJECT:
  "sdr.detection"     — SDR/FMCW detected a signal: {range_m, rssi_db, freq_hz, ts}
  "visual.detection"  — YOLO found a drone: {bbox, confidence, range_est_m, ts}
  "identity.result"   — OpenDroneID classified: {drone_id, label, rssi, ts, raw}
  "fusion.confirmed"  — Fusion engine confirmed physical presence: {range_m, ts}
  "mux.lock"          — MUX controller should lock/unlock antenna: {antenna, lock}
  "jammer.trigger"    — Jammer should activate/deactivate: {activate, freq_hz, reason}
  "system.alert"      — Human-readable system alert logged to dashboard: {msg, level}
"""

import queue
import threading
import time
import logging
from typing import Callable, Dict, List, Any

logger = logging.getLogger(__name__)


class EventBus:
    """
    Thread-safe in-process pub/sub event bus.

    For multi-PROCESS usage (SDR, YOLO in separate processes), use the
    MultiprocessBus subclass below which wraps multiprocessing.Queue.
    """

    def __init__(self):
        # _subscribers maps topic -> list of callback functions
        self._subscribers: Dict[str, List[Callable]] = {}
        # Lock protects subscriber registration which can happen from any thread
        self._lock = threading.Lock()

    def subscribe(self, topic: str, callback: Callable[[str, Any], None]):
        """
        Register a callback for a topic.

        callback signature:  callback(topic: str, payload: dict)

        The callback is called synchronously inside publish(), so keep it
        fast.  For heavy work, push into another queue inside the callback.
        """
        with self._lock:
            if topic not in self._subscribers:
                self._subscribers[topic] = []
            self._subscribers[topic].append(callback)
        logger.debug(f"[EventBus] Subscribed '{callback.__qualname__}' to '{topic}'")

    def publish(self, topic: str, payload: Any):
        """
        Publish an event to all subscribers of that topic.

        payload should always be a plain dict so it serialises cleanly
        to JSON for logging.  Never put object references in payload.
        """
        # Stamp every event with a monotonic timestamp at publish time
        if isinstance(payload, dict) and "ts" not in payload:
            payload["ts"] = time.monotonic()

        with self._lock:
            callbacks = list(self._subscribers.get(topic, []))

        if not callbacks:
            # Nothing subscribed — not an error, just debug-log it
            logger.debug(f"[EventBus] No subscribers for topic '{topic}'")
            return

        for cb in callbacks:
            try:
                cb(topic, payload)
            except Exception as exc:
                # Never let one bad subscriber kill the whole bus
                logger.error(f"[EventBus] Subscriber {cb.__qualname__} raised: {exc}")

    def unsubscribe(self, topic: str, callback: Callable):
        """Remove a specific callback from a topic."""
        with self._lock:
            if topic in self._subscribers:
                self._subscribers[topic] = [
                    cb for cb in self._subscribers[topic] if cb is not callback
                ]


class QueueBridge:
    """
    Bridges a multiprocessing.Queue into the EventBus so that child
    processes can publish events back to the main process.

    Usage in child process:
        bridge = QueueBridge(queue)
        bridge.publish("sdr.detection", {...})

    Usage in main process:
        bus = EventBus()
        bridge = QueueBridge(queue)
        bridge.start_listener(bus)   # spawns a daemon thread
    """

    def __init__(self, mp_queue):
        self._q = mp_queue

    def publish(self, topic: str, payload: dict):
        """Called from child process — puts (topic, payload) on the queue."""
        if "ts" not in payload:
            payload["ts"] = time.monotonic()
        self._q.put((topic, payload), block=False)

    def start_listener(self, bus: EventBus):
        """
        Starts a daemon thread in the MAIN process that drains the queue
        and re-publishes events on the local EventBus so all subscribers
        receive them normally.
        """
        def _drain():
            while True:
                try:
                    topic, payload = self._q.get(timeout=0.05)
                    bus.publish(topic, payload)
                except queue.Empty:
                    pass
                except Exception as exc:
                    logger.error(f"[QueueBridge] drain error: {exc}")

        t = threading.Thread(target=_drain, daemon=True, name="QueueBridgeDrain")
        t.start()
        logger.info("[QueueBridge] Listener thread started")


# ---------------------------------------------------------------------------
# Module-level singleton so everyone imports the same bus instance
# ---------------------------------------------------------------------------
bus = EventBus()
