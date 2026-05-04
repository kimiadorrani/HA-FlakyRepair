"""
Standalone runner for OD (Order-Dependent) IDOFT tests.

Strategy (validated by experiments on real test code):

  OD-BRIT (brittle) — fails in isolation, needs a prior "setter" test:
    Step 1: run in isolation → if FAIL → brit_confirmed (done, clear signal)
    Step 2: (verification only) run whole file in natural order → should PASS

  OD-VIC (victim) — passes in isolation, fails after a polluter:
    Step 1: run in isolation → should PASS (baseline)
    Step 2: run "target-last" — collect all tests in file, run them all,
            place target at the very end → if target FAILS → vic_confirmed
            (deterministic: guarantees every file-peer runs before target)
    Step 3: fallback — try N random-seed file-runs if step 2 still can't reproduce

  OD (generic): try BRIT check first, then VIC strategy.

Output CSV: datasets/idoft/idoft-od-reproduction-results.csv
Output JSON: results/idoft-od/*.json

Usage:
  .venv/bin/python scripts/idoft/run_idoft_od_test.py --row-index 0 --iterations 20
"""

import argparse
import csv
import fcntl
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_idoft_test import (
    TEST_NAME_COL,
    _run_subprocess,
    _sanitize_container_name,
    build_install_command,
    cleanup_container,
    detect_python_image,
    ensure_persistent_docker_container,
    prepare_repo,
)

OD_RESULTS_CSV = Path("datasets/idoft/idoft-od-reproduction-results.csv")

FIELDNAMES = [
    "Project", "Project URL", "Category", "SHA Detected",
    "Checkout OK", "Test Name",
    "Isolated Result",        # pass / fail / error
    "Target Last Result",     # pass / fail / error / unknown (step 2 for VIC)
    "Random Pass Count",      # target passes across random-seed file-runs (step 3 fallback)
    "Random Fail Count",      # target fails across random-seed file-runs
    "Iterations Requested",
    "Iterations Executed",
    "Reproduced", "Status",   # brit_confirmed / vic_confirmed / could_not_reproduce / error
    "Error", "Result JSON",
]


# ---------------------------------------------------------------------------
# Docker command builders
# ---------------------------------------------------------------------------

def _pytest_isolated(container_name: str, test_name: str) -> list[str]:
    return ["docker", "exec", container_name, "bash", "-c",
            f"pytest -v --tb=short -p no:randomly '{test_name}'"]


def _collect_tests_in_file(container_name: str, test_file: str) -> list[str]:
    """Return ordered list of test node IDs in the file (natural order)."""
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"pytest --collect-only -q -p no:randomly '{test_file}' 2>/dev/null"],
        capture_output=True, text=True, timeout=60,
    )
    tests = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith("=") and not line.startswith("-"):
            tests.append(line)
    return tests


def _pytest_target_last(container_name: str, test_file: str, target: str) -> list[str]:
    """Run all tests in file with target placed last."""
    tests = _collect_tests_in_file(container_name, test_file)
    others = [t for t in tests if t != target]
    if not others:
        return _pytest_isolated(container_name, target)
    ordered = " ".join(f"'{t}'" for t in others) + f" '{target}'"
    return ["docker", "exec", container_name, "bash", "-c",
            f"pytest -v --tb=short -p no:randomly {ordered}"]


def _pytest_file_random(container_name: str, test_file: str, seed: int) -> list[str]:
    return ["docker", "exec", container_name, "bash", "-c",
            f"pytest -v --tb=short --randomly-seed={seed} '{test_file}'"]


def _pytest_file_natural(container_name: str, test_file: str) -> list[str]:
    return ["docker", "exec", container_name, "bash", "-c",
            f"pytest -v --tb=short -p no:randomly '{test_file}'"]


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------

def _extract_test_result(output: str, test_name: str) -> str:
    """Find PASSED/FAILED/ERROR for *test_name* in pytest -v output."""
    candidates = [test_name]
    parts = test_name.split("::")
    if len(parts) > 1:
        candidates.append("::".join(parts[-2:]))
        candidates.append(parts[-1])

    for line in output.splitlines():
        for candidate in candidates:
            if candidate in line:
                if "PASSED" in line:
                    return "pass"
                if "FAILED" in line:
                    return "fail"
                if "ERROR" in line:
                    return "error"
    return "unknown"


# ---------------------------------------------------------------------------
# Core OD reproduction logic
# ---------------------------------------------------------------------------

def run_od(
    container_name: str,
    work_dir: Path,
    iterations: int,
    row: dict,
    results_dir: Path,
    checkout_ok: bool = False,
) -> dict:
    test_name = row[TEST_NAME_COL].strip()
    test_file = test_name.split("::")[0]
    project_name = row["Project URL"].rstrip("/").split("/")[-1]
    category = row.get("Category", "").upper()

    error_msg = ""
    target_last_result = "skipped"
    random_pass_count = 0
    random_fail_count = 0
    first_pass_log = ""
    first_fail_log = ""

    # ------------------------------------------------------------------
    # Step 1: isolated run
    # ------------------------------------------------------------------
    iso_rc, iso_log = _run_subprocess(_pytest_isolated(container_name, test_name), work_dir)
    if iso_log == "Timeout":
        isolated_result = "error"
        error_msg = "Timeout on isolated run"
    else:
        isolated_result = "pass" if iso_rc == 0 else "fail"
    print(f"[{project_name}] isolated: {isolated_result.upper()}")

    if isolated_result == "pass":
        first_pass_log = iso_log
    elif isolated_result == "fail":
        first_fail_log = iso_log

    # ------------------------------------------------------------------
    # Early exit: OD-BRIT confirmed by isolated failure
    # ------------------------------------------------------------------
    if "BRIT" in category and isolated_result == "fail":
        return _save_od_result(
            row=row, results_dir=results_dir,
            isolated_result=isolated_result,
            target_last_result=target_last_result,
            random_pass_count=0, random_fail_count=0,
            iterations_requested=iterations, iterations_executed=0,
            reproduced=True, status="brit_confirmed",
            error_msg="", first_pass_log=first_pass_log,
            first_fail_log=first_fail_log, checkout_ok=checkout_ok,
        )

    if error_msg:
        return _save_od_result(
            row=row, results_dir=results_dir,
            isolated_result=isolated_result,
            target_last_result=target_last_result,
            random_pass_count=0, random_fail_count=0,
            iterations_requested=iterations, iterations_executed=0,
            reproduced=False, status="error",
            error_msg=error_msg, first_pass_log=first_pass_log,
            first_fail_log=first_fail_log, checkout_ok=checkout_ok,
        )

    # ------------------------------------------------------------------
    # Step 2: target-last run
    # For VIC/OD: isolated=PASS, so we try every peer test as potential polluter
    # For BRIT: isolated=PASS (unexpected), still try the ordered approach
    # ------------------------------------------------------------------
    tl_rc, tl_log = _run_subprocess(_pytest_target_last(container_name, test_file, test_name), work_dir)
    target_last_result = _extract_test_result(tl_log, test_name)
    print(f"[{project_name}] target-last: {target_last_result.upper()}")

    if target_last_result == "fail":
        first_fail_log = tl_log
        status = "vic_confirmed" if "VIC" in category or category == "OD" else "brit_confirmed"
        return _save_od_result(
            row=row, results_dir=results_dir,
            isolated_result=isolated_result,
            target_last_result=target_last_result,
            random_pass_count=0, random_fail_count=0,
            iterations_requested=iterations, iterations_executed=1,
            reproduced=True, status=status,
            error_msg="", first_pass_log=first_pass_log,
            first_fail_log=first_fail_log, checkout_ok=checkout_ok,
        )
    elif target_last_result == "pass" and not first_pass_log:
        first_pass_log = tl_log

    # ------------------------------------------------------------------
    # Step 3: random-seed fallback (VIC / OD only)
    # ------------------------------------------------------------------
    print(f"[{project_name}] target-last did not reproduce — trying {iterations} random seeds...")
    for i in range(iterations):
        rc, out = _run_subprocess(_pytest_file_random(container_name, test_file, seed=i), work_dir)
        target = _extract_test_result(out, test_name)

        if target == "pass":
            random_pass_count += 1
            if not first_pass_log:
                first_pass_log = out
        elif target in ("fail", "error"):
            random_fail_count += 1
            if not first_fail_log:
                first_fail_log = out

        print(f"[{project_name}] random-seed [{i+1}/{iterations}] target={target.upper()}")

        if random_fail_count > 0:
            print(f"[{project_name}] Reproduced via random seed {i}!")
            break

    # ------------------------------------------------------------------
    # Final status
    # ------------------------------------------------------------------
    if random_fail_count > 0:
        status = "vic_confirmed" if "VIC" in category or category == "OD" else "brit_confirmed"
        reproduced = True
    elif "BRIT" in category and isolated_result == "fail":
        status = "brit_confirmed"
        reproduced = True
    else:
        status = "could_not_reproduce"
        reproduced = False

    return _save_od_result(
        row=row, results_dir=results_dir,
        isolated_result=isolated_result,
        target_last_result=target_last_result,
        random_pass_count=random_pass_count,
        random_fail_count=random_fail_count,
        iterations_requested=iterations,
        iterations_executed=random_pass_count + random_fail_count + 1,
        reproduced=reproduced, status=status,
        error_msg=error_msg, first_pass_log=first_pass_log,
        first_fail_log=first_fail_log, checkout_ok=checkout_ok,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _save_od_result(
    row: dict,
    results_dir: Path,
    isolated_result: str,
    target_last_result: str,
    random_pass_count: int,
    random_fail_count: int,
    iterations_requested: int,
    iterations_executed: int,
    reproduced: bool,
    status: str,
    error_msg: str,
    first_pass_log: str,
    first_fail_log: str,
    checkout_ok: bool,
) -> dict:
    results_dir.mkdir(parents=True, exist_ok=True)
    project_name = row["Project URL"].rstrip("/").split("/")[-1]
    safe_test = _sanitize_container_name(row[TEST_NAME_COL][:60])
    output_path = results_dir / f"{project_name}-{safe_test}.json"

    res_dict = {
        "isolated_result": isolated_result,
        "target_last_result": target_last_result,
        "random_pass_count": random_pass_count,
        "random_fail_count": random_fail_count,
        "iterations_requested": iterations_requested,
        "iterations_executed": iterations_executed,
        "reproduced": reproduced,
        "status": status,
        "error": error_msg,
        "checkout_ok": checkout_ok,
        "first_pass_log": first_pass_log,
        "first_fail_log": first_fail_log,
        "result_json": str(output_path),
    }

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(res_dict, f, indent=2)

    update_od_results_csv(row, res_dict)
    return res_dict


def update_od_results_csv(row: dict, result: dict) -> None:
    project_url = row.get("Project URL", "")
    project_name = project_url.rstrip("/").split("/")[-1]
    test_name = row.get(TEST_NAME_COL, "")
    key = (project_url.strip(), test_name.strip())

    output_row = {
        "Project": project_name,
        "Project URL": project_url,
        "Category": row.get("Category", ""),
        "SHA Detected": row.get("SHA Detected", ""),
        "Checkout OK": str(result.get("checkout_ok", False)).lower(),
        "Test Name": test_name,
        "Isolated Result": result.get("isolated_result", ""),
        "Target Last Result": result.get("target_last_result", ""),
        "Random Pass Count": result.get("random_pass_count", ""),
        "Random Fail Count": result.get("random_fail_count", ""),
        "Iterations Requested": result.get("iterations_requested", ""),
        "Iterations Executed": result.get("iterations_executed", ""),
        "Reproduced": str(result.get("reproduced", False)).lower(),
        "Status": result.get("status", ""),
        "Error": result.get("error", ""),
        "Result JSON": result.get("result_json", ""),
    }

    with open(OD_RESULTS_CSV, "a+", encoding="utf-8", newline="") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        content = f.read()
        existing_rows = list(csv.DictReader(content.splitlines())) if content.strip() else []

        replaced = False
        new_rows = []
        for existing in existing_rows:
            if (existing.get("Project URL", "").strip(), existing.get("Test Name", "").strip()) == key:
                new_rows.append(output_row)
                replaced = True
            else:
                new_rows.append(existing)
        if not replaced:
            new_rows.append(output_row)

        f.seek(0)
        f.truncate()
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(new_rows)
        fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Entry point (standalone single-row usage)
# ---------------------------------------------------------------------------

def execute_od_row(
    row_index: int,
    row: dict,
    workspaces_dir: Path,
    results_dir: Path,
    iterations: int,
) -> dict:
    project_name = row["Project URL"].rstrip("/").split("/")[-1]
    category = row.get("Category", "").upper()
    repo_dir = None
    try:
        repo_dir, checkout_ok = prepare_repo(row, workspaces_dir)
        image = detect_python_image(repo_dir)
        container_name = ensure_persistent_docker_container(repo_dir, image)

        print(f"[{project_name}] Installing dependencies...")
        install_result = subprocess.run(
            build_install_command(repo_dir, container_name),
            cwd=repo_dir, capture_output=True, text=True, timeout=360,
        )
        if install_result.returncode != 0:
            print(f"[{project_name}] Warning: install had errors:\n{install_result.stdout[-300:]}")

        print(f"Running OD test [{row_index}] ({category}) {project_name} :: {row[TEST_NAME_COL][:70]}")
        return run_od(container_name, repo_dir, iterations, row, results_dir, checkout_ok=checkout_ok)

    except Exception as e:
        print(f"[{project_name}] Error: {e}")
        error_result = {
            "isolated_result": "error", "target_last_result": "skipped",
            "random_pass_count": 0, "random_fail_count": 0,
            "iterations_requested": iterations, "iterations_executed": 0,
            "reproduced": False, "status": "error", "error": str(e),
            "checkout_ok": False, "result_json": "",
        }
        update_od_results_csv(row, error_result)
        return error_result
    finally:
        if repo_dir:
            try:
                cleanup_container(repo_dir)
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a single OD IDOFT test row.")
    parser.add_argument("--row-index", type=int, required=True)
    parser.add_argument("--iterations", type=int, default=20,
                        help="Random-seed fallback iterations (used only if target-last fails).")
    parser.add_argument("--input-csv", default="datasets/idoft/raw/py-data-od.csv")
    args = parser.parse_args()

    with open(args.input_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if 0 <= args.row_index < len(rows):
        execute_od_row(
            args.row_index, rows[args.row_index],
            Path("workspaces/idoft"), Path("results/idoft-od"),
            args.iterations,
        )
    else:
        print(f"Invalid row index: {args.row_index} (max {len(rows)-1})")
