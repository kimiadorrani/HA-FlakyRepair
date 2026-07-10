#!/usr/bin/env python3
"""
Compute detection accuracy for a results session by joining it back to the dataset CSV.

Example:
  .venv/bin/python scripts/shared/compute_detection_accuracy.py \
    --session 2026-03-26_08-55-19 \
    --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT_DIR / "results"
DEFAULT_INPUT_CSV = ROOT_DIR / "datasets" / "idoft" / "preprocessed" / "py-data-reproducible.csv"
TEST_NAME_COLUMN = (
    "Pytest Test Name (PathToFile::TestClass::TestMethod or PathToFile::TestMethod)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute accuracy for a detection results session."
    )
    parser.add_argument(
        "--session",
        required=True,
        help="Results session id, for example 2026-03-26_08-55-19",
    )
    parser.add_argument(
        "--input-csv",
        default=str(DEFAULT_INPUT_CSV),
        help="Dataset CSV used as ground truth.",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional path for the computed accuracy summary JSON. "
             "Defaults to results/<session>/accuracy.json",
    )
    return parser.parse_args()


def normalize_truth_category(raw: str) -> str:
    category = (raw or "").strip().upper()
    if category in {"NIO", "NOD"}:
        return category
    return category or "UNKNOWN"


def normalize_predicted_category(raw: str) -> str:
    text = (raw or "").strip().upper()
    if "NIO" in text:
        return "NIO"
    if "NOD" in text:
        return "NOD"
    if "NOT FLAKY" in text:
        return "NOT_FLAKY"
    if "UNKNOWN" in text:
        return "UNKNOWN"
    return text or "UNKNOWN"


def load_ground_truth(input_csv: Path) -> dict[tuple[str, str, str], str]:
    csv.field_size_limit(sys.maxsize)
    ground_truth: dict[tuple[str, str, str], str] = {}
    with input_csv.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            project_url = row.get("Project URL", "").strip()
            project_name = project_url.rstrip("/").split("/")[-1]
            sha = row.get("SHA Detected", "").strip()
            test_name = row.get(TEST_NAME_COLUMN, "").strip()
            if not project_name or not sha or not test_name:
                continue
            ground_truth[(project_name, sha, test_name)] = normalize_truth_category(
                row.get("Category", "")
            )
    return ground_truth


def compute_accuracy(session_dir: Path, ground_truth: dict[tuple[str, str, str], str]) -> dict:
    total_results = 0
    matched_results = 0
    exact_matches = 0
    confusion: dict[str, Counter] = defaultdict(Counter)
    unmatched: list[dict[str, str]] = []
    detailed_rows: list[dict[str, str | bool]] = []

    # Per-test records live under detection/ in newer sessions; fall back to the
    # session root for older flat sessions.
    detection_dir = session_dir / "detection"
    json_files = sorted(detection_dir.glob("*.json")) if detection_dir.is_dir() \
        else sorted(session_dir.glob("*.json"))
    for json_file in json_files:
        if json_file.name in {"summary.json", "accuracy.json", "evaluation.json"}:
            continue

        with json_file.open(encoding="utf-8") as handle:
            payload = json.load(handle)

        project_name = payload.get("project", "")
        for test in payload.get("tests", []):
            total_results += 1
            key = (
                project_name,
                test.get("sha_detected", ""),
                test.get("test_name", ""),
            )
            truth = ground_truth.get(key)
            predicted = normalize_predicted_category(test.get("flaky_type", ""))

            if truth is None:
                unmatched.append({
                    "project": project_name,
                    "sha_detected": test.get("sha_detected", ""),
                    "test_name": test.get("test_name", ""),
                })
                continue

            matched_results += 1
            is_exact_match = truth == predicted
            if is_exact_match:
                exact_matches += 1
            confusion[truth][predicted] += 1
            detailed_rows.append({
                "project": project_name,
                "sha_detected": test.get("sha_detected", ""),
                "test_name": test.get("test_name", ""),
                "ground_truth_category": truth,
                "predicted_category": predicted,
                "exact_match": is_exact_match,
            })

    accuracy = (exact_matches / matched_results) if matched_results else 0.0
    return {
        "total_results": total_results,
        "matched_results": matched_results,
        "unmatched_results": len(unmatched),
        "exact_category_matches": exact_matches,
        "exact_category_accuracy": accuracy,
        "confusion_matrix": {
            truth: dict(counter) for truth, counter in sorted(confusion.items())
        },
        "unmatched_examples": unmatched[:20],
        "details": detailed_rows,
    }


def main() -> int:
    args = parse_args()
    session_dir = RESULTS_DIR / args.session
    input_csv = Path(args.input_csv)
    output_json = (
        Path(args.output_json)
        if args.output_json
        else session_dir / "accuracy.json"
    )

    if not session_dir.exists():
        print(f"Results session not found: {session_dir}", file=sys.stderr)
        return 1
    if not input_csv.exists():
        print(f"Input CSV not found: {input_csv}", file=sys.stderr)
        return 1

    ground_truth = load_ground_truth(input_csv)
    summary = compute_accuracy(session_dir, ground_truth)

    with output_json.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    print(f"Session: {args.session}")
    print(f"Matched results: {summary['matched_results']} / {summary['total_results']}")
    print(f"Exact category matches: {summary['exact_category_matches']}")
    print(f"Exact category accuracy: {summary['exact_category_accuracy']:.4f}")
    print(f"Wrote accuracy summary to: {output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
