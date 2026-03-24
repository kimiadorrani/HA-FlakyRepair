"""
Fetch missing workspaces from the dataset CSV.

This script:
- reads `src/data/py-data.csv`
- normalizes GitHub repo/issue/PR URLs to cloneable repo URLs
- skips repos already present in `workspaces/`
- clones missing repos into `workspaces/<repo_name>`

Example:
  .venv/bin/python -m src.data.preprocess.fetch_workspaces
"""

from __future__ import annotations

import argparse
import csv
import subprocess
from pathlib import Path
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[3]
DATASET_PATH = ROOT_DIR / "src" / "data" / "py-data.csv"
WORKSPACES_DIR = ROOT_DIR / "workspaces"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clone missing workspaces from the dataset CSV.")
    parser.add_argument(
        "--input-csv",
        default=str(DATASET_PATH),
        help="Path to the source dataset CSV",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional cap on the number of missing repos to clone",
    )
    parser.add_argument(
        "--include-od",
        action="store_true",
        default=False,
        help="Include OD/OD-Vic/OD-Brit rows when scanning the dataset CSV",
    )
    return parser.parse_args()


def normalize_github_repo_url(raw_url: str) -> tuple[str, str] | None:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() != "github.com":
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None

    owner, repo = parts[0], parts[1]
    clone_url = f"https://github.com/{owner}/{repo}.git"
    return clone_url, repo


def iter_unique_repos(
    csv_path: Path,
    include_od: bool = False,
) -> list[tuple[str, str]]:
    repos: dict[str, str] = {}
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_url = row.get("Project URL", "").strip()
            if not raw_url:
                continue
            category = row.get("Category", "").strip().upper()
            if not include_od and category.startswith("OD"):
                continue
            normalized = normalize_github_repo_url(raw_url)
            if not normalized:
                continue
            clone_url, repo_name = normalized
            repos.setdefault(repo_name, clone_url)
    return sorted(repos.items())


def clone_missing_repos(repos: list[tuple[str, str]], limit: int | None = None) -> tuple[int, int]:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

    cloned = 0
    failed = 0

    for repo_name, clone_url in repos:
        target_dir = WORKSPACES_DIR / repo_name
        if target_dir.exists():
            continue
        if limit is not None and cloned >= limit:
            break

        print(f"Cloning {clone_url} -> {target_dir}")
        result = subprocess.run(
            ["git", "clone", clone_url, str(target_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            cloned += 1
            continue

        failed += 1
        print(f"FAILED {clone_url}")
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        if stderr:
            print(stderr)
        elif stdout:
            print(stdout)

    return cloned, failed


def main() -> None:
    args = parse_args()
    repos = iter_unique_repos(Path(args.input_csv), include_od=args.include_od)
    existing = {path.name for path in WORKSPACES_DIR.iterdir()} if WORKSPACES_DIR.exists() else set()
    missing = [(name, url) for name, url in repos if name not in existing]

    print(f"Dataset repos (normalized): {len(repos)}")
    print(f"Already present in workspaces/: {len(existing & {name for name, _ in repos})}")
    print(f"Missing repos to clone: {len(missing)}")

    cloned, failed = clone_missing_repos(missing, limit=args.limit)
    print(f"Cloned: {cloned}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    main()
