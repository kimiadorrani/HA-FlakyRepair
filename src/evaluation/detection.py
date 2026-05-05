"""
Detection-phase evaluator.

Accumulates (actual, predicted) pairs across a session and produces
a confusion matrix + per-class precision/recall/F1 at write time.

Usage:
    evaluator = DetectionEvaluator()

    # after each test:
    evaluator.record(
        project="bottle-neck",
        test_name="test/test_routing.py::...",
        actual_category="OD-Brit",   # ground truth from CSV
        predicted_type="OD-Brit",    # agent's flaky_type output
        reproduced=True,
    )

    # at session end:
    report = evaluator.report()
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

# Normalise IDoFT ground-truth category strings → canonical display form
_TRUTH_NORM: dict[str, str] = {
    "NIO":     "NIO",
    "NOD":     "NOD",
    "OD-VIC":  "OD-Vic",
    "OD-BRIT": "OD-Brit",
    "OD":      "OD-Vic",   # generic OD treated as OD-Vic for accuracy matching
}


@dataclass
class DetectionRecord:
    project:    str
    test_name:  str
    actual:     str    # normalised ground truth
    predicted:  str    # agent output (flaky_type)
    correct:    bool
    reproduced: bool   # did the agent confirm flaky behaviour?


class DetectionEvaluator:
    """Accumulates detection results and produces accuracy / confusion-matrix report."""

    def __init__(self) -> None:
        self._records: list[DetectionRecord] = []

    def record(
        self,
        project: str,
        test_name: str,
        actual_category: str,
        predicted_type: str,
        reproduced: bool,
    ) -> DetectionRecord:
        actual = _TRUTH_NORM.get(actual_category.strip().upper(), actual_category.strip())
        correct = (actual == predicted_type.strip()) if actual and predicted_type else False
        rec = DetectionRecord(
            project=project,
            test_name=test_name,
            actual=actual,
            predicted=predicted_type.strip(),
            correct=correct,
            reproduced=reproduced,
        )
        self._records.append(rec)
        return rec

    def report(self) -> dict[str, Any]:
        if not self._records:
            return {}

        total      = len(self._records)
        correct    = sum(1 for r in self._records if r.correct)
        reproduced = sum(1 for r in self._records if r.reproduced)

        classes = sorted(
            set(r.actual    for r in self._records) |
            set(r.predicted for r in self._records)
        )

        cm: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in self._records:
            cm[r.actual][r.predicted] += 1

        per_class: dict[str, Any] = {}
        for cls in classes:
            tp = cm[cls][cls]
            fp = sum(cm[other][cls] for other in classes if other != cls)
            fn = sum(cm[cls][other] for other in classes if other != cls)
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1        = (
                2 * precision * recall / (precision + recall)
                if (precision + recall) > 0 else 0.0
            )
            per_class[cls] = {
                "support":   tp + fn,
                "tp": tp, "fp": fp, "fn": fn,
                "precision": round(precision, 4),
                "recall":    round(recall,    4),
                "f1":        round(f1,        4),
            }

        return {
            "phase":               "detection",
            "overall_accuracy":    round(correct    / total, 4) if total > 0 else 0.0,
            "reproduction_rate":   round(reproduced / total, 4) if total > 0 else 0.0,
            "correct":             correct,
            "reproduced":          reproduced,
            "total":               total,
            "per_class":           per_class,
            "confusion_matrix":    {k: dict(v) for k, v in cm.items()},
            "predictions": [
                {
                    "project":    r.project,
                    "test_name":  r.test_name,
                    "actual":     r.actual,
                    "predicted":  r.predicted,
                    "correct":    r.correct,
                    "reproduced": r.reproduced,
                }
                for r in self._records
            ],
        }
