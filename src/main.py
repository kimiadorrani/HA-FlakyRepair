"""
HA-FlakyRepair — Main Entry Point.

Normal run  : detection → repair (for reproduced tests)
Repair-only : --from-detection <session_dir>  skips detection, runs repair
              on tests already confirmed as flaky in a previous session.

Usage examples:
  python -m src.main

  python -m src.main --project plcx

  python -m src.main --project PyGraph cloudnetpy compare-mt coo

  python -m src.main --no-repair           # detection only

  python -m src.main --from-detection results/2026-05-05_09-52-56

  python -m src.main --from-detection results/2026-05-05_09-52-56 --project plcx

  python -m src.main --detection-model minimax --repair-model gpt-4o
"""

import os
import csv
import json
import logging
import sys
import argparse
import random
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

from src.agents.repair import repair_agent_node
from src.orchestrator import build_graph
from src.tools.result_logger import ResultLogger, RESULTS_DIR
from src.tools.docker_infra import reset_docker_environment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(BASE_DIR, "..")
MERGED_CSV   = os.path.join(PROJECT_ROOT, "datasets", "idoft", "idoft-merged-reproduction-results.csv")
WORKSPACE_DIR = os.path.join(PROJECT_ROOT, "workspaces", "idoft")


def get_available_workspaces() -> set[str]:
    if not os.path.isdir(WORKSPACE_DIR):
        return set()
    return {
        name for name in os.listdir(WORKSPACE_DIR)
        if os.path.isdir(os.path.join(WORKSPACE_DIR, name)) and not name.startswith(".")
    }


def load_dataset(
    available_projects: set[str],
    data_file: str = MERGED_CSV,
    project_filter: list[str] | None = None,
    skip_projects: set[str] | None = None,
    test_filter: str | None = None,
    category_filter: str | None = None,
    limit: int | None = None,
    reproduced_only: bool = True,
    shuffle: bool = False,
) -> list[dict]:
    """
    Read the merged reproduction CSV and return runnable test rows.

    Rows are included when:
    - The project workspace is cloned locally
    - reproduced_only=True (default): only rows where Reproduced == true
    - Optional project / test / category filters applied

    The ground-truth Category column is loaded but never passed to the
    detection agent — it is used only for accuracy evaluation afterwards.
    """
    tests = []
    normalized_skip = {p.lower() for p in (skip_projects or set())}

    with open(data_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get("Project URL", "").strip()
            if not url:
                continue

            project_name = url.rstrip("/").split("/")[-1]

            if project_name not in available_projects:
                continue
            if project_name.lower() in normalized_skip:
                continue

            # The merged CSV uses "Test Name"; fall back to the raw py-data.csv column
            test_name = (
                row.get("Test Name")
                or row.get(
                    "Pytest Test Name "
                    "(PathToFile::TestClass::TestMethod or "
                    "PathToFile::TestMethod)"
                )
                or ""
            ).strip()
            category = row.get("Category", "").strip()

            if not test_name or not category:
                continue

            # Skip categories that aren't meaningful flakiness types
            if category.upper() in ("ID", "UD"):
                continue

            reproduced_value = row.get("Reproduced", "").strip().lower()
            if reproduced_only and reproduced_value != "true":
                continue

            if project_filter and project_name.lower() not in {p.lower() for p in project_filter}:
                continue
            if test_filter and test_filter.lower() not in test_name.lower():
                continue
            if category_filter and category.upper() != category_filter.upper():
                continue

            tests.append({
                "project_url":  url,
                "project_name": project_name,
                "sha_detected": row.get("SHA Detected", "").strip(),
                "test_name":    test_name,
                "category":     category,
            })

    if shuffle:
        random.shuffle(tests)
        
    if limit:
        # Limit by number of *projects*, not individual test cases.
        unique_projects = []
        for t in tests:
            if t["project_name"] not in unique_projects:
                unique_projects.append(t["project_name"])
        
        selected_projects = set(unique_projects[:limit])
        tests = [t for t in tests if t["project_name"] in selected_projects]

    return tests


def parse_args() -> argparse.Namespace:
    from src.config.models import list_models
    parser = argparse.ArgumentParser(
        description="HA-FlakyRepair — Flaky Test Detection Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project", "-p", metavar="NAME", nargs="+",
                        help="Only run tests from these project(s) (space-separated)")
    parser.add_argument("--test", "-t", metavar="PATTERN",
                        help="Only run tests whose name contains this string")
    parser.add_argument("--category", "-c", metavar="CATEGORY",
                        help="Only run tests with this category (NIO, NOD, OD-Vic, OD-Brit, OD)")
    parser.add_argument("--limit", "-n", type=int, metavar="N",
                        help="Stop after N tests")
    parser.add_argument("--input-csv", default=MERGED_CSV,
                        help="Path to input dataset CSV (default: merged reproduction results)")
    parser.add_argument("--include-not-reproduced", action="store_true", default=False,
                        help="Include rows not marked as reproduced")
    parser.add_argument("--shuffle", action="store_true", default=False,
                        help="Shuffle the matching tests randomly before applying limit")
    parser.add_argument(
        "--detection-model", default="gpt-oss", metavar="MODEL_KEY",
        help=f"Model key for the Detection Agent. Available: {list_models()} (default: gpt-oss)",
    )
    parser.add_argument(
        "--repair-model", default="gpt-oss", metavar="MODEL_KEY",
        help=f"Model key for the Repair Agent. Available: {list_models()} (default: gpt-oss)",
    )
    parser.add_argument(
        "--no-repair", action="store_true", default=False,
        help="Run detection only — skip the repair agent even when flakiness is reproduced",
    )
    parser.add_argument(
        "--from-detection", metavar="SESSION_DIR", default=None,
        help="Skip detection; run repair on reproduced tests from a previous session directory",
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=1, metavar="N",
        help="Number of parallel project workers (default: 1). Each worker runs one project's tests sequentially.",
    )
    return parser.parse_args()


# ── Repair-from-saved-detection ────────────────────────────────────────────

def _get_project_url(project_name: str, saved_url: str) -> str:
    """
    Return the project's remote URL.  saved_url is used when present (new
    format); otherwise the git remote of the workspace is read as a fallback
    (old format where project_url was not persisted).
    """
    if saved_url:
        return saved_url
    workspace = os.path.join(WORKSPACE_DIR, project_name)
    if os.path.isdir(workspace):
        import subprocess as _sp
        r = _sp.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace, capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    return ""


def _load_detection_results(session_dir: str, project_filter: list[str] | None = None) -> list[dict]:
    """
    Load reproduced tests from a previous session's detection output.

    Handles both the new layout (detection/<project>.json) and the legacy
    flat layout (<project>.json at session root).
    """
    detection_dir = os.path.join(session_dir, "detection")
    search_dir = detection_dir if os.path.isdir(detection_dir) else session_dir

    tests = []
    for fname in sorted(os.listdir(search_dir)):
        if not fname.endswith(".json") or fname == "summary.json":
            continue
        project_name = fname[:-5]
        if project_filter and project_name.lower() not in {p.lower() for p in project_filter}:
            continue

        with open(os.path.join(search_dir, fname)) as f:
            data = json.load(f)

        for t in data.get("tests", []):
            if not t.get("is_flakiness_reproduced"):
                continue
            saved_url = t.get("project_url") or data.get("project_url", "")
            project_url = _get_project_url(project_name, saved_url)
            if not project_url:
                logger.warning("Cannot resolve project_url for %s — skipping", project_name)
                continue
            tests.append({
                "project_name":       project_name,
                "project_url":        project_url,
                "sha_detected":       t.get("sha_detected", ""),
                "test_name":          t.get("test_name", ""),
                "category":           t.get("category", []),
                "flaky_type":         t.get("flaky_type"),
                "root_cause_analysis": t.get("root_cause_analysis", ""),
                "failing_log":        t.get("failing_log"),
                "execution_profiles": t.get("execution_profiles", []),
                "token_usage":        t.get("token_usage", {}),
            })
    return tests


def run_repair_from_detection(args: argparse.Namespace) -> None:
    """Run the repair agent against pre-computed detection results."""
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

    session_dir = args.from_detection
    if not os.path.isdir(session_dir):
        # Allow bare session IDs like "2026-05-05_09-52-56"
        session_dir = os.path.join(RESULTS_DIR, session_dir)
    if not os.path.isdir(session_dir):
        logger.error("Session directory not found: %s", session_dir)
        return

    tests = _load_detection_results(session_dir, project_filter=args.project)
    if not tests:
        logger.error("No reproduced tests found in %s", session_dir)
        return

    logger.info("Running repair on %d reproduced test(s) from %s", len(tests), session_dir)

    # Reuse the existing session directory so repair/ lands next to detection/
    session_id  = os.path.basename(session_dir.rstrip("/"))
    result_logger = ResultLogger(session_id=session_id)

    for i, t in enumerate(tests, 1):
        logger.info("[%d/%d] %s :: %s", i, len(tests), t["project_name"], t["test_name"])

        state = {
            "session_id":             result_logger.session_id,
            "detection_model":        args.detection_model,
            "repair_model":           args.repair_model,
            "dataset":                "IDoFT",
            "language":               "python",
            "build_system":           "pytest",
            "project_url":            t["project_url"],
            "sha_detected":           t["sha_detected"],
            "module_path":            ".",
            "test_name":              t["test_name"],
            "category":               t["category"],
            "passing_log":            None,
            "failing_log":            t.get("failing_log"),
            "is_flakiness_reproduced": True,
            "error_message":          None,
            "pass_count":             0,
            "fail_count":             0,
            "outcome_profile":        None,
            "execution_profiles":     t.get("execution_profiles", []),
            "flaky_type":             t.get("flaky_type"),
            "root_cause_analysis":    t.get("root_cause_analysis", ""),
            "agent_trace":            [],
            "token_usage":            {},
            "pipeline_trace":         [],
            "patch":                  None,
            "patch_target":           None,
            "files_modified":         [],
            "fix_summary":            None,
            "is_fixed":               False,
            "fix_attempts":           0,
            "repair_error":           None,
            "trajectory":             [],
        }

        invoke_config = {
            "configurable": {
                "repair_model": args.repair_model,
            }
        }

        try:
            final_state = repair_agent_node(state, config=invoke_config)
            # Merge repair output back so log_repair_result can read all fields
            state.update(final_state)
        except Exception as e:
            logger.error("Repair crashed for %s: %s", t["test_name"], e)
            state["repair_error"] = f"Pipeline crash: {e}"

        result_logger.log_repair_result(t["project_name"], state)
        logger.info(
            "→ fixed=%s | target=%s | files=%s",
            state.get("is_fixed"),
            state.get("patch_target"),
            state.get("files_modified"),
        )

    result_logger.write_summary()
    reset_docker_environment()
    logger.info("Done. Repair results saved to: %s/repair/", result_logger.session_dir)


def _is_fatal_api_error(exc: Exception) -> bool:
    """Return True for errors that indicate we should stop (out of funds, hard auth failure)."""
    msg = str(exc).lower()
    fatal_signals = [
        "insufficient_quota", "out of funds", "you have run out",
        "payment required", "exceeded your current quota",
        "billing", "402", "account balance",
    ]
    return any(s in msg for s in fatal_signals)


def _build_initial_state(test_info: dict, result_logger: ResultLogger,
                         detection_model: str, repair_model: str) -> dict:
    return {
        "session_id":             result_logger.session_id,
        "detection_model":        detection_model,
        "repair_model":           repair_model,
        "dataset":                "IDoFT",
        "language":               "python",
        "build_system":           "pytest",
        "project_url":            test_info["project_url"],
        "sha_detected":           test_info["sha_detected"],
        "module_path":            ".",
        "test_name":              test_info["test_name"],
        "category":               [test_info["category"]],
        "passing_log":            None,
        "failing_log":            None,
        "is_flakiness_reproduced": False,
        "error_message":          None,
        "pass_count":             0,
        "fail_count":             0,
        "outcome_profile":        None,
        "execution_profiles":     [],
        "flaky_type":             None,
        "root_cause_analysis":    None,
        "agent_trace":            [],
        "token_usage":            {},
        "pipeline_trace":         [],
        "patch":                  None,
        "patch_target":           None,
        "files_modified":         [],
        "fix_summary":            None,
        "is_fixed":               False,
        "fix_attempts":           0,
        "repair_error":           None,
        "trajectory":             [],
    }


def _run_project_tests(
    project_name: str,
    project_tests: list[dict],
    configurable: dict,
    result_logger: ResultLogger,
    stop_event: threading.Event,
    total_tests: int,
    counter: list,          # [current_index] shared counter, protected by counter_lock
    counter_lock: threading.Lock,
) -> None:
    """Run all tests for one project sequentially. Called from worker threads."""
    app = build_graph()  # each thread gets its own compiled graph (avoids shared mutable state)

    for test_info in project_tests:
        if stop_event.is_set():
            logger.info("Stop signal received — skipping remaining tests for %s", project_name)
            break

        with counter_lock:
            counter[0] += 1
            idx = counter[0]

        logger.info("[%d/%d] %s :: %s", idx, total_tests, project_name, test_info["test_name"])

        initial_state = _build_initial_state(
            test_info, result_logger,
            configurable["detection_model"], configurable["repair_model"],
        )

        test_invoke_config = {
            "configurable": configurable,
            "run_name": test_info["test_name"],
            "metadata": {
                "session_id": f"{project_name}_{result_logger.session_id}",
            },
        }

        try:
            final_state = app.invoke(initial_state, config=test_invoke_config)
        except Exception as e:
            if _is_fatal_api_error(e):
                logger.error(
                    "Fatal API error (out of funds / quota exceeded) on %s — stopping all workers.\n%s",
                    test_info["test_name"], e,
                )
                stop_event.set()
                initial_state["error_message"] = f"Fatal API error: {e}"
                result_logger.log_result(project_name, initial_state)
                break
            logger.error("Pipeline crashed for %s: %s", test_info["test_name"], e)
            initial_state["error_message"] = f"Pipeline crash: {e}"
            final_state = initial_state

        result_logger.log_result(project_name, final_state)
        logger.info(
            "→ reproduced=%s | type=%s",
            final_state.get("is_flakiness_reproduced"),
            final_state.get("flaky_type"),
        )


def run_detection_pipeline(args: argparse.Namespace | None = None):
    if args is None:
        args = parse_args()
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

    available = get_available_workspaces()

    if not available:
        logger.error("No workspaces found in %s. Clone some repos first.", WORKSPACE_DIR)
        return

    tests = load_dataset(
        available,
        data_file=args.input_csv,
        project_filter=args.project,
        test_filter=args.test,
        category_filter=args.category,
        limit=args.limit,
        reproduced_only=not args.include_not_reproduced,
        shuffle=args.shuffle,
    )

    if not tests:
        logger.error(
            "No matching tests found. Filters: --project=%s --test=%s --category=%s",
            args.project, args.test, args.category,
        )
        return

    workers = getattr(args, "workers", 1)
    logger.info("Running %d test(s) across %d worker(s).", len(tests), workers)

    result_logger = ResultLogger()

    # Group tests by project so each worker owns one project's Docker container
    by_project: dict[str, list[dict]] = defaultdict(list)
    for t in tests:
        by_project[t["project_name"]].append(t)

    configurable = {
        "detection_model": args.detection_model,
        "repair_model":    args.repair_model,
        "skip_repair":     args.no_repair,
    }

    stop_event = threading.Event()
    counter = [0]
    counter_lock = threading.Lock()

    if workers == 1:
        # Single-threaded fast path — no executor overhead
        for project_name, project_tests in by_project.items():
            if stop_event.is_set():
                break
            _run_project_tests(
                project_name, project_tests, configurable,
                result_logger, stop_event, len(tests), counter, counter_lock,
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _run_project_tests,
                    project_name, project_tests, configurable,
                    result_logger, stop_event, len(tests), counter, counter_lock,
                ): project_name
                for project_name, project_tests in by_project.items()
            }
            for future in as_completed(futures):
                project_name = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error("Worker for %s raised unexpected error: %s", project_name, e)

    if stop_event.is_set():
        logger.warning(
            "Run stopped early due to fatal API error. "
            "Results saved so far — re-run to resume (already-recorded tests are skipped automatically)."
        )

    result_logger.write_summary()
    reset_docker_environment()
    logger.info("Done. Results saved to: %s", result_logger.session_dir)


if __name__ == "__main__":
    _args = parse_args()
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    if _args.from_detection:
        run_repair_from_detection(_args)
    else:
        run_detection_pipeline(_args)
