#!/usr/bin/env python3
"""
Run the non-order-dependent FLAKYCAT rows in batch and keep an incremental CSV.

Example:
  .venv/bin/python scripts/flakycat/run_flakycat_batch.py --iterations 100
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from run_flakycat_java_test import (
    DEFAULT_INPUT_CSV,
    DEFAULT_RESULTS_DIR,
    DEFAULT_WORKSPACES_DIR,
    is_order_dependent,
    load_rows,
    repo_key,
    execute_row,
)


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_CSV = ROOT_DIR / "datasets" / "flakycat" / "flakycat-reproduction-results.csv"

FIELDNAMES = [
    "Project",
    "Project URL",
    "Category",
    "Commit SHA",
    "Checkout SHA",
    "Module Path",
    "Test File",
    "Test Method",
    "Fully Qualified Test Name",
    "Runtime",
    "Build Tool",
    "Iterations Requested",
    "Iterations Executed",
    "Pass Count",
    "Fail Count",
    "Reproduced",
    "Status",
    "Error",
    "Result JSON",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch-run non-order-dependent FLAKYCAT rows.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV), help="FLAKYCAT metadata CSV path")
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV), help="Incremental result CSV path")
    parser.add_argument("--workspaces-dir", default=str(DEFAULT_WORKSPACES_DIR), help="FLAKYCAT workspace root")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR), help="Per-test JSON result directory")
    parser.add_argument("--iterations", type=int, default=100, help="Executions per test row")
    parser.add_argument("--checkout", choices=["auto", "commit", "parent"], default="auto")
    parser.add_argument("--runtime", choices=["auto", "host", "docker"], default="auto")
    parser.add_argument("--project", help="Optional project-name filter")
    parser.add_argument("--test", help="Optional substring filter")
    parser.add_argument("--limit", type=int, help="Optional cap after filtering")
    parser.add_argument("--skip-missing-workspaces", action="store_true", help="Skip rows whose repos are not cloned")
    parser.add_argument("--dry-run", action="store_true", help="Resolve rows and commands without executing tests")
    return parser.parse_args()


def row_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        row.get("Project", "").strip(),
        row.get("Commit SHA", "").strip(),
        row.get("Test Method", "").strip(),
        row.get("Category", "").strip(),
    )


def load_existing(output_csv: Path) -> dict[tuple[str, str, str, str], dict[str, str]]:
    if not output_csv.exists():
        return {}
    with output_csv.open(encoding="utf-8", newline="") as handle:
        return {
            (
                row.get("Project", "").strip(),
                row.get("Commit SHA", "").strip(),
                row.get("Test Method", "").strip(),
                row.get("Category", "").strip(),
            ): row
            for row in csv.DictReader(handle)
        }


def should_skip_existing(existing_row: dict[str, str], iterations: int, workspaces_dir: Path) -> bool:
    status = existing_row.get("Status", "").strip().lower()
    recorded_iterations = existing_row.get("Iterations Requested", "").strip()
    if recorded_iterations != str(iterations):
        return False
    if status == "dry_run":
        return False
    if status == "missing_workspace":
        repo_url = existing_row.get("Project URL", "").strip()
        if repo_url and (workspaces_dir / repo_key(repo_url)).exists():
            return False
    return True


def select_rows(rows: list[dict[str, str]], args: argparse.Namespace) -> list[dict[str, str]]:
    filtered = []
    for row in rows:
        if is_order_dependent(row):
            continue
        if args.project and row.get("Project", "").lower() != args.project.lower():
            continue
        if args.test:
            haystack = f"{row.get('Test Method', '')} {row.get('Fully Qualified Test Name', '')}".lower()
            if args.test.lower() not in haystack:
                continue
        filtered.append(row)
    if args.limit is not None:
        return filtered[:args.limit]
    return filtered


def has_workspace(row: dict[str, str], workspaces_dir: Path) -> bool:
    repo_url = row.get("Project URL", "").strip()
    if not repo_url:
        return False
    return (workspaces_dir / repo_key(repo_url)).exists()


def status_from_result(result: dict[str, object]) -> tuple[str, str]:
    error = str(result.get("error", "") or "").strip()
    first_fail_log = str(result.get("first_fail_log", "") or "").strip()
    if result.get("dry_run"):
        return "dry_run", ""
    if error:
        return "execution_error", error
    if result.get("reproduced"):
        return "reproduced", ""
    if int(result.get("pass_count", 0) or 0) > 0 and int(result.get("fail_count", 0) or 0) == 0:
        return "always_pass", ""
    if int(result.get("fail_count", 0) or 0) > 0 and int(result.get("pass_count", 0) or 0) == 0:
        if "could not resolve" in first_fail_log.lower() or "dependencyresolutionexception" in first_fail_log.lower():
            return "dependency_error", first_fail_log.splitlines()[0] if first_fail_log else ""
        if "unable to locate a java runtime" in first_fail_log.lower():
            return "runtime_error", first_fail_log.splitlines()[0]
        return "always_fail", first_fail_log.splitlines()[0] if first_fail_log else ""
    return "unknown", ""


def write_rows(output_csv: Path, rows: list[dict[str, str]]) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    input_rows = load_rows(Path(args.input_csv))
    selected_rows = select_rows(input_rows, args)
    output_csv = Path(args.output_csv)
    existing = load_existing(output_csv)
    results_map = dict(existing)
    workspaces_dir = Path(args.workspaces_dir)
    results_dir = Path(args.results_dir)

    print(f"Selected non-order-dependent rows: {len(selected_rows)}")
    print(f"Already recorded rows: {len(existing)}")

    for index, row in enumerate(selected_rows, start=1):
        key = row_key(row)
        if key in results_map and should_skip_existing(results_map[key], args.iterations, workspaces_dir):
            print(f"[{index}/{len(selected_rows)}] Skipping already recorded row: {row['Project']} :: {row['Test Method']}")
            continue

        if args.skip_missing_workspaces and not has_workspace(row, workspaces_dir):
            print(f"[{index}/{len(selected_rows)}] Missing workspace, skipping: {row['Project']} :: {row['Test Method']}")
            result_row = {
                "Project": row.get("Project", ""),
                "Project URL": row.get("Project URL", ""),
                "Category": row.get("Category", ""),
                "Commit SHA": row.get("Commit SHA", ""),
                "Checkout SHA": row.get("Checkout SHA", ""),
                "Module Path": row.get("Module Path", ""),
                "Test File": row.get("Test File", ""),
                "Test Method": row.get("Test Method", ""),
                "Fully Qualified Test Name": row.get("Fully Qualified Test Name", ""),
                "Runtime": "",
                "Build Tool": "",
                "Iterations Requested": str(args.iterations),
                "Iterations Executed": "0",
                "Pass Count": "0",
                "Fail Count": "0",
                "Reproduced": "false",
                "Status": "missing_workspace",
                "Error": "Workspace repo not cloned",
                "Result JSON": "",
            }
            results_map[key] = result_row
            write_rows(output_csv, list(results_map.values()))
            continue

        print(f"[{index}/{len(selected_rows)}] Running {row['Project']} :: {row['Test Method']} ({row['Category']})")
        try:
            result, output_path = execute_row(
                row=row,
                workspaces_dir=workspaces_dir,
                results_dir=results_dir,
                iterations=args.iterations,
                checkout_mode=args.checkout,
                runtime_preference=args.runtime,
                dry_run=args.dry_run,
            )
        except (Exception, SystemExit) as exc:
            result = {
                "runtime": "",
                "build_tool": "",
                "iterations_requested": args.iterations,
                "iterations_executed": 0,
                "pass_count": 0,
                "fail_count": 0,
                "reproduced": False,
                "error": str(exc),
            }
            output_path = Path("")

        status, error = status_from_result(result)
        if not error:
            error = str(result.get("error", "") or "").strip()
        result_row = {
            "Project": row.get("Project", ""),
            "Project URL": row.get("Project URL", ""),
            "Category": row.get("Category", ""),
            "Commit SHA": row.get("Commit SHA", ""),
            "Checkout SHA": row.get("Checkout SHA", ""),
            "Module Path": row.get("Module Path", ""),
            "Test File": row.get("Test File", ""),
            "Test Method": row.get("Test Method", ""),
            "Fully Qualified Test Name": row.get("Fully Qualified Test Name", ""),
            "Runtime": str(result.get("runtime", "") or ""),
            "Build Tool": str(result.get("build_tool", "") or ""),
            "Iterations Requested": str(result.get("iterations_requested", args.iterations)),
            "Iterations Executed": str(result.get("iterations_executed", 0)),
            "Pass Count": str(result.get("pass_count", 0)),
            "Fail Count": str(result.get("fail_count", 0)),
            "Reproduced": str(bool(result.get("reproduced"))).lower(),
            "Status": status,
            "Error": error,
            "Result JSON": str(output_path),
        }
        results_map[key] = result_row
        write_rows(output_csv, list(results_map.values()))

    print(f"Wrote incremental batch CSV to {output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
