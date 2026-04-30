import argparse
import csv
import subprocess
import concurrent.futures
from pathlib import Path
from collections import defaultdict

def run_project(project_rows: list, iterations: int):
    # Run the tests for a single project sequentially to avoid Git/Docker clashes
    for row_index, row in project_rows:
        cmd = [
            ".venv/bin/python", "scripts/uiflaky/run_uiflaky_test.py",
            "--row-index", str(row_index),
            "--iterations", str(iterations)
        ]
        subprocess.run(cmd)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20, help="Number of iterations per test")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel workers (projects)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of tests to run")
    args = parser.parse_args()

    metadata_path = Path("datasets/uiflaky/preprocessed/uiflaky-metadata.csv")
    if not metadata_path.exists():
        print(f"Metadata not found at {metadata_path}")
        return

    rows = []
    with open(metadata_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if args.limit:
        rows = rows[:args.limit]

    # Group by project to avoid container and git checkout clashing
    projects = defaultdict(list)
    for i, row in enumerate(rows):
        projects[row['Project']].append((i, row))

    print(f"Starting UI-Flaky reproduction batch for {len(rows)} tests across {len(projects)} projects...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = []
        for project_name, project_rows in projects.items():
            futures.append(executor.submit(run_project, project_rows, args.iterations))
        
        concurrent.futures.wait(futures)

    print("\nBatch run completed. Check results/uiflaky/ for details.")

if __name__ == "__main__":
    main()
