"""
Interactive antenna-based drone signal detection dashboard.

This first version is intentionally receive-only. It models the hardware path:

    Yagi / LPDA / Vivaldi -> RF MUX -> HackRF One -> system

The app shows three waterfall panes, one per antenna band. Because one HackRF
behind a MUX can only observe one antenna at a time, panes update in a
round-robin scan pattern. The current backend uses a simulation generator so
the UI can be developed and tested before the live HackRF integration is added.
"""

from __future__ import annotations

import argparse
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from flask import Flask, jsonify, render_template


@dataclass
class AntennaBand:
    key: str
    label: str
    antenna: str
    mux_port: str
    low_mhz: float
    high_mhz: float
    watch_mhz: list[float]
    rows: Deque[list[float]] = field(default_factory=lambda: deque(maxlen=140))
    peaks: list[dict] = field(default_factory=list)
    suspicious: bool = False
    drones_estimate: int = 0
    last_scan_ts: float = 0.0
    status: str = "waiting"


class DetectionSimulator:
    """Produces waterfall rows shaped like RF activity for UI development."""

    def __init__(self, bands: list[AntennaBand], bins: int = 160):
        self.bands = bands
        self.bins = bins
        self.scan_index = 0
        self.running = False
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.started_at = time.monotonic()

        for band in self.bands:
            for _ in range(band.rows.maxlen):
                band.rows.append(self._noise_row())

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)

    def snapshot(self) -> dict:
        with self.lock:
            active = self.bands[self.scan_index].key
            detections = [
                {
                    "antenna": band.label,
                    "estimated_drones": band.drones_estimate,
                    "peaks": band.peaks,
                }
                for band in self.bands
                if band.suspicious
            ]
            return {
                "active_antenna": active,
                "detected_count": sum(item["estimated_drones"] for item in detections),
                "alert": bool(detections),
                "detections": detections,
                "bands": [self._band_payload(band) for band in self.bands],
            }

    def _loop(self) -> None:
        while self.running:
            with self.lock:
                band = self.bands[self.scan_index]
                self._scan_band(band)
                self.scan_index = (self.scan_index + 1) % len(self.bands)
            time.sleep(0.45)

    def _scan_band(self, band: AntennaBand) -> None:
        row = self._noise_row()
        elapsed = time.monotonic() - self.started_at

        peaks = []
        suspicious = False

        for idx, freq in enumerate(band.watch_mhz):
            signal_probability = 0.12
            if band.key == "vivaldi":
                signal_probability = 0.33
            if band.key == "yagi":
                signal_probability = 0.18

            hopping_phase = math.sin(elapsed * (0.7 + idx * 0.17) + idx)
            is_active = hopping_phase > 0.62 or random.random() < signal_probability
            if not is_active:
                continue

            bin_index = self._freq_to_bin(freq, band.low_mhz, band.high_mhz)
            width = random.randint(2, 7)
            strength = random.uniform(0.58, 0.96)
            self._add_signal(row, bin_index, width, strength)
            peaks.append(
                {
                    "freq_mhz": round(freq + random.uniform(-0.35, 0.35), 3),
                    "power_db": round(-82 + strength * 45, 1),
                    "type": "hopping burst" if len(peaks) % 2 == 0 else "new carrier",
                }
            )

        if len(peaks) >= 2:
            suspicious = True

        band.rows.append(row)
        band.peaks = peaks[:4]
        band.suspicious = suspicious
        band.drones_estimate = min(3, max(1, len(peaks) // 2)) if suspicious else 0
        band.last_scan_ts = time.time()
        band.status = "suspicious" if suspicious else "scanning"

    def _band_payload(self, band: AntennaBand) -> dict:
        return {
            "key": band.key,
            "label": band.label,
            "antenna": band.antenna,
            "mux_port": band.mux_port,
            "range": f"{band.low_mhz:g}-{band.high_mhz:g} MHz",
            "low_mhz": band.low_mhz,
            "high_mhz": band.high_mhz,
            "rows": list(band.rows),
            "peaks": band.peaks,
            "suspicious": band.suspicious,
            "drones_estimate": band.drones_estimate,
            "last_scan_ts": band.last_scan_ts,
            "status": band.status,
        }

    def _noise_row(self) -> list[float]:
        return [random.uniform(0.08, 0.22) for _ in range(self.bins)]

    def _add_signal(self, row: list[float], center: int, width: int, strength: float) -> None:
        for offset in range(-width, width + 1):
            index = center + offset
            if 0 <= index < len(row):
                taper = 1.0 - abs(offset) / (width + 1)
                row[index] = max(row[index], strength * taper + random.uniform(0.05, 0.18))

    def _freq_to_bin(self, freq_mhz: float, low: float, high: float) -> int:
        clamped = min(max(freq_mhz, low), high)
        return int((clamped - low) / (high - low) * (self.bins - 1))


def create_app() -> Flask:
    bands = [
        AntennaBand(
            key="yagi",
            label="Yagi-Uda 433",
            antenna="433 MHz directional Yagi-Uda",
            mux_port="RF1",
            low_mhz=420,
            high_mhz=450,
            watch_mhz=[433.05, 433.92, 434.45],
        ),
        AntennaBand(
            key="lpda",
            label="LPDA 915-1600",
            antenna="915 MHz to 1.6 GHz LPDA",
            mux_port="RF2",
            low_mhz=915,
            high_mhz=1600,
            watch_mhz=[915.0, 922.5, 1227.6, 1575.42],
        ),
        AntennaBand(
            key="vivaldi",
            label="Vivaldi 2-6 GHz",
            antenna="2 GHz to 6 GHz Vivaldi",
            mux_port="RF3",
            low_mhz=2400,
            high_mhz=6000,
            watch_mhz=[2412, 2437, 2462, 5725, 5805],
        ),
    ]

    simulator = DetectionSimulator(bands)
    simulator.start()

    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("dashboard.html")

    @app.get("/api/state")
    def state():
        return jsonify(simulator.snapshot())

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "mode": "simulation"})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the antenna detection dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    app = create_app()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
