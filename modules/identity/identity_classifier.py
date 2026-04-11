"""
identity_classifier.py  —  OpenDroneID serial reader + drone classifier.

WHAT THIS DOES:
  1. Opens the serial port where the ESP32 sends its parsed JSON lines.
  2. Reads one JSON line per BLE advertisement sighting.
  3. Classifies the drone as FRIENDLY, NON_FRIENDLY, or UNKNOWN using:
       a. Friendly ID whitelist (JSON files in data/friendly_ids/).
       b. Operator ID whitelist.
       c. Fallback: unknown = threat (configurable).
  4. Publishes "identity.result" events on the event bus.

THIS MODULE IS INDEPENDENT OF FUSION:
  Classification runs on EVERY BLE packet the ESP32 receives, regardless
  of whether the SDR or camera has confirmed physical presence.
  The jammer will only fire once the fusion engine ALSO confirms presence.

FRIENDLY ID FILES:
  Put JSON files in data/friendly_ids/ like:
    {
      "drone_ids":    ["SN-12345-XYZ", "SN-ABCDE-001"],
      "operator_ids": ["GBR-OP-MYCOMPANY01"]
    }
  Multiple files are merged at startup.

SIMULATION MODE:
  If pyserial is not installed or the port can't be opened, this module
  generates synthetic BLE events on a 3-second timer.

DEPENDENCIES:
  pip install pyserial
"""

import json
import os
import time
import serial
import threading
import logging
from typing import Optional, Set

logger = logging.getLogger(__name__)

try:
    import serial as pyserial
    _HAS_SERIAL = True
except ImportError:
    logger.warning("[Identity] pyserial not found — simulation mode")
    _HAS_SERIAL = False


class DroneLabel:
    """Possible classification outcomes."""
    FRIENDLY     = "FRIENDLY"
    NON_FRIENDLY = "NON_FRIENDLY"
    UNKNOWN      = "UNKNOWN"


class IdentityClassifier:
    """
    Reads OpenDroneID data from the ESP32 serial port and classifies drones.

    Args:
        cfg: The 'identity' section from system_config.yaml.
        bus: Shared EventBus instance.
    """

    def __init__(self, cfg: dict, bus):
        self._cfg = cfg
        self._bus = bus

        self._port          = cfg["serial_port"]
        self._baud          = cfg["baud_rate"]
        self._friendly_dir  = cfg["friendly_ids_dir"]
        self._unknown_threat = cfg.get("unknown_is_threat", True)

        # Whitelists loaded from data/friendly_ids/*.json
        self._friendly_drone_ids:    Set[str] = set()
        self._friendly_operator_ids: Set[str] = set()

        self._serial: Optional[serial.Serial] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Load whitelists at init time
        self._load_friendly_ids()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        """Open serial port and start the reader thread."""
        self._open_serial()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="IdentityClassifier"
        )
        self._thread.start()
        logger.info(f"[Identity] Listening on {self._port} @ {self._baud} baud")

    def stop(self):
        """Stop the reader thread and close the serial port."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._serial and self._serial.is_open:
            self._serial.close()
        logger.info("[Identity] Stopped")

    def reload_friendly_ids(self):
        """Hot-reload the friendly ID lists without restarting."""
        self._load_friendly_ids()
        logger.info("[Identity] Friendly ID lists reloaded")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_friendly_ids(self):
        """
        Scan data/friendly_ids/*.json and merge all drone_ids and
        operator_ids into the in-memory whitelist sets.
        """
        self._friendly_drone_ids.clear()
        self._friendly_operator_ids.clear()

        if not os.path.isdir(self._friendly_dir):
            logger.warning(
                f"[Identity] Friendly IDs dir '{self._friendly_dir}' not found — "
                f"all drones will be NON_FRIENDLY unless dir is created"
            )
            return

        for fname in os.listdir(self._friendly_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(self._friendly_dir, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                self._friendly_drone_ids.update(
                    data.get("drone_ids", [])
                )
                self._friendly_operator_ids.update(
                    data.get("operator_ids", [])
                )
            except Exception as exc:
                logger.error(f"[Identity] Failed to load '{fpath}': {exc}")

        logger.info(
            f"[Identity] Loaded {len(self._friendly_drone_ids)} drone IDs "
            f"and {len(self._friendly_operator_ids)} operator IDs as friendly"
        )

    def _open_serial(self):
        """Attempt to open the serial port.  Falls back to simulation if unavailable."""
        if not _HAS_SERIAL:
            logger.warning("[Identity] No pyserial — simulation mode")
            return
        try:
            self._serial = serial.Serial(
                self._port,
                baudrate=self._baud,
                timeout=1.0,   # readline() timeout in seconds
            )
            logger.info(f"[Identity] Serial port {self._port} opened")
        except serial.SerialException as exc:
            logger.error(
                f"[Identity] Cannot open {self._port}: {exc} — falling back to simulation"
            )
            self._serial = None

    def _run_loop(self):
        """
        Reader loop.  Runs in a background thread.
        If a real serial port is open: reads lines from it.
        Otherwise: generates synthetic BLE packets every 3 seconds.
        """
        while self._running:
            if self._serial and self._serial.is_open:
                self._read_real_serial()
            else:
                self._simulate_ble_event()
                time.sleep(3)

    def _read_real_serial(self):
        """
        Read one line from the serial port.
        Each line is a JSON object from the ESP32 firmware.
        """
        try:
            raw_line = self._serial.readline()
            if not raw_line:
                return  # timeout — no data, loop again
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                return

            # Parse JSON
            try:
                packet = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(f"[Identity] Non-JSON line from ESP32: {line[:80]}")
                return

            # Skip status messages (they have "status" key, not "mac")
            if "status" in packet and "mac" not in packet:
                logger.debug(f"[Identity] ESP32 status: {packet['status']}")
                return

            self._process_packet(packet)

        except serial.SerialException as exc:
            logger.error(f"[Identity] Serial read error: {exc}")
            time.sleep(1)

    def _process_packet(self, packet: dict):
        """
        Classify one BLE sighting and publish the result.

        packet keys (from ESP32 firmware):
          mac, rssi, name, msg_type, drone_id, operator_id, ts_ms
        """
        drone_id    = packet.get("drone_id", "").strip()
        operator_id = packet.get("operator_id", "").strip()
        rssi        = packet.get("rssi", 0)
        mac         = packet.get("mac", "")
        msg_type    = packet.get("msg_type", "unknown")

        # ── Classification logic (priority order) ──────────────────────────
        # Priority 1: drone_id is in the friendly whitelist
        if drone_id and drone_id in self._friendly_drone_ids:
            label = DroneLabel.FRIENDLY
            reason = f"drone_id '{drone_id}' in whitelist"

        # Priority 2: operator_id is in the friendly whitelist
        elif operator_id and operator_id in self._friendly_operator_ids:
            label = DroneLabel.FRIENDLY
            reason = f"operator_id '{operator_id}' in whitelist"

        # Priority 3: non-empty ID but not in whitelist → non-friendly
        elif drone_id or operator_id:
            label = DroneLabel.NON_FRIENDLY
            reason = "ID present but not in friendly whitelist"

        # Priority 4: no ID at all → unknown
        else:
            label = (DroneLabel.NON_FRIENDLY
                     if self._unknown_threat else DroneLabel.UNKNOWN)
            reason = "no drone_id or operator_id in packet"

        logger.info(
            f"[Identity] {label}  mac={mac}  "
            f"drone_id='{drone_id}'  rssi={rssi} dB  reason='{reason}'"
        )

        # Publish result — this is what the action decision module listens to
        self._bus.publish("identity.result", {
            "drone_id":     drone_id,
            "operator_id":  operator_id,
            "mac":          mac,
            "label":        label,          # FRIENDLY / NON_FRIENDLY / UNKNOWN
            "rssi":         rssi,
            "msg_type":     msg_type,
            "reason":       reason,
            "raw":          packet,
            "ts":           time.monotonic(),
        })

    def _simulate_ble_event(self):
        """
        Publish a synthetic BLE event for testing without ESP32 hardware.
        Alternates between friendly and non-friendly every 6 seconds.
        """
        cycle = int(time.time() / 6) % 2
        if cycle == 0:
            packet = {
                "mac": "AA:BB:CC:DD:EE:01",
                "rssi": -65,
                "name": "SimDrone-A",
                "msg_type": "BasicID",
                "drone_id": "SIM-FRIENDLY-001",
                "operator_id": "GBR-OP-SIM",
                "ts_ms": 12345,
            }
        else:
            packet = {
                "mac": "AA:BB:CC:DD:EE:02",
                "rssi": -72,
                "name": "SimDrone-B",
                "msg_type": "BasicID",
                "drone_id": "SIM-UNKNOWN-999",
                "operator_id": "",
                "ts_ms": 12346,
            }
        self._process_packet(packet)
