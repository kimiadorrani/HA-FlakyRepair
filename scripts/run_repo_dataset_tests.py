#!/usr/bin/env python3
"""
Run all dataset-listed tests for a single repository in one pytest invocation.

Example:
  .venv/bin/python scripts/run_repo_dataset_tests.py \
    --project-url https://github.com/bennymeg/Butter.MAS.PythonAPI
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_CSV = ROOT_DIR / "src" / "data" / "py-data.csv"
DEFAULT_WORKSPACES_DIR = ROOT_DIR / "workspaces"
TEST_NAME_COLUMN = (
    "Pytest Test Name (PathToFile::TestClass::TestMethod or PathToFile::TestMethod)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run all dataset tests for one repo in a single pytest command."
    )
    parser.add_argument(
        "--project-url",
        required=True,
        help="Repository URL as it appears in the dataset.",
    )
    parser.add_argument(
        "--input-csv",
        default=str(DEFAULT_INPUT_CSV),
        help="Dataset CSV path.",
    )
    parser.add_argument(
        "--workspaces-dir",
        default=str(DEFAULT_WORKSPACES_DIR),
        help="Directory containing checked-out repos.",
    )
    parser.add_argument(
        "--sha",
        default="",
        help="Optional SHA filter. If omitted, all SHAs for the repo are included.",
    )
    parser.add_argument(
        "--category",
        default="",
        help="Optional category filter, for example NOD or NIO.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the pytest command without executing it.",
    )
    return parser.parse_args()


def repo_name_from_url(project_url: str) -> str:
    return project_url.rstrip("/").split("/")[-1]


def collect_tests(
    input_csv: Path,
    project_url: str,
    sha: str,
    category: str,
) -> list[str]:
    tests: list[str] = []
    seen: set[str] = set()

    with input_csv.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("Project URL", "").strip() != project_url:
                continue
            if sha and row.get("SHA Detected", "").strip() != sha:
                continue
            if category and row.get("Category", "").strip() != category:
                continue

            test_name = row.get(TEST_NAME_COLUMN, "").strip()
            if not test_name or test_name in seen:
                continue

            seen.add(test_name)
            tests.append(test_name)

    return tests


def main() -> int:
    args = parse_args()

    input_csv = Path(args.input_csv)
    workspaces_dir = Path(args.workspaces_dir)
    repo_name = repo_name_from_url(args.project_url)
    repo_dir = workspaces_dir / repo_name

    if not input_csv.exists():
        print(f"Input CSV not found: {input_csv}", file=sys.stderr)
        return 1
    if not repo_dir.exists():
        print(f"Workspace repo not found: {repo_dir}", file=sys.stderr)
        return 1

    tests = collect_tests(
        input_csv=input_csv,
        project_url=args.project_url.strip(),
        sha=args.sha.strip(),
        category=args.category.strip(),
    )
    if not tests:
        print("No matching tests found.", file=sys.stderr)
        return 1

    cmd = ["pytest", "-v", *tests]

    print(f"Repo: {repo_name}")
    print(f"Workspace: {repo_dir}")
    print(f"Tests selected: {len(tests)}")
    print("Command:")
    print(" ".join(cmd))

    if args.dry_run:
        return 0

    result = subprocess.run(cmd, cwd=repo_dir)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
