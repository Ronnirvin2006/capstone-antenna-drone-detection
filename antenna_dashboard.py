"""
Gqrx-style RF waterfall monitor for the antenna drone-detection project.

This version is intentionally an RF viewer, not a drone classifier. It scans
fixed useful bands with HackRF One and displays waterfall panes for:

  433 MHz, 915 MHz, GPS/GNSS, 2.4 GHz, and 5.8 GHz.

Detection/classification will be added later after the RF viewing stage is
stable and the MUX wiring is final.
"""

from __future__ import annotations

import argparse
import csv
import random
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from flask import Flask, jsonify, render_template

from modules.signal_detection.ml_detector import normalize_db_row


@dataclass
class MonitorBand:
    key: str
    label: str
    antenna: str
    low_mhz: float
    high_mhz: float
    markers_mhz: list[float]
    rows: Deque[list[float]] = field(default_factory=lambda: deque(maxlen=180))
    peaks: list[dict] = field(default_factory=list)
    noise_floor_db: float = -100.0
    peak_power_db: float = -100.0
    last_scan_ts: float = 0.0
    status: str = "waiting"


class OfflineWaterfallBackend:
    """Shows quiet instrument panes when HackRF live mode is not selected."""

    def __init__(self, bands: list[MonitorBand], bins: int = 220):
        self.bands = bands
        self.bins = bins
        self.running = False
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.active_key = bands[0].key
        for band in self.bands:
            for _ in range(band.rows.maxlen):
                band.rows.append(self._quiet_row())

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
            return {
                "mode": "offline",
                "source": "HackRF live scan not started",
                "active_band": self.active_key,
                "bands": [band_payload(band) for band in self.bands],
            }

    def _loop(self) -> None:
        index = 0
        while self.running:
            with self.lock:
                band = self.bands[index]
                band.rows.append(self._quiet_row())
                band.status = "offline"
                band.peaks = []
                band.last_scan_ts = time.time()
                self.active_key = band.key
                index = (index + 1) % len(self.bands)
            time.sleep(0.5)

    def _quiet_row(self) -> list[float]:
        return [random.uniform(0.03, 0.12) for _ in range(self.bins)]


class HackRFSweepBackend(OfflineWaterfallBackend):
    """Feeds all monitor panes from one continuous hackrf_sweep process."""

    def __init__(
        self,
        bands: list[MonitorBand],
        bins: int = 220,
        sweep_start_mhz: int = 420,
        sweep_stop_mhz: int = 5900,
        bin_width_hz: int = 2_000_000,
        lna_gain: int = 16,
        vga_gain: int = 20,
        amp: int = 0,
        avg_alpha: float = 0.35,
    ):
        super().__init__(bands, bins=bins)
        self.sweep_start_mhz = sweep_start_mhz
        self.sweep_stop_mhz = sweep_stop_mhz
        self.bin_width_hz = bin_width_hz
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.amp = amp
        self.avg_alpha = avg_alpha
        self.process: subprocess.Popen[str] | None = None
        self.error_message = ""
        self._raw_rows: dict[str, list[float | None]] = {
            band.key: [None] * self.bins for band in self.bands
        }
        self._smooth_rows: dict[str, list[float] | None] = {
            band.key: None for band in self.bands
        }

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "mode": "live",
                "source": (
                    self.error_message
                    or f"HackRF sweep {self.sweep_start_mhz}-{self.sweep_stop_mhz} MHz, "
                    f"LNA {self.lna_gain} dB, VGA {self.vga_gain} dB, amp {self.amp}"
                ),
                "active_band": self.active_key,
                "bands": [band_payload(band) for band in self.bands],
            }

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
            f"{self.sweep_start_mhz}:{self.sweep_stop_mhz}",
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

        last_flush = time.monotonic()
        assert self.process.stdout is not None
        while self.running:
            line = self.process.stdout.readline()
            if not line:
                if self.process.poll() is not None:
                    break
                time.sleep(0.05)
                continue

            parsed = parse_sweep_line(line)
            if parsed is None:
                continue
            hz_low, bin_width_hz, powers = parsed
            self._merge_segment(hz_low, bin_width_hz, powers)

            now = time.monotonic()
            if now - last_flush >= 0.35:
                self._publish_rows()
                last_flush = now

        stderr = ""
        if self.process and self.process.stderr:
            stderr = self.process.stderr.read().strip()
        with self.lock:
            self.error_message = stderr.splitlines()[0] if stderr else "HackRF sweep stopped"
            for band in self.bands:
                band.status = "hackrf error"

    def _merge_segment(self, hz_low: int, bin_width_hz: float, powers: list[float]) -> None:
        for index, power_db in enumerate(powers):
            freq_mhz = (hz_low + index * bin_width_hz) / 1_000_000
            for band in self.bands:
                if not (band.low_mhz <= freq_mhz <= band.high_mhz):
                    continue
                bin_index = freq_to_bin(freq_mhz, band.low_mhz, band.high_mhz, self.bins)
                row = self._raw_rows[band.key]
                previous = row[bin_index]
                row[bin_index] = power_db if previous is None else max(previous, power_db)

    def _publish_rows(self) -> None:
        with self.lock:
            for band in self.bands:
                raw = self._raw_rows[band.key]
                normalized = normalize_db_row(raw)
                previous = self._smooth_rows[band.key]
                if previous is not None:
                    normalized = [
                        previous[i] * (1 - self.avg_alpha) + value * self.avg_alpha
                        for i, value in enumerate(normalized)
                    ]
                self._smooth_rows[band.key] = normalized
                band.rows.append(normalized)
                band.peaks = find_peaks(normalized, band)
                valid_db = [value for value in raw if value is not None]
                if valid_db:
                    band.noise_floor_db = percentile(sorted(valid_db), 20)
                    band.peak_power_db = max(valid_db)
                    band.status = "live"
                    band.last_scan_ts = time.time()
                    self.active_key = band.key
                else:
                    band.status = "waiting"
                self._raw_rows[band.key] = [None] * self.bins


def parse_sweep_line(line: str) -> tuple[int, float, list[float]] | None:
    try:
        fields = next(csv.reader([line.strip()]))
        if len(fields) < 7:
            return None
        hz_low = int(fields[2])
        bin_width_hz = float(fields[4])
        powers = [float(value) for value in fields[6:]]
        return hz_low, bin_width_hz, powers
    except (ValueError, csv.Error):
        return None


def make_bands() -> list[MonitorBand]:
    return [
        MonitorBand(
            key="mhz433",
            label="433 MHz",
            antenna="Yagi-Uda 433 MHz",
            low_mhz=420,
            high_mhz=450,
            markers_mhz=[433.05, 433.92, 434.45],
        ),
        MonitorBand(
            key="mhz915",
            label="915 MHz",
            antenna="LPDA 915 MHz-1.6 GHz",
            low_mhz=902,
            high_mhz=928,
            markers_mhz=[915.0, 918.0, 922.0],
        ),
        MonitorBand(
            key="gps",
            label="GPS / GNSS",
            antenna="LPDA 915 MHz-1.6 GHz",
            low_mhz=1160,
            high_mhz=1605,
            markers_mhz=[1176.45, 1227.60, 1575.42],
        ),
        MonitorBand(
            key="ghz24",
            label="2.4 GHz",
            antenna="Vivaldi 2-6 GHz",
            low_mhz=2400,
            high_mhz=2485,
            markers_mhz=[2412, 2437, 2462, 2480],
        ),
        MonitorBand(
            key="ghz58",
            label="5.8 GHz",
            antenna="Vivaldi 2-6 GHz",
            low_mhz=5725,
            high_mhz=5875,
            markers_mhz=[5745, 5785, 5805, 5825],
        ),
    ]


def band_payload(band: MonitorBand) -> dict:
    return {
        "key": band.key,
        "label": band.label,
        "antenna": band.antenna,
        "range": f"{band.low_mhz:g}-{band.high_mhz:g} MHz",
        "low_mhz": band.low_mhz,
        "high_mhz": band.high_mhz,
        "markers_mhz": band.markers_mhz,
        "rows": list(band.rows),
        "peaks": band.peaks,
        "noise_floor_db": round(band.noise_floor_db, 1),
        "peak_power_db": round(band.peak_power_db, 1),
        "last_scan_ts": band.last_scan_ts,
        "status": band.status,
    }


def find_peaks(row: list[float], band: MonitorBand) -> list[dict]:
    threshold = 0.72
    peaks = []
    in_cluster = False
    cluster_start = 0
    for index, value in enumerate(row + [0.0]):
        if value >= threshold and not in_cluster:
            in_cluster = True
            cluster_start = index
        elif value < threshold and in_cluster:
            cluster_end = index - 1
            center = max(cluster_start, min(cluster_end, (cluster_start + cluster_end) // 2))
            freq_mhz = band.low_mhz + (center / max(1, len(row) - 1)) * (band.high_mhz - band.low_mhz)
            strength = max(row[cluster_start : cluster_end + 1])
            peaks.append({"freq_mhz": round(freq_mhz, 3), "strength": round(strength, 2)})
            in_cluster = False
    return sorted(peaks, key=lambda item: item["strength"], reverse=True)[:6]


def freq_to_bin(freq_mhz: float, low_mhz: float, high_mhz: float, bins: int) -> int:
    clamped = min(max(freq_mhz, low_mhz), high_mhz)
    return int((clamped - low_mhz) / (high_mhz - low_mhz) * (bins - 1))


def percentile(ordered: list[float], percent: float) -> float:
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * percent / 100.0
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    blend = index - lower
    return ordered[lower] * (1 - blend) + ordered[upper] * blend


def create_app(live: bool = False, args: argparse.Namespace | None = None) -> Flask:
    bands = make_bands()
    if live:
        assert args is not None
        backend = HackRFSweepBackend(
            bands,
            sweep_start_mhz=args.sweep_start,
            sweep_stop_mhz=args.sweep_stop,
            bin_width_hz=args.bin_width,
            lna_gain=args.lna,
            vga_gain=args.vga,
            amp=args.amp,
            avg_alpha=args.avg_alpha,
        )
    else:
        backend = OfflineWaterfallBackend(bands)
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
        return jsonify({"ok": True, "mode": "live" if live else "offline"})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the RF waterfall dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--live", action="store_true", help="Read real spectrum data from HackRF")
    parser.add_argument("--hackrf-vivaldi", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sweep-start", type=int, default=420, help="Sweep start in MHz")
    parser.add_argument("--sweep-stop", type=int, default=5900, help="Sweep stop in MHz")
    parser.add_argument("--bin-width", type=int, default=2_000_000, help="hackrf_sweep bin width in Hz")
    parser.add_argument("--lna", type=int, default=16, help="HackRF LNA/IF gain in dB")
    parser.add_argument("--vga", type=int, default=20, help="HackRF VGA/baseband gain in dB")
    parser.add_argument("--amp", type=int, default=0, choices=[0, 1], help="HackRF RF amp, 0 off or 1 on")
    parser.add_argument("--avg-alpha", type=float, default=0.35, help="Waterfall smoothing, 0.1 slow to 1.0 raw")
    args = parser.parse_args()

    app = create_app(live=args.live or args.hackrf_vivaldi, args=args)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
