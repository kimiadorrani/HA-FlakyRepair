"""
HA-FlakyRepair — Main Entry Point.

Reads the merged IDoFT reproduction dataset, filters for tests whose
repos exist in workspaces/idoft/, and runs each through the LangGraph
detection pipeline. Results are saved per-project in timestamped folders.

The merged dataset contains both OD and non-OD tests. The detection agent
handles all categories (NIO, NOD, OD-Vic, OD-Brit) without requiring the
category as input.

Usage examples:
  python -m src.main

  python -m src.main --project bottle-neck

  python -m src.main --project PyGraph cloudnetpy compare-mt coo

  python -m src.main --test test_router_register_handler_fn_pass

  python -m src.main --category NIO

  python -m src.main --limit 3

  python -m src.main --project bottle-neck --category NIO --limit 2

  python -m src.main --input-csv datasets/idoft/idoft-merged-reproduction-results.csv
"""

import os
import csv
import logging
import sys
import argparse
import random

from dotenv import load_dotenv

from src.orchestrator import build_graph
from src.tools.result_logger import ResultLogger
from src.tools.docker_runner import reset_docker_environment

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
        "--detection-model", default="minimax", metavar="MODEL_KEY",
        help=f"Model key for the Detection Agent. Available: {list_models()} (default: minimax)",
    )
    return parser.parse_args()


def run_detection_pipeline():
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

    logger.info("Running %d test(s).", len(tests))

    app = build_graph()
    result_logger = ResultLogger()

    # LangGraph configurable parameters — passed to every node via invoke config
    invoke_config = {
        "configurable": {
            "detection_model": args.detection_model,
        }
    }

    for i, test_info in enumerate(tests, 1):
        logger.info("[%d/%d] %s :: %s", i, len(tests), test_info["project_name"], test_info["test_name"])

        initial_state = {
            "session_id":             result_logger.session_id,
            "detection_model":        args.detection_model,
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
            "code_context":           None,
            "current_patch":          None,
            "validation_result":      None,
            "rotation_count":         0,
            "trajectory":             [],
        }

        # For LangSmith: dynamically create a new project folder per repo per run
        os.environ["LANGCHAIN_PROJECT"] = f"{test_info['project_name']}_{result_logger.session_id}"
        
        # For Langfuse: "Projects" are tied to API keys and can't be created dynamically.
        # Instead, Langfuse groups traces into unlimited "Sessions" based on this metadata.
        test_invoke_config = {
            **invoke_config,
            "run_name": test_info["test_name"],
            "metadata": {
                **invoke_config.get("metadata", {}),
                "session_id": f"{test_info['project_name']}_{result_logger.session_id}",
            }
        }

        # Force the Langchain root trace name to be the test name instead of "LangGraph"
        app.name = test_info["test_name"]

        try:
            final_state = app.invoke(initial_state, config=test_invoke_config)
        except Exception as e:
            logger.error("Pipeline crashed for %s: %s", test_info["test_name"], e)
            initial_state["error_message"] = f"Pipeline crash: {e}"
            final_state = initial_state

        result_logger.log_result(test_info["project_name"], final_state)

        logger.info(
            "→ reproduced=%s | type=%s",
            final_state.get("is_flakiness_reproduced"),
            final_state.get("flaky_type"),
        )

    result_logger.write_summary()
    reset_docker_environment()
    logger.info("Done. Results saved to: %s", result_logger.session_dir)


if __name__ == "__main__":
    run_detection_pipeline()
