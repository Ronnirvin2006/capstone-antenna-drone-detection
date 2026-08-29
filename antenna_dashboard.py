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
import csv
import subprocess
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from flask import Flask, jsonify, render_template

from modules.signal_detection.ml_detector import TrainedSignalClassifier, normalize_db_row


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
    ml_result: dict = field(default_factory=dict)


class DetectionBackend:
    """Produces waterfall rows until the real HackRF backend is connected."""

    def __init__(self, bands: list[AntennaBand], bins: int = 160, demo_signals: bool = False):
        self.bands = bands
        self.bins = bins
        self.demo_signals = demo_signals
        self.scan_index = 0
        self.running = False
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.started_at = time.monotonic()
        self.classifier = TrainedSignalClassifier()

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
                "mode": "demo" if self.demo_signals else "offline",
                "source": "synthetic demo generator" if self.demo_signals else "no SDR connected",
                "ml_enabled": True,
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

        peaks = []

        if self.demo_signals:
            peaks = self._add_demo_activity(row, band)

        band.rows.append(row)
        ml_result = self.classifier.classify(list(band.rows))
        suspicious = bool(ml_result["drone_like"] and peaks)
        band.peaks = peaks[:4]
        band.suspicious = suspicious
        band.drones_estimate = min(3, max(1, len(peaks) // 2)) if suspicious else 0
        band.last_scan_ts = time.time()
        band.status = "suspicious" if suspicious else "offline scan"
        band.ml_result = ml_result

    def _add_demo_activity(self, row: list[float], band: AntennaBand) -> list[dict]:
        peaks = []
        elapsed = time.monotonic() - self.started_at
        for idx, freq in enumerate(band.watch_mhz):
            signal_probability = 0.08
            if band.key == "vivaldi":
                signal_probability = 0.28
            if band.key == "yagi":
                signal_probability = 0.14

            hopping_phase = random.random() + 0.35 * ((elapsed * (idx + 1)) % 1)
            is_active = hopping_phase > 0.78 or random.random() < signal_probability
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
                    "type": "demo hopping burst" if len(peaks) % 2 == 0 else "demo new carrier",
                }
            )
        return peaks

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
            "ml_result": band.ml_result,
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


class HackRFVivaldiBackend(DetectionBackend):
    """Reads real spectrum rows from hackrf_sweep for the Vivaldi antenna."""

    def __init__(
        self,
        bands: list[AntennaBand],
        bins: int = 160,
        start_mhz: int = 2400,
        stop_mhz: int = 6000,
        bin_width_hz: int = 5_000_000,
        lna_gain: int = 24,
        vga_gain: int = 32,
        amp: int = 0,
    ):
        super().__init__(bands, bins=bins, demo_signals=False)
        self.start_mhz = start_mhz
        self.stop_mhz = stop_mhz
        self.bin_width_hz = bin_width_hz
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.amp = amp
        self.process: subprocess.Popen[str] | None = None
        self.vivaldi = next(band for band in self.bands if band.key == "vivaldi")

    def snapshot(self) -> dict:
        data = super().snapshot()
        data["mode"] = "live"
        data["source"] = "HackRF One via hackrf_sweep, Vivaldi antenna"
        data["active_antenna"] = "vivaldi"
        return data

    def stop(self) -> None:
        self.running = False
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.thread:
            self.thread.join(timeout=2)

    def _loop(self) -> None:
        cmd = [
            "hackrf_sweep",
            "-f",
            f"{self.start_mhz}:{self.stop_mhz}",
            "-w",
            str(self.bin_width_hz),
            "-l",
            str(self.lna_gain),
            "-g",
            str(self.vga_gain),
            "-a",
            str(self.amp),
        ]

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        current_row: list[float | None] = [None] * self.bins
        last_flush = time.monotonic()

        assert self.process.stdout is not None
        while self.running:
            line = self.process.stdout.readline()
            if not line:
                if self.process.poll() is not None:
                    break
                time.sleep(0.05)
                continue

            parsed = self._parse_sweep_line(line)
            if parsed is None:
                continue

            hz_low, hz_high, bin_width_hz, powers = parsed
            self._merge_sweep_segment(current_row, hz_low, hz_high, bin_width_hz, powers)

            now = time.monotonic()
            if now - last_flush >= 0.45:
                self._publish_vivaldi_row(current_row)
                current_row = [None] * self.bins
                last_flush = now

        self._mark_error_if_needed()

    def _parse_sweep_line(self, line: str) -> tuple[int, int, float, list[float]] | None:
        try:
            fields = next(csv.reader([line.strip()]))
            if len(fields) < 7:
                return None
            hz_low = int(fields[2])
            hz_high = int(fields[3])
            bin_width_hz = float(fields[4])
            powers = [float(value) for value in fields[6:]]
            return hz_low, hz_high, bin_width_hz, powers
        except (ValueError, csv.Error):
            return None

    def _merge_sweep_segment(
        self,
        row: list[float | None],
        hz_low: int,
        hz_high: int,
        bin_width_hz: float,
        powers: list[float],
    ) -> None:
        del hz_high
        for index, power_db in enumerate(powers):
            freq_mhz = (hz_low + index * bin_width_hz) / 1_000_000
            if not (self.vivaldi.low_mhz <= freq_mhz <= self.vivaldi.high_mhz):
                continue
            bin_index = self._freq_to_bin(freq_mhz, self.vivaldi.low_mhz, self.vivaldi.high_mhz)
            previous = row[bin_index]
            row[bin_index] = power_db if previous is None else max(previous, power_db)

    def _publish_vivaldi_row(self, raw_row: list[float | None]) -> None:
        row = normalize_db_row(raw_row)
        peaks = self._find_peaks(row, self.vivaldi)
        with self.lock:
            self.vivaldi.rows.append(row)
            ml_result = self.classifier.classify(list(self.vivaldi.rows))
            suspicious = bool(ml_result["drone_like"] and peaks)
            self.vivaldi.peaks = peaks[:6]
            self.vivaldi.suspicious = suspicious
            self.vivaldi.drones_estimate = min(3, max(1, len(peaks) // 2)) if suspicious else 0
            self.vivaldi.last_scan_ts = time.time()
            self.vivaldi.status = "live suspicious" if suspicious else "live scan"
            self.vivaldi.ml_result = ml_result

            for band in self.bands:
                if band.key != "vivaldi":
                    band.status = "not connected"
                    band.ml_result = {"label": "offline", "confidence": 1.0, "drone_like": False}

    def _find_peaks(self, row: list[float], band: AntennaBand) -> list[dict]:
        threshold = 0.58
        peaks = []
        in_cluster = False
        cluster_start = 0

        for index, value in enumerate(row + [0.0]):
            if value >= threshold and not in_cluster:
                in_cluster = True
                cluster_start = index
            elif value < threshold and in_cluster:
                cluster_end = index - 1
                center = (cluster_start + cluster_end) // 2
                freq_mhz = band.low_mhz + (center / max(1, len(row) - 1)) * (band.high_mhz - band.low_mhz)
                peak_power = max(row[cluster_start : cluster_end + 1])
                peaks.append(
                    {
                        "freq_mhz": round(freq_mhz, 3),
                        "power_db": round(self._denormalize_db(peak_power), 1),
                        "type": "new/hopping energy",
                    }
                )
                in_cluster = False

        return peaks

    def _denormalize_db(self, value: float) -> float:
        return value * 65.0 - 105.0

    def _mark_error_if_needed(self) -> None:
        with self.lock:
            if self.process and self.process.returncode not in (None, 0):
                self.vivaldi.status = "hackrf error"


def create_app(demo_signals: bool = False, hackrf_vivaldi: bool = False) -> Flask:
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

    if hackrf_vivaldi:
        backend = HackRFVivaldiBackend(bands)
    else:
        backend = DetectionBackend(bands, demo_signals=demo_signals)
    backend.start()

    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("dashboard.html")

    @app.get("/api/state")
    def state():
        return jsonify(backend.snapshot())

    @app.get("/api/health")
    def health():
        return jsonify({
            "ok": True,
            "mode": "live" if hackrf_vivaldi else "demo" if demo_signals else "offline",
            "ml_enabled": True,
        })

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the antenna detection dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--demo-signals",
        action="store_true",
        help="Show synthetic drone-like signals for UI demonstration",
    )
    parser.add_argument(
        "--hackrf-vivaldi",
        action="store_true",
        help="Use real HackRF sweep data for the Vivaldi 2-6 GHz slot",
    )
    args = parser.parse_args()

    app = create_app(demo_signals=args.demo_signals, hackrf_vivaldi=args.hackrf_vivaldi)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
