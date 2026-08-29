"""
Record labeled HackRF sweep windows for RF drone-signal training.

Examples:
  python collect_rf_training_data.py --label background --seconds 30
  python collect_rf_training_data.py --label drone --seconds 30
  python collect_rf_training_data.py --label controller --seconds 30
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
from pathlib import Path

from modules.signal_detection.ml_detector import extract_features, normalize_db_row


def parse_sweep_line(line: str):
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


def freq_to_bin(freq_mhz: float, low_mhz: float, high_mhz: float, bins: int) -> int:
    clamped = min(max(freq_mhz, low_mhz), high_mhz)
    return int((clamped - low_mhz) / (high_mhz - low_mhz) * (bins - 1))


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect HackRF sweep data for training")
    parser.add_argument("--label", required=True, choices=["background", "drone", "controller", "wifi"])
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--start-mhz", type=int, default=2400)
    parser.add_argument("--stop-mhz", type=int, default=6000)
    parser.add_argument("--bins", type=int, default=160)
    parser.add_argument("--window-rows", type=int, default=24)
    parser.add_argument("--bin-width", type=int, default=5_000_000)
    parser.add_argument("--lna", type=int, default=24)
    parser.add_argument("--vga", type=int, default=32)
    parser.add_argument("--amp", type=int, default=0)
    parser.add_argument("--out-dir", default="data/rf_captures")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{timestamp}_{args.label}_{args.start_mhz}_{args.stop_mhz}.jsonl"

    cmd = [
        "hackrf_sweep",
        "-f",
        f"{args.start_mhz}:{args.stop_mhz}",
        "-w",
        str(args.bin_width),
        "-l",
        str(args.lna),
        "-g",
        str(args.vga),
        "-a",
        str(args.amp),
    ]

    print(f"Recording label={args.label} for {args.seconds}s")
    print(f"Output: {out_path}")
    print("Close Gqrx first. Only one program can use HackRF.")

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    rows: list[list[float]] = []
    current_row: list[float | None] = [None] * args.bins
    last_flush = time.monotonic()
    end_time = time.monotonic() + args.seconds
    saved = 0

    interrupted = False
    try:
        assert process.stdout is not None
        with out_path.open("w", encoding="utf-8") as fh:
            while time.monotonic() < end_time:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue

                parsed = parse_sweep_line(line)
                if parsed is None:
                    continue

                hz_low, bin_width_hz, powers = parsed
                for index, power_db in enumerate(powers):
                    freq_mhz = (hz_low + index * bin_width_hz) / 1_000_000
                    if not (args.start_mhz <= freq_mhz <= args.stop_mhz):
                        continue
                    bin_index = freq_to_bin(freq_mhz, args.start_mhz, args.stop_mhz, args.bins)
                    previous = current_row[bin_index]
                    current_row[bin_index] = power_db if previous is None else max(previous, power_db)

                now = time.monotonic()
                if now - last_flush >= 0.45:
                    rows.append(normalize_db_row(current_row))
                    rows = rows[-args.window_rows :]
                    current_row = [None] * args.bins
                    last_flush = now

                    if len(rows) == args.window_rows:
                        features = extract_features(rows)
                        record = {
                            "label": args.label,
                            "start_mhz": args.start_mhz,
                            "stop_mhz": args.stop_mhz,
                            "rows": rows,
                            "features": features.__dict__,
                            "created_at": time.time(),
                        }
                        fh.write(json.dumps(record) + "\n")
                        saved += 1
                        print(f"saved windows: {saved}", end="\r", flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\nRecording stopped by user.")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    if interrupted and saved == 0:
        print("No full training window was saved. Try 30 seconds again.")
    else:
        print(f"\nDone. Saved {saved} training windows.")


if __name__ == "__main__":
    main()
