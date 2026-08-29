"""
Small trainable classifier for drone-like RF waterfall patterns.

This is a lightweight ML-style detector for the first app version. It uses
synthetic prototypes now, then the same feature interface can be trained from
real HackRF captures later.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class SignalFeatures:
    peak_count: float
    peak_power: float
    occupied_ratio: float
    hopping_score: float
    burstiness: float


class PrototypeSignalClassifier:
    """Nearest-prototype classifier: noise, steady carrier, or drone-like."""

    def __init__(self) -> None:
        self.prototypes = {
            "noise": self._average(self._synthetic_noise() for _ in range(80)),
            "steady": self._average(self._synthetic_steady() for _ in range(80)),
            "drone_like": self._average(self._synthetic_hopping() for _ in range(80)),
        }

    def classify(self, rows: list[list[float]]) -> dict:
        features = extract_features(rows)
        distances = {
            label: self._distance(features, proto)
            for label, proto in self.prototypes.items()
        }
        label = min(distances, key=distances.get)
        ordered = sorted(distances.values())
        margin = ordered[1] - ordered[0] if len(ordered) > 1 else 0.0
        confidence = max(0.0, min(0.99, 0.45 + margin * 1.8))
        return {
            "label": label,
            "confidence": round(confidence, 3),
            "features": features.__dict__,
            "drone_like": label == "drone_like" and confidence >= 0.52,
        }

    def _synthetic_noise(self) -> SignalFeatures:
        return SignalFeatures(
            peak_count=random.uniform(0, 1),
            peak_power=random.uniform(0.12, 0.25),
            occupied_ratio=random.uniform(0.01, 0.04),
            hopping_score=random.uniform(0, 0.08),
            burstiness=random.uniform(0.01, 0.06),
        )

    def _synthetic_steady(self) -> SignalFeatures:
        return SignalFeatures(
            peak_count=random.uniform(1, 3),
            peak_power=random.uniform(0.45, 0.75),
            occupied_ratio=random.uniform(0.02, 0.08),
            hopping_score=random.uniform(0.02, 0.16),
            burstiness=random.uniform(0.02, 0.16),
        )

    def _synthetic_hopping(self) -> SignalFeatures:
        return SignalFeatures(
            peak_count=random.uniform(3, 8),
            peak_power=random.uniform(0.55, 0.98),
            occupied_ratio=random.uniform(0.06, 0.22),
            hopping_score=random.uniform(0.28, 0.85),
            burstiness=random.uniform(0.24, 0.78),
        )

    def _average(self, samples) -> SignalFeatures:
        values = list(samples)
        count = len(values)
        return SignalFeatures(
            peak_count=sum(v.peak_count for v in values) / count,
            peak_power=sum(v.peak_power for v in values) / count,
            occupied_ratio=sum(v.occupied_ratio for v in values) / count,
            hopping_score=sum(v.hopping_score for v in values) / count,
            burstiness=sum(v.burstiness for v in values) / count,
        )

    def _distance(self, a: SignalFeatures, b: SignalFeatures) -> float:
        weights = {
            "peak_count": 0.12,
            "peak_power": 1.25,
            "occupied_ratio": 2.0,
            "hopping_score": 2.8,
            "burstiness": 2.4,
        }
        total = 0.0
        for key, weight in weights.items():
            total += weight * (getattr(a, key) - getattr(b, key)) ** 2
        return math.sqrt(total)


def extract_features(rows: list[list[float]]) -> SignalFeatures:
    if not rows or not rows[0]:
        return SignalFeatures(0, 0, 0, 0, 0)

    recent = rows[-24:]
    bins = len(recent[0])
    column_max = [max(row[col] for row in recent) for col in range(bins)]
    threshold = 0.42
    active_columns = [index for index, value in enumerate(column_max) if value >= threshold]

    peak_count = _count_clusters(active_columns)
    peak_power = max(column_max) if column_max else 0.0
    occupied_ratio = len(active_columns) / bins

    active_sets = []
    row_energy = []
    for row in recent:
        active = {index for index, value in enumerate(row) if value >= threshold}
        active_sets.append(active)
        row_energy.append(sum(row) / len(row))

    changes = 0
    comparisons = 0
    for before, after in zip(active_sets, active_sets[1:]):
        union = before | after
        if not union:
            continue
        changes += len(before ^ after) / len(union)
        comparisons += 1
    hopping_score = changes / comparisons if comparisons else 0.0

    mean_energy = sum(row_energy) / len(row_energy)
    burstiness = sum(abs(value - mean_energy) for value in row_energy) / len(row_energy)

    return SignalFeatures(
        peak_count=float(peak_count),
        peak_power=peak_power,
        occupied_ratio=occupied_ratio,
        hopping_score=hopping_score,
        burstiness=burstiness,
    )


def _count_clusters(indexes: list[int]) -> int:
    if not indexes:
        return 0
    clusters = 1
    for previous, current in zip(indexes, indexes[1:]):
        if current - previous > 1:
            clusters += 1
    return clusters
