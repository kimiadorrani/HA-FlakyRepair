"""
Export reproduced flaky tests from a results session into a new CSV.

By default this script reads the original dataset at `src/data/py-data.csv`,
matches rows against reproduced tests from a results session, and writes a new
CSV containing only the reproduced rows.

Example:
  .venv/bin/python -m src.data.preprocess.export_reproducible_csv --session 2026-03-24_14-35-48
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[3]
RESULTS_DIR = ROOT_DIR / "results"
DATASET_PATH = ROOT_DIR / "src" / "data" / "py-data.csv"

TEST_NAME_COLUMN = (
    "Pytest Test Name (PathToFile::TestClass::TestMethod or PathToFile::TestMethod)"
)
EXTRA_FIELDS = (
    "Preprocess Status",
    "Reproduced",
    "Preprocess Error",
    "Selected Profile",
    "CPU Limit",
    "Memory Limit",
    "Pass Count",
    "Fail Count",
    "Outcome Profile",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export reproduced non-OD flaky tests into a new CSV."
    )
    parser.add_argument(
        "--session",
        required=True,
        help="Results session id, for example 2026-03-24_14-35-48",
    )
    parser.add_argument(
        "--input-csv",
        default=str(DATASET_PATH),
        help="Source dataset CSV path",
    )
    parser.add_argument(
        "--output-csv",
        default=str(ROOT_DIR / "src" / "data" / "preprocessed" / "py-data-reproducible.csv"),
        help="Destination CSV path",
    )
    return parser.parse_args()


def _result_key(project: str, sha: str, test_name: str, category: str) -> tuple[str, str, str, str]:
    return (project, sha, test_name, category)


def _row_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    url = row.get("Project URL", "").strip()
    project_name = url.rstrip("/").split("/")[-1]
    return (
        project_name,
        row.get("SHA Detected", "").strip(),
        row.get(TEST_NAME_COLUMN, "").strip(),
        row.get("Category", "").strip(),
    )


def _fieldnames_from_input(input_csv: Path) -> list[str]:
    with input_csv.open(encoding="utf-8", newline="") as src_handle:
        reader = csv.DictReader(src_handle)
        fieldnames = list(reader.fieldnames or [])
    for extra_field in EXTRA_FIELDS:
        if extra_field not in fieldnames:
            fieldnames.append(extra_field)
    return fieldnames


def _selected_profile_from_test(test: dict[str, Any]) -> dict[str, str]:
    attempts = test.get("execution_profiles") or []
    preprocess_status = "reproduced"
    if test.get("error_message"):
        preprocess_status = "execution_error"
    elif not test.get("is_flakiness_reproduced"):
        preprocess_status = "could_not_reproduce"

    if not attempts:
        return {
            "Preprocess Status": preprocess_status,
            "Reproduced": "true" if test.get("is_flakiness_reproduced") else "false",
            "Preprocess Error": str(test.get("error_message") or ""),
            "Selected Profile": "",
            "CPU Limit": "",
            "Memory Limit": "",
            "Pass Count": str(test.get("pass_count", 0)),
            "Fail Count": str(test.get("fail_count", 0)),
            "Outcome Profile": test.get("outcome_profile", "") or "",
        }

    chosen_attempt = None
    for attempt in attempts:
        if attempt.get("outcome_profile") == "mixed":
            chosen_attempt = attempt
            break
    if chosen_attempt is None:
        chosen_attempt = attempts[-1]

    return {
        "Preprocess Status": preprocess_status,
        "Reproduced": "true" if test.get("is_flakiness_reproduced") else "false",
        "Preprocess Error": str(test.get("error_message") or ""),
        "Selected Profile": str(chosen_attempt.get("name") or ""),
        "CPU Limit": str(chosen_attempt.get("cpu_limit") or ""),
        "Memory Limit": str(chosen_attempt.get("memory_limit") or ""),
        "Pass Count": str(chosen_attempt.get("pass_count", test.get("pass_count", 0))),
        "Fail Count": str(chosen_attempt.get("fail_count", test.get("fail_count", 0))),
        "Outcome Profile": str(chosen_attempt.get("outcome_profile") or test.get("outcome_profile") or ""),
    }


def reproduced_rows_from_session(session_dir: Path) -> dict[tuple[str, str, str, str], dict[str, str]]:
    reproduced: dict[tuple[str, str, str, str], dict[str, str]] = {}

    for json_file in session_dir.glob("*.json"):
        if json_file.name == "summary.json":
            continue

        with json_file.open(encoding="utf-8") as handle:
            payload = json.load(handle)

        for test in payload.get("tests", []):
            if not test.get("is_flakiness_reproduced"):
                continue

            categories = test.get("category") or []
            category = categories[0] if categories else ""
            reproduced[
                _result_key(
                    payload.get("project", ""),
                    test.get("sha_detected", ""),
                    test.get("test_name", ""),
                    category,
                )
            ] = _selected_profile_from_test(test)

    return reproduced


def reproduced_keys_from_session(session_dir: Path) -> set[tuple[str, str, str, str]]:
    return set(reproduced_rows_from_session(session_dir))


def existing_output_keys(output_csv: Path) -> set[tuple[str, str, str, str]]:
    if not output_csv.exists():
        return set()

    with output_csv.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return {_row_key(row) for row in reader}


def append_processed_row(
    input_csv: Path,
    output_csv: Path,
    result_key: tuple[str, str, str, str],
    row_data: dict[str, str],
) -> bool:
    fieldnames = _fieldnames_from_input(input_csv)
    matched_row: dict[str, str] | None = None

    with input_csv.open(encoding="utf-8", newline="") as src_handle:
        reader = csv.DictReader(src_handle)
        for row in reader:
            if _row_key(row) == result_key:
                matched_row = dict(row)
                break

    if matched_row is None:
        return False

    matched_row.update(row_data)
    write_header = not output_csv.exists() or output_csv.stat().st_size == 0
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("a", encoding="utf-8", newline="") as dst_handle:
        writer = csv.DictWriter(dst_handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(matched_row)

    return True


def processed_row_data_from_state(state: dict[str, Any]) -> dict[str, str]:
    return _selected_profile_from_test(state)


def export_rows(
    input_csv: Path,
    output_csv: Path,
    reproduced_rows: dict[tuple[str, str, str, str], dict[str, str]],
) -> int:
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    with input_csv.open(encoding="utf-8", newline="") as src_handle:
        reader = csv.DictReader(src_handle)
        fieldnames = _fieldnames_from_input(input_csv)
        rows_to_write: list[dict[str, str]] = []

        for row in reader:
            key = _row_key(row)
            if key in reproduced_rows:
                enriched_row = dict(row)
                enriched_row.update(reproduced_rows[key])
                rows_to_write.append(enriched_row)

    with output_csv.open("w", encoding="utf-8", newline="") as dst_handle:
        writer = csv.DictWriter(dst_handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_to_write)

    return len(rows_to_write)


def main() -> None:
    args = parse_args()
    session_dir = RESULTS_DIR / args.session
    if not session_dir.is_dir():
        raise SystemExit(f"Results session not found: {session_dir}")

    reproduced_rows = reproduced_rows_from_session(session_dir)
    count = export_rows(
        Path(args.input_csv),
        Path(args.output_csv),
        reproduced_rows,
    )
    print(
        f"Wrote {count} reproduced row(s) from session {args.session} "
        f"to {args.output_csv}"
    )


if __name__ == "__main__":
    main()
