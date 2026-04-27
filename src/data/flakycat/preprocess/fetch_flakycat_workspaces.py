"""
Clone FLAKYCAT Java project repositories into a separate workspace tree.

Repos are written to `workspaces/flakycat/<owner>__<repo>` so they remain
distinguishable from the IDOFT Python workspaces in `workspaces/idoft/`.

Example:
  .venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces --limit 3
  .venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces --prune-failed-from-csv
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[4]
DEFAULT_INPUT_CSV = ROOT_DIR / "datasets" / "flakycat" / "flakycat-java-tests.csv"
DEFAULT_WORKSPACES_DIR = ROOT_DIR / "workspaces" / "flakycat"
DEFAULT_FAILURE_CSV = ROOT_DIR / "datasets" / "flakycat" / "flakycat-clone-failures.csv"
FAILURE_FIELDNAMES = ["Repo Key", "Clone URL", "Failure Reason"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clone missing FLAKYCAT Java repositories.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV), help="FLAKYCAT metadata CSV path")
    parser.add_argument("--workspaces-dir", default=str(DEFAULT_WORKSPACES_DIR), help="Clone destination root")
    parser.add_argument("--limit", type=int, help="Optional cap on missing repos to clone")
    parser.add_argument(
        "--clone-timeout",
        type=int,
        default=180,
        help="Seconds to wait per git clone before treating it as failed",
    )
    parser.add_argument(
        "--failure-csv",
        default=str(DEFAULT_FAILURE_CSV),
        help="CSV path for repos that failed cloning",
    )
    parser.add_argument(
        "--prune-failed-from-csv",
        action="store_true",
        help="Remove rows from the metadata CSV when their repo fails cloning",
    )
    return parser.parse_args()


def repo_key(repo_url: str) -> str:
    parsed = urlparse(repo_url.rstrip("/"))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Not a GitHub repo URL: {repo_url}")
    return f"{parts[0]}__{parts[1].removesuffix('.git')}"


def iter_unique_repos(input_csv: Path) -> list[tuple[str, str]]:
    repos: dict[str, str] = {}
    with input_csv.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            repo_url = row.get("Project URL", "").strip()
            if not repo_url:
                continue
            key = repo_key(repo_url)
            repos.setdefault(key, repo_url.rstrip("/") + ".git")
    return sorted(repos.items())


def write_failure_csv(failure_csv: Path, failures: list[dict[str, str]]) -> None:
    failure_csv.parent.mkdir(parents=True, exist_ok=True)
    with failure_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FAILURE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(failures)


def prune_failed_rows(input_csv: Path, failed_keys: set[str]) -> int:
    with input_csv.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    kept_rows = []
    removed = 0
    for row in rows:
        repo_url = row.get("Project URL", "").strip()
        if repo_url and repo_key(repo_url) in failed_keys:
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


def clone_missing_repos(
    repos: list[tuple[str, str]],
    workspaces_dir: Path,
    clone_timeout: int,
    limit: int | None = None,
) -> tuple[int, int, list[dict[str, str]]]:
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    cloned = 0
    failed = 0
    failures: list[dict[str, str]] = []

    for key, clone_url in repos:
        target_dir = workspaces_dir / key
        if target_dir.exists():
            continue
        if limit is not None and cloned >= limit:
            break

        print(f"Cloning {clone_url} -> {target_dir}")
        try:
            result = subprocess.run(
                ["git", "clone", clone_url, str(target_dir)],
                capture_output=True,
                text=True,
                timeout=clone_timeout,
            )
        except subprocess.TimeoutExpired:
            failed += 1
            shutil.rmtree(target_dir, ignore_errors=True)
            reason = f"clone_timeout_after_{clone_timeout}s"
            failures.append({"Repo Key": key, "Clone URL": clone_url, "Failure Reason": reason})
            print(f"FAILED {clone_url}")
            print(reason)
            continue
        if result.returncode == 0:
            cloned += 1
            continue

        failed += 1
        shutil.rmtree(target_dir, ignore_errors=True)
        print(f"FAILED {clone_url}")
        output = (result.stderr or result.stdout or "").strip()
        reason = output.splitlines()[-1] if output else "unknown_clone_error"
        failures.append({"Repo Key": key, "Clone URL": clone_url, "Failure Reason": reason})
        if output:
            print(output)

    return cloned, failed, failures


def main() -> None:
    args = parse_args()
    input_csv = Path(args.input_csv)
    workspaces_dir = Path(args.workspaces_dir)
    failure_csv = Path(args.failure_csv)
    repos = iter_unique_repos(input_csv)
    existing = {path.name for path in workspaces_dir.iterdir()} if workspaces_dir.exists() else set()
    missing = [(key, url) for key, url in repos if key not in existing]

    print(f"FLAKYCAT repos with clone URLs: {len(repos)}")
    print(f"Already present in {workspaces_dir}: {len(repos) - len(missing)}")
    print(f"Missing repos to clone: {len(missing)}")

    cloned, failed, failures = clone_missing_repos(
        missing,
        workspaces_dir,
        clone_timeout=args.clone_timeout,
        limit=args.limit,
    )
    write_failure_csv(failure_csv, failures)
    print(f"Cloned: {cloned}")
    print(f"Failed: {failed}")
    print(f"Failure CSV: {failure_csv}")

    if args.prune_failed_from_csv and failures:
        failed_keys = {failure["Repo Key"] for failure in failures}
        removed = prune_failed_rows(input_csv, failed_keys)
        print(f"Pruned rows from {input_csv}: {removed}")
        print(f"Backup written to: {input_csv.with_suffix(input_csv.suffix + '.bak')}")


if __name__ == "__main__":
    main()
