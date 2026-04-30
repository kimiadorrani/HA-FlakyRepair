#!/usr/bin/env python3
"""
Verify that UI-FLAKY workspace folders are real git repositories.

Checks the preprocessed metadata CSV against `workspaces/uiflaky/`, validates
that each expected repo has a `.git` directory and passes `git rev-parse`, and
can optionally prune broken repos from the metadata CSV after writing a backup.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_CSV = ROOT_DIR / "datasets" / "uiflaky" / "preprocessed" / "uiflaky-metadata.csv"
DEFAULT_WORKSPACES_DIR = ROOT_DIR / "workspaces" / "uiflaky"
DEFAULT_OUTPUT_JSON = ROOT_DIR / "datasets" / "uiflaky" / "uiflaky-workspace-audit.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify UI-FLAKY workspace clones.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV), help="UI-FLAKY metadata CSV path")
    parser.add_argument("--workspaces-dir", default=str(DEFAULT_WORKSPACES_DIR), help="UI-FLAKY workspace root")
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON), help="Audit summary output path")
    parser.add_argument(
        "--prune-invalid-from-csv",
        action="store_true",
        help="Remove rows whose repos are missing or invalid, after backing up the CSV",
    )
    return parser.parse_args()


def repo_key(project: str) -> str:
    return project.replace("/", "__")


def load_expected_repos(input_csv: Path) -> dict[str, str]:
    repos: dict[str, str] = {}
    with input_csv.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            project = row.get("Project", "").strip()
            project_url = row.get("Project URL", "").strip()
            if not project:
                continue
            repos.setdefault(repo_key(project), project_url or f"https://github.com/{project}")
    return repos


def validate_git_repo(repo_dir: Path) -> tuple[bool, str]:
    if not (repo_dir / ".git").exists():
        return False, "missing_dot_git"
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        return False, output or "git_rev_parse_failed"
    if result.stdout.strip().lower() != "true":
        return False, f"unexpected_rev_parse_output:{result.stdout.strip()}"
    return True, ""


def prune_invalid_rows(input_csv: Path, invalid_keys: set[str]) -> int:
    with input_csv.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    kept_rows = []
    removed = 0
    for row in rows:
        project = row.get("Project", "").strip()
        if project and repo_key(project) in invalid_keys:
            removed += 1
            continue
        kept_rows.append(row)

    backup_path = input_csv.with_suffix(input_csv.suffix + ".bak")
    shutil.copy2(input_csv, backup_path)
    with input_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept_rows)
    return removed


def main() -> int:
    args = parse_args()
    input_csv = Path(args.input_csv)
    workspaces_dir = Path(args.workspaces_dir)
    output_json = Path(args.output_json)

    expected = load_expected_repos(input_csv)
    actual_dirs = {
        repo_dir.name.lower(): repo_dir
        for repo_dir in workspaces_dir.iterdir()
        if repo_dir.is_dir()
    } if workspaces_dir.exists() else {}

    valid = []
    invalid = []
    missing = []

    for key, repo_url in sorted(expected.items()):
        repo_dir = actual_dirs.get(key.lower())
        if repo_dir is None:
            missing.append({"repo_key": key, "repo_url": repo_url, "reason": "missing_workspace_dir"})
            continue
        is_valid, reason = validate_git_repo(repo_dir)
        if is_valid:
            valid.append({"repo_key": key, "repo_url": repo_url, "repo_dir": str(repo_dir)})
        else:
            invalid.append({"repo_key": key, "repo_url": repo_url, "repo_dir": str(repo_dir), "reason": reason})

    summary = {
        "expected_repos": len(expected),
        "workspace_directories": len(actual_dirs),
        "valid_repos": len(valid),
        "missing_repos": len(missing),
        "invalid_repos": len(invalid),
        "missing_examples": missing[:20],
        "invalid_examples": invalid[:20],
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with output_json.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    print(f"Expected repos:        {summary['expected_repos']}")
    print(f"Workspace directories: {summary['workspace_directories']}")
    print(f"Valid repos:           {summary['valid_repos']}")
    print(f"Missing repos:         {summary['missing_repos']}")
    print(f"Invalid repos:         {summary['invalid_repos']}")
    print(f"Wrote audit summary to: {output_json}")

    if args.prune_invalid_from_csv and (missing or invalid):
        invalid_keys = {entry["repo_key"] for entry in missing + invalid}
        removed = prune_invalid_rows(input_csv, invalid_keys)
        print(f"Pruned rows from {input_csv}: {removed}")
        print(f"Backup written to: {input_csv.with_suffix(input_csv.suffix + '.bak')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
