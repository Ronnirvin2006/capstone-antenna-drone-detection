"""
Train the dashboard signal classifier from recorded RF capture windows.

Run after collecting at least background and drone samples:
  python train_signal_classifier.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from modules.signal_detection.ml_detector import SignalFeatures, save_prototype_model


def main() -> None:
    parser = argparse.ArgumentParser(description="Train RF signal classifier")
    parser.add_argument("--data-dir", default="data/rf_captures")
    parser.add_argument("--model-path", default="models/signal_classifier.json")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    samples: list[tuple[str, SignalFeatures]] = []

    for path in sorted(data_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                record = json.loads(line)
                label = record["label"]
                if label in {"controller", "wifi"}:
                    label = "background"
                samples.append((label, SignalFeatures(**record["features"])))

    if not samples:
        raise SystemExit(f"No training data found in {data_dir}")

    model = save_prototype_model(samples, args.model_path)
    print(f"Saved model: {args.model_path}")
    print(f"Samples: {model['sample_count']}")
    print(f"Labels: {', '.join(model['labels'])}")


if __name__ == "__main__":
    main()
