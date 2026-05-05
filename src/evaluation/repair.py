"""
Repair-phase evaluator — stub for future Repair Agent and Review Agent.

When the Repair Agent is implemented, call record() after each rotation
and report() at session end to get patch-quality metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RepairRecord:
    project:         str
    test_name:       str
    rotation:        int     # which repair iteration (1-indexed)
    patch_valid:     bool    # patch applies and is syntactically valid
    tests_pass:      bool    # target test passes after patch
    regression:      bool    # any other test broke
    rotation_count:  int     # total rotations used to reach this outcome


class RepairEvaluator:
    """Stub: accumulates repair outcomes for patch-quality reporting."""

    def __init__(self) -> None:
        self._records: list[RepairRecord] = []

    def record(
        self,
        project: str,
        test_name: str,
        rotation: int,
        patch_valid: bool,
        tests_pass: bool,
        regression: bool,
        rotation_count: int,
    ) -> RepairRecord:
        rec = RepairRecord(
            project=project, test_name=test_name, rotation=rotation,
            patch_valid=patch_valid, tests_pass=tests_pass,
            regression=regression, rotation_count=rotation_count,
        )
        self._records.append(rec)
        return rec

    def report(self) -> dict[str, Any]:
        if not self._records:
            return {}

        total    = len(self._records)
        repaired = sum(1 for r in self._records if r.tests_pass and not r.regression)
        return {
            "phase":          "repair",
            "total":          total,
            "repaired":       repaired,
            "repair_rate":    round(repaired / total, 4) if total > 0 else 0.0,
            "avg_rotations":  round(
                sum(r.rotation_count for r in self._records) / total, 2
            ) if total > 0 else 0.0,
        }
