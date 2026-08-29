#!/usr/bin/env python3
import argparse
import shutil
import subprocess
from collections import defaultdict


BANDS = {
    "24": [(2400, 2485)],
    "58": [(5725, 5850)],
    "all": [(2400, 2485), (5725, 5850)],
}


def run_sweep(freq_min, freq_max, sweeps, bin_width, lna, vga, amp):
    cmd = [
        "hackrf_sweep",
        "-f",
        f"{freq_min}:{freq_max}",
        "-N",
        str(sweeps),
        "-w",
        str(bin_width),
        "-l",
        str(lna),
        "-g",
        str(vga),
        "-a",
        "1" if amp else "0",
    ]
    process = subprocess.run(cmd, text=True, capture_output=True, check=True)
    return parse_sweep(process.stdout)


def parse_sweep(text):
    bins = defaultdict(list)
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            hz_low = float(parts[2])
            hz_high = float(parts[3])
            hz_bin_width = float(parts[4])
            powers = [float(value) for value in parts[6:] if value]
        except ValueError:
            continue
        if not powers:
            continue
        for index, power in enumerate(powers):
            freq_hz = hz_low + (index + 0.5) * hz_bin_width
            if freq_hz <= hz_high:
                bins[round(freq_hz / 1e6, 3)].append(power)
    return {freq: sum(values) / len(values) for freq, values in bins.items()}


def merge_candidates(candidates, spacing_mhz):
    groups = []
    for candidate in sorted(candidates, key=lambda item: item["freq_mhz"]):
        if not groups or candidate["freq_mhz"] - groups[-1][-1]["freq_mhz"] > spacing_mhz:
            groups.append([candidate])
        else:
            groups[-1].append(candidate)

    peaks = []
    for group in groups:
        best = max(group, key=lambda item: item["delta_db"])
        peaks.append(best)
    return sorted(peaks, key=lambda item: item["delta_db"], reverse=True)


def compare(background, active, threshold_db):
    candidates = []
    for freq_mhz, active_db in active.items():
        if freq_mhz not in background:
            continue
        delta_db = active_db - background[freq_mhz]
        if delta_db >= threshold_db:
            candidates.append(
                {
                    "freq_mhz": freq_mhz,
                    "background_db": background[freq_mhz],
                    "active_db": active_db,
                    "delta_db": delta_db,
                }
            )
    return merge_candidates(candidates, spacing_mhz=1.0)


def print_results(results, limit):
    if not results:
        print("\nNo strong repeatable change found.")
        print("Try closer distance, point the Vivaldi at the drone/controller, or scan 5.8 GHz.")
        return

    print("\nStrongest changed frequencies:")
    print("Tune GNU Radio to one of these center frequencies:")
    for item in results[:limit]:
        print(
            f"  {item['freq_mhz']:9.3f} MHz  "
            f"delta={item['delta_db']:5.1f} dB  "
            f"off={item['background_db']:6.1f} dB  "
            f"on={item['active_db']:6.1f} dB"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Compare HackRF sweeps with drone/controller off and on."
    )
    parser.add_argument("--band", choices=BANDS.keys(), default="all")
    parser.add_argument("--sweeps", type=int, default=12)
    parser.add_argument("--bin-width", type=int, default=250000)
    parser.add_argument("--lna", type=int, default=16)
    parser.add_argument("--vga", type=int, default=24)
    parser.add_argument("--amp", action="store_true")
    parser.add_argument("--threshold-db", type=float, default=6.0)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()

    if shutil.which("hackrf_sweep") is None:
        raise SystemExit("hackrf_sweep not found. Install the hackrf package first.")

    print("Close Gqrx and the GNU Radio monitor before running this.")
    print("Keep antenna connected to HackRF RX/ANT.")
    input("\nTurn drone and controller OFF, then press Enter for baseline scan...")

    background = {}
    for freq_min, freq_max in BANDS[args.band]:
        print(f"Scanning OFF baseline {freq_min}-{freq_max} MHz...")
        background.update(
            run_sweep(freq_min, freq_max, args.sweeps, args.bin_width, args.lna, args.vga, args.amp)
        )

    input("\nTurn controller/drone ON, wait 5 seconds, then press Enter for active scan...")

    active = {}
    for freq_min, freq_max in BANDS[args.band]:
        print(f"Scanning ON activity {freq_min}-{freq_max} MHz...")
        active.update(
            run_sweep(freq_min, freq_max, args.sweeps, args.bin_width, args.lna, args.vga, args.amp)
        )

    results = compare(background, active, args.threshold_db)
    print_results(results, args.limit)


if __name__ == "__main__":
    main()
