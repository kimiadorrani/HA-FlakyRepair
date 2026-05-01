"""
Batch runner for the IDOFT Python flaky-test dataset.

Reads rows from the input CSV, groups them by project, and runs each test
repeatedly inside a persistent Docker container to check for flakiness.

Examples:
  # Fresh run from the raw dataset (skips OD rows by default)
  PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_batch.py --iterations 10

  # Retry only tests that previously failed or errored
  PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_batch.py \\
      --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv \\
      --retry-statuses execution_error could_not_reproduce \\
      --iterations 10

  # Single project
  PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_batch.py \\
      --project webssh --iterations 10

  # Limit to first N tests (debugging)
  PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_batch.py --limit 5
"""

import argparse
import concurrent.futures
import csv
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# Allow importing run_idoft_test as a module (both files live in scripts/idoft/)
sys.path.insert(0, str(Path(__file__).parent))

TEST_NAME_COL = (
    "Pytest Test Name "
    "(PathToFile::TestClass::TestMethod or PathToFile::TestMethod)"
)

RESULTS_CSV = Path("datasets/idoft/idoft-reproduction-results.csv")


def load_done_keys() -> set[tuple[str, str]]:
    """Return (Project URL, Test Name) pairs already recorded in the results CSV."""
    if not RESULTS_CSV.exists():
        return set()
    done: set[tuple[str, str]] = set()
    with open(RESULTS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row.get("Project URL", "").strip()
            test = row.get("Test Name", "").strip()
            if url and test:
                done.add((url, test))
    return done


def drop_result_rows(statuses: list[str]) -> int:
    """Remove rows with given Status values from the results CSV. Returns count removed."""
    if not RESULTS_CSV.exists():
        return 0
    with open(RESULTS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        all_rows = list(reader)

    keep = [r for r in all_rows if r.get("Status", "") not in statuses]
    removed = len(all_rows) - len(keep)

    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(keep)

    return removed


def load_rows(
    input_csv: Path,
    skip_od: bool = True,
    project_filter: str | None = None,
    retry_statuses: list[str] | None = None,
    skip_already_reproduced: bool = True,
    resume: bool = True,
    limit: int | None = None,
) -> list[tuple[int, dict]]:
    rows: list[tuple[int, dict]] = []
    idx = 0
    done_keys = load_done_keys() if resume else set()

    with open(input_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            category = row.get("Category", "").upper()
            if skip_od and category.startswith("OD"):
                continue

            project_url = row.get("Project URL", "").strip()
            if not project_url:
                continue

            test_name = row.get(TEST_NAME_COL, "").strip()
            if not test_name:
                continue

            project_name = project_url.rstrip("/").split("/")[-1]
            if project_filter and project_name.lower() != project_filter.lower():
                continue

            # If retry_statuses given, only include rows matching those statuses
            if retry_statuses:
                preprocess_status = row.get("Preprocess Status", "").strip()
                if preprocess_status not in retry_statuses:
                    continue
            elif skip_already_reproduced:
                # Skip rows already confirmed reproduced
                if row.get("Reproduced", "").strip().lower() == "true":
                    continue

            # Resume: skip rows already recorded in the results CSV
            if done_keys and (project_url, test_name) in done_keys:
                continue

            rows.append((idx, row))
            idx += 1

            if limit and len(rows) >= limit:
                break

    return rows


def run_project(
    project_rows: list[tuple[int, dict]],
    iterations: int,
) -> None:
    import run_idoft_test as runner

    first_row = project_rows[0][1]
    project_name = first_row["Project URL"].rstrip("/").split("/")[-1]
    workspaces_dir = Path("workspaces/idoft")
    results_dir = Path("results/idoft")

    repo_dir = None
    try:
        repo_dir, checkout_ok = runner.prepare_repo(first_row, workspaces_dir)
        image = runner.detect_python_image(repo_dir)
        container_name = runner.ensure_persistent_docker_container(repo_dir, image)

        print(f"[{project_name}] Installing dependencies (once for {len(project_rows)} tests)...")
        install_result = subprocess.run(
            runner.build_install_command(repo_dir, container_name),
            cwd=repo_dir, capture_output=True, text=True, timeout=360,
        )
        if install_result.returncode != 0:
            print(f"[{project_name}] Warning: install had errors:\n{install_result.stdout[-300:]}")

        for row_index, row in project_rows:
            test_name = row.get(TEST_NAME_COL, "").strip()
            category = row.get("Category", "").upper()
            print(f"  → [{row_index}] {project_name} :: {test_name[:70]}")
            if "NIO" in category:
                runner.run_nio(container_name, repo_dir, iterations, row, results_dir, checkout_ok=checkout_ok)
            else:
                cmd = runner._pytest_cmd(container_name, test_name)
                runner.run_repeated(cmd, repo_dir, iterations, row, results_dir, checkout_ok=checkout_ok)

    except Exception as e:
        print(f"[{project_name}] Project-level error: {e}")
        for _row_index, row in project_rows:
            runner.update_results_csv(row, {
                "error": str(e), "fail_count": 0, "pass_count": 0,
                "iterations_executed": 0, "reproduced": False,
                "iterations_requested": iterations, "checkout_ok": False,
            })
    finally:
        if repo_dir:
            runner.cleanup_container(repo_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch reproduce IDOFT Python flaky tests in Docker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input-csv",
        default="datasets/idoft/raw/py-data.csv",
        help="Source dataset CSV (raw or preprocessed).",
    )
    parser.add_argument("--iterations", type=int, default=100,
                        help="Iterations per test.")
    parser.add_argument("--workers", type=int, default=2,
                        help="Parallel project workers.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap total number of tests to run.")
    parser.add_argument("--project", default=None,
                        help="Only run tests from this project name.")
    parser.add_argument(
        "--retry-statuses", nargs="+", default=None,
        metavar="STATUS",
        help=(
            "Only run rows with these Preprocess Status values "
            "(e.g. execution_error could_not_reproduce). "
            "Only useful with a preprocessed CSV."
        ),
    )
    parser.add_argument(
        "--include-reproduced", action="store_true", default=False,
        help="Also run rows already marked as reproduced (re-verify).",
    )
    parser.add_argument(
        "--include-od", action="store_true", default=False,
        help="Include OD/OD-Vic/OD-Brit rows (skipped by default).",
    )
    parser.add_argument(
        "--no-resume", action="store_true", default=False,
        help="Ignore existing results CSV and re-run all matching rows.",
    )

    parser.add_argument(
        "--retry-result-status", nargs="+", default=None,
        metavar="STATUS",
        help=(
            "Remove rows with these Status values from the results CSV and re-run them. "
            "E.g. --retry-result-status error always_pass"
        ),
    )
    args = parser.parse_args()

    input_csv = Path(args.input_csv)
    if not input_csv.exists():
        print(f"Input CSV not found: {input_csv}")
        return

    if args.retry_result_status:
        removed = drop_result_rows(args.retry_result_status)
        print(f"Dropped {removed} rows with status {args.retry_result_status} from results CSV — will re-run them.")

    indexed_rows = load_rows(
        input_csv,
        skip_od=not args.include_od,
        project_filter=args.project,
        retry_statuses=args.retry_statuses,
        skip_already_reproduced=not args.include_reproduced,
        resume=not args.no_resume,
        limit=args.limit,
    )

    if not indexed_rows:
        already_done = len(load_done_keys()) if not args.no_resume else 0
        print(f"No matching rows found. ({already_done} already in results CSV, use --no-resume to re-run them)")
        return

    # Group by project so we reuse the same Docker container
    projects: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for row_index, row in indexed_rows:
        project_name = row["Project URL"].rstrip("/").split("/")[-1]
        projects[project_name].append((row_index, row))

    print(
        f"Starting IDOFT reproduction batch: "
        f"{len(indexed_rows)} tests across {len(projects)} projects "
        f"(workers={args.workers}, iterations={args.iterations})"
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(run_project, project_rows, args.iterations)
            for project_rows in projects.values()
        ]
        concurrent.futures.wait(futures)

    print("\nBatch completed. Results → datasets/idoft/idoft-reproduction-results.csv")


if __name__ == "__main__":
    main()
