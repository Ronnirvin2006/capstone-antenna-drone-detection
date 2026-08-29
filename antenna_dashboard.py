"""
Gqrx-style RF waterfall monitor for the antenna drone-detection project.

This version is intentionally an RF viewer, not a drone classifier. It scans a
single 2.4 GHz window with HackRF One and displays a spectrum waveform plus a
wide waterfall.

Detection/classification will be added later after the RF viewing stage is
stable and the MUX wiring is final.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from flask import Flask, jsonify, render_template
import numpy as np


@dataclass
class MonitorBand:
    key: str
    label: str
    antenna: str
    low_mhz: float
    high_mhz: float
    markers_mhz: list[float]
    rows: Deque[list[float]] = field(default_factory=lambda: deque(maxlen=300))
    peaks: list[dict] = field(default_factory=list)
    noise_floor_db: float = -100.0
    peak_power_db: float = -100.0
    last_scan_ts: float = 0.0
    status: str = "waiting"
    row_seq: int = 0


class ProcessLog:
    def __init__(self, path: str = "logs/rf_monitor.jsonl", max_items: int = 120):
        self.path = path
        self.items: Deque[dict] = deque(maxlen=max_items)
        self.lock = threading.Lock()

    def add(self, level: str, message: str, **fields) -> None:
        item = {
            "ts": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            **fields,
        }
        with self.lock:
            self.items.appendleft(item)
            try:
                PathSafe.ensure_parent(self.path)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(item) + "\n")
            except OSError:
                pass

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.items)


class PathSafe:
    @staticmethod
    def ensure_parent(path: str) -> None:
        import os

        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)


class OfflineWaterfallBackend:
    """Shows quiet instrument panes when HackRF live mode is not selected."""

    def __init__(self, bands: list[MonitorBand], bins: int = 220, log: ProcessLog | None = None):
        self.bands = bands
        self.bins = bins
        self.log = log or ProcessLog()
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
                "logs": self.log.snapshot(),
            }

    def _loop(self) -> None:
        index = 0
        while self.running:
            with self.lock:
                band = self.bands[index]
                band.rows.append(self._quiet_row())
                band.row_seq += 1
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
        db_min: float = -95.0,
        db_max: float = -35.0,
        update_interval: float = 1 / 60,
    ):
        super().__init__(bands, bins=bins)
        self.sweep_start_mhz = sweep_start_mhz
        self.sweep_stop_mhz = sweep_stop_mhz
        self.bin_width_hz = bin_width_hz
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.amp = amp
        self.avg_alpha = avg_alpha
        self.db_min = db_min
        self.db_max = db_max
        self.update_interval = update_interval
        self.process: subprocess.Popen[str] | None = None
        self.error_message = ""
        self.sweep_lines = 0
        self.rows_published = 0
        self.last_log_ts = 0.0
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
                    f"LNA {self.lna_gain} dB, VGA {self.vga_gain} dB, amp {self.amp}, "
                    f"color {self.db_min:g}..{self.db_max:g} dB"
                ),
                "active_band": self.active_key,
                "bands": [band_payload(band) for band in self.bands],
                "logs": self.log.snapshot(),
                "stats": {
                    "sweep_lines": self.sweep_lines,
                    "rows_published": self.rows_published,
                },
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
        self.log.add("info", "starting hackrf_sweep", command=" ".join(cmd))
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
            self.sweep_lines += 1
            hz_low, bin_width_hz, powers = parsed
            self._merge_segment(hz_low, bin_width_hz, powers)

            now = time.monotonic()
            if now - last_flush >= self.update_interval:
                self._publish_rows()
                last_flush = now

        stderr = ""
        if self.process and self.process.stderr:
            stderr = self.process.stderr.read().strip()
        with self.lock:
            self.error_message = stderr.splitlines()[0] if stderr else "HackRF sweep stopped"
            for band in self.bands:
                band.status = "hackrf error"
        self.log.add("error", self.error_message)

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
                normalized = normalize_db_fixed(raw, self.db_min, self.db_max)
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
                    if band.key == "ghz24" and time.monotonic() - self.last_log_ts >= 0.25:
                        self.log.add(
                            "debug",
                            "2.4 GHz row",
                            noise_db=round(band.noise_floor_db, 1),
                            peak_db=round(band.peak_power_db, 1),
                            peaks=[peak["freq_mhz"] for peak in band.peaks[:4]],
                        )
                        self.last_log_ts = time.monotonic()
                else:
                    band.status = "waiting"
                self._raw_rows[band.key] = [None] * self.bins
            self.rows_published += 1


class HackRFIQBackend(OfflineWaterfallBackend):
    """Gqrx-like live FFT from HackRF IQ samples using hackrf_transfer."""

    def __init__(
        self,
        bands: list[MonitorBand],
        center_hz: int = 2_400_000_000,
        sample_rate_hz: int = 10_000_000,
        fft_size: int = 1024,
        lna_gain: int = 16,
        vga_gain: int = 20,
        amp: int = 0,
        avg_alpha: float = 0.45,
        db_min: float = -95.0,
        db_max: float = -35.0,
        update_interval: float = 1 / 60,
    ):
        super().__init__(bands, bins=fft_size)
        self.center_hz = center_hz
        self.sample_rate_hz = sample_rate_hz
        self.fft_size = fft_size
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.amp = amp
        self.avg_alpha = avg_alpha
        self.db_min = db_min
        self.db_max = db_max
        self.update_interval = update_interval
        self.process: subprocess.Popen[bytes] | None = None
        self.error_message = ""
        self.fft_frames = 0
        self.rows_published = 0
        self.last_log_ts = 0.0
        self._smooth_row: list[float] | None = None

        span_mhz = sample_rate_hz / 1_000_000
        low_mhz = center_hz / 1_000_000 - span_mhz / 2
        high_mhz = center_hz / 1_000_000 + span_mhz / 2
        band = self.bands[0]
        band.low_mhz = low_mhz
        band.high_mhz = high_mhz
        band.markers_mhz = [low_mhz, center_hz / 1_000_000, high_mhz]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "mode": "live",
                "source": (
                    self.error_message
                    or f"HackRF IQ center {self.center_hz / 1e6:.6f} MHz, "
                    f"span {self.sample_rate_hz / 1e6:g} MHz, LNA {self.lna_gain} dB, "
                    f"VGA {self.vga_gain} dB, amp {self.amp}, color {self.db_min:g}..{self.db_max:g} dB"
                ),
                "active_band": self.active_key,
                "bands": [band_payload(band) for band in self.bands],
                "logs": self.log.snapshot(),
                "stats": {
                    "fft_frames": self.fft_frames,
                    "rows_published": self.rows_published,
                },
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
            "hackrf_transfer",
            "-r",
            "-",
            "-f",
            str(self.center_hz),
            "-s",
            str(self.sample_rate_hz),
            "-l",
            str(self.lna_gain),
            "-g",
            str(self.vga_gain),
            "-a",
            str(self.amp),
            "-b",
            str(min(10_000_000, self.sample_rate_hz)),
        ]
        self.log.add("info", "starting hackrf_transfer IQ stream", command=" ".join(cmd))
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        window = np.hanning(self.fft_size).astype(np.float32)
        last_publish = time.monotonic()
        assert self.process.stdout is not None
        while self.running:
            chunk = self.process.stdout.read(self.fft_size * 2)
            if not chunk:
                if self.process.poll() is not None:
                    break
                time.sleep(0.002)
                continue
            if len(chunk) < self.fft_size * 2:
                continue

            samples_i8 = np.frombuffer(chunk, dtype=np.int8).astype(np.float32)
            iq = samples_i8[0::2] + 1j * samples_i8[1::2]
            iq = iq[: self.fft_size]
            iq = iq - np.mean(iq)
            spectrum = np.fft.fftshift(np.fft.fft(iq * window))
            power_db = 20 * np.log10(np.abs(spectrum) + 1e-6) - 45.0
            row = normalize_db_array(power_db, self.db_min, self.db_max)

            if self._smooth_row is not None:
                row = [
                    self._smooth_row[index] * (1 - self.avg_alpha) + value * self.avg_alpha
                    for index, value in enumerate(row)
                ]
            self._smooth_row = row
            self.fft_frames += 1

            now = time.monotonic()
            if now - last_publish >= self.update_interval:
                self._publish_row(row, power_db)
                last_publish = now

        stderr = ""
        if self.process and self.process.stderr:
            stderr = self.process.stderr.read().decode("utf-8", errors="replace").strip()
        with self.lock:
            self.error_message = stderr.splitlines()[0] if stderr else "HackRF IQ stream stopped"
            self.bands[0].status = "hackrf error"
        self.log.add("error", self.error_message)

    def _publish_row(self, row: list[float], power_db: np.ndarray) -> None:
        band = self.bands[0]
        valid_db = [float(value) for value in power_db]
        with self.lock:
            band.rows.append(row)
            band.row_seq += 1
            band.peaks = find_peaks(row, band)
            band.noise_floor_db = percentile(sorted(valid_db), 20)
            band.peak_power_db = max(valid_db)
            band.status = "live iq"
            band.last_scan_ts = time.time()
            self.active_key = band.key
            self.rows_published += 1
            if time.monotonic() - self.last_log_ts >= 0.25:
                self.log.add(
                    "debug",
                    "IQ FFT row",
                    center_mhz=round(self.center_hz / 1e6, 6),
                    span_mhz=round(self.sample_rate_hz / 1e6, 3),
                    noise_db=round(band.noise_floor_db, 1),
                    peak_db=round(band.peak_power_db, 1),
                    peaks=[peak["freq_mhz"] for peak in band.peaks[:4]],
                )
                self.last_log_ts = time.monotonic()


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


def normalize_db_fixed(raw_db_row: list[float | None], db_min: float, db_max: float) -> list[float]:
    span = max(1.0, db_max - db_min)
    return [
        0.0 if value is None else max(0.0, min(1.0, (value - db_min) / span))
        for value in raw_db_row
    ]


def normalize_db_array(values: np.ndarray, db_min: float, db_max: float) -> list[float]:
    span = max(1.0, db_max - db_min)
    clipped = np.clip((values - db_min) / span, 0.0, 1.0)
    return clipped.astype(float).tolist()


def make_bands() -> list[MonitorBand]:
    return [
        MonitorBand(
            key="ghz24",
            label="2.4 GHz Drone Band",
            antenna="Vivaldi 2-6 GHz",
            low_mhz=2300,
            high_mhz=2500,
            markers_mhz=[2300, 2400, 2483.5, 2500],
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
        "rows": [],
        "waterfall_row": list(band.rows[-1]) if band.rows else [],
        "waveform": list(band.rows[-1]) if band.rows else [],
        "peaks": band.peaks,
        "noise_floor_db": round(band.noise_floor_db, 1),
        "peak_power_db": round(band.peak_power_db, 1),
        "last_scan_ts": band.last_scan_ts,
        "status": band.status,
        "row_seq": band.row_seq,
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
        if args.backend == "sweep":
            backend = HackRFSweepBackend(
                bands,
                sweep_start_mhz=args.sweep_start,
                sweep_stop_mhz=args.sweep_stop,
                bin_width_hz=args.bin_width,
                lna_gain=args.lna,
                vga_gain=args.vga,
                amp=args.amp,
                avg_alpha=args.avg_alpha,
                db_min=args.db_min,
                db_max=args.db_max,
                update_interval=args.update_interval,
            )
        else:
            backend = HackRFIQBackend(
                bands,
                center_hz=args.center_hz,
                sample_rate_hz=args.sample_rate,
                fft_size=args.fft_size,
                lna_gain=args.lna,
                vga_gain=args.vga,
                amp=args.amp,
                avg_alpha=args.avg_alpha,
                db_min=args.db_min,
                db_max=args.db_max,
                update_interval=args.update_interval,
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
    parser.add_argument("--backend", choices=["iq", "sweep"], default="iq", help="HackRF backend")
    parser.add_argument("--center-hz", type=int, default=2_400_000_000, help="IQ center frequency in Hz")
    parser.add_argument("--sample-rate", type=int, default=10_000_000, help="IQ sample rate/span in Hz")
    parser.add_argument("--fft-size", type=int, default=1024, help="FFT bins for IQ backend")
    parser.add_argument("--sweep-start", type=int, default=2300, help="Sweep start in MHz")
    parser.add_argument("--sweep-stop", type=int, default=2500, help="Sweep stop in MHz")
    parser.add_argument("--bin-width", type=int, default=1_000_000, help="hackrf_sweep bin width in Hz")
    parser.add_argument("--lna", type=int, default=16, help="HackRF LNA/IF gain in dB")
    parser.add_argument("--vga", type=int, default=20, help="HackRF VGA/baseband gain in dB")
    parser.add_argument("--amp", type=int, default=0, choices=[0, 1], help="HackRF RF amp, 0 off or 1 on")
    parser.add_argument("--avg-alpha", type=float, default=0.75, help="Waterfall smoothing, 0.1 slow to 1.0 raw")
    parser.add_argument("--db-min", type=float, default=-95.0, help="Waterfall color floor in dB")
    parser.add_argument("--db-max", type=float, default=-35.0, help="Waterfall color ceiling in dB")
    parser.add_argument("--update-interval", type=float, default=1 / 60, help="Backend waterfall row interval in seconds")
    args = parser.parse_args()

    app = create_app(live=args.live or args.hackrf_vivaldi, args=args)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
