"""
Batch runner for OD (Order-Dependent) IDOFT tests.

Groups rows by project, reuses one Docker container per project, installs deps
once, then runs all OD tests for that project before tearing down the container.

Examples:
  # Run all OD tests
  PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_od_batch.py --iterations 30

  # Single project
  PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_od_batch.py \\
      --project webssh --iterations 30

  # Only OD-VIC rows
  PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_od_batch.py \\
      --category OD-VIC --iterations 30

  # Retry errors
  PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_od_batch.py \\
      --retry-result-status error could_not_reproduce --iterations 30
"""

import argparse
import concurrent.futures
import csv
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TEST_NAME_COL = (
    "Pytest Test Name "
    "(PathToFile::TestClass::TestMethod or PathToFile::TestMethod)"
)

OD_RESULTS_CSV = Path("datasets/idoft/idoft-od-reproduction-results.csv")


def load_done_keys() -> set[tuple[str, str]]:
    if not OD_RESULTS_CSV.exists():
        return set()
    done: set[tuple[str, str]] = set()
    with open(OD_RESULTS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row.get("Project URL", "").strip()
            test = row.get("Test Name", "").strip()
            if url and test:
                done.add((url, test))
    return done


def drop_result_rows(statuses: list[str]) -> int:
    if not OD_RESULTS_CSV.exists():
        return 0
    with open(OD_RESULTS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        all_rows = list(reader)
    keep = [r for r in all_rows if r.get("Status", "") not in statuses]
    removed = len(all_rows) - len(keep)
    with open(OD_RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(keep)
    return removed


def load_rows(
    input_csv: Path,
    project_filter: str | None = None,
    category_filter: str | None = None,
    resume: bool = True,
    limit: int | None = None,
) -> list[tuple[int, dict]]:
    rows: list[tuple[int, dict]] = []
    idx = 0
    done_keys = load_done_keys() if resume else set()

    with open(input_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            project_url = row.get("Project URL", "").strip()
            if not project_url:
                continue
            test_name = row.get(TEST_NAME_COL, "").strip()
            if not test_name:
                continue

            project_name = project_url.rstrip("/").split("/")[-1]
            if project_filter and project_name.lower() != project_filter.lower():
                continue

            category = row.get("Category", "").upper()
            if category_filter and category_filter.upper() not in category:
                continue

            if done_keys and (project_url, test_name) in done_keys:
                continue

            rows.append((idx, row))
            idx += 1

            if limit and len(rows) >= limit:
                break

    return rows


def run_od_project(
    project_rows: list[tuple[int, dict]],
    iterations: int,
) -> None:
    import run_idoft_od_test as od_runner
    import run_idoft_test as runner

    first_row = project_rows[0][1]
    project_name = first_row["Project URL"].rstrip("/").split("/")[-1]
    workspaces_dir = Path("workspaces/idoft")
    results_dir = Path("results/idoft-od")

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
            print(f"  → [{row_index}] ({category}) {project_name} :: {test_name[:70]}")
            od_runner.run_od(container_name, repo_dir, iterations, row, results_dir, checkout_ok=checkout_ok)

    except Exception as e:
        print(f"[{project_name}] Project-level error: {e}")
        for _row_index, row in project_rows:
            od_runner.update_od_results_csv(row, {
                "isolated_result": "error", "file_pass_count": 0, "file_fail_count": 0,
                "iterations_requested": iterations, "iterations_executed": 0,
                "reproduced": False, "status": "error", "error": str(e),
                "checkout_ok": False, "result_json": "",
            })
    finally:
        if repo_dir:
            import run_idoft_test as runner
            runner.cleanup_container(repo_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch reproduce OD IDOFT tests in Docker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input-csv", default="datasets/idoft/raw/py-data-od.csv")
    parser.add_argument("--iterations", type=int, default=30,
                        help="Number of random-order file runs per test.")
    parser.add_argument("--workers", type=int, default=2,
                        help="Parallel project workers.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap total number of tests to run.")
    parser.add_argument("--project", default=None,
                        help="Only run tests from this project name.")
    parser.add_argument("--category", default=None,
                        help="Filter by category substring, e.g. OD-VIC, OD-BRIT, OD.")
    parser.add_argument("--no-resume", action="store_true", default=False,
                        help="Ignore existing results and re-run all matching rows.")
    parser.add_argument(
        "--retry-result-status", nargs="+", default=None,
        metavar="STATUS",
        help="Remove rows with these Status values and re-run them. "
             "E.g. --retry-result-status error could_not_reproduce",
    )
    args = parser.parse_args()

    input_csv = Path(args.input_csv)
    if not input_csv.exists():
        print(f"Input CSV not found: {input_csv}")
        return

    if args.retry_result_status:
        removed = drop_result_rows(args.retry_result_status)
        print(f"Dropped {removed} rows with status {args.retry_result_status} — will re-run them.")

    indexed_rows = load_rows(
        input_csv,
        project_filter=args.project,
        category_filter=args.category,
        resume=not args.no_resume,
        limit=args.limit,
    )

    if not indexed_rows:
        already_done = len(load_done_keys()) if not args.no_resume else 0
        print(f"No matching rows found. ({already_done} already in results CSV)")
        return

    projects: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for row_index, row in indexed_rows:
        project_name = row["Project URL"].rstrip("/").split("/")[-1]
        projects[project_name].append((row_index, row))

    print(
        f"Starting OD batch: {len(indexed_rows)} tests across {len(projects)} projects "
        f"(workers={args.workers}, iterations={args.iterations})"
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(run_od_project, project_rows, args.iterations)
            for project_rows in projects.values()
        ]
        concurrent.futures.wait(futures)

    print("\nBatch completed. Results → datasets/idoft/idoft-od-reproduction-results.csv")


if __name__ == "__main__":
    main()
