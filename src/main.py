"""
HA-FlakyRepair — Main Entry Point.

Reads the IDoFT dataset (py-data.csv), filters for tests whose repos
exist in the workspaces/ directory, and runs each through the LangGraph
detection pipeline. Results are saved per-project in timestamped folders.

Usage examples:
  # Run everything available in workspaces/
  python -m src.main

  # Run only tests from a specific project
  python -m src.main --project bottle-neck

  # Run a single specific test by name (partial match)
  python -m src.main --test test_router_register_handler_fn_pass

  # Run only NIO tests
  python -m src.main --category NIO

  # Run only the first N tests (useful for quick debugging)
  python -m src.main --limit 3

  # Combine filters (e.g., first 2 NIO tests from bottle-neck)
  python -m src.main --project bottle-neck --category NIO --limit 2

Adding more workspaces:
  git clone <repo_url> workspaces/<repo_name>
  python -m src.main   # it will be picked up automatically
"""

import os
import csv
import logging
import sys
import argparse

from dotenv import load_dotenv

from src.orchestrator import build_graph
from src.tools.result_logger import ResultLogger

# ── Setup Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Paths ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(BASE_DIR, "..")
DATA_FILE = os.path.join(BASE_DIR, "data", "py-data.csv")
WORKSPACE_DIR = os.path.join(PROJECT_ROOT, "workspaces")


def get_available_workspaces() -> set[str]:
    """Return the set of project names cloned in workspaces/."""
    if not os.path.isdir(WORKSPACE_DIR):
        return set()
    return {
        name for name in os.listdir(WORKSPACE_DIR)
        if os.path.isdir(os.path.join(WORKSPACE_DIR, name))
        and not name.startswith(".")
    }


def load_dataset(
    available_projects: set[str],
    data_file: str = DATA_FILE,
    project_filter: str | None = None,
    skip_projects: set[str] | None = None,
    test_filter: str | None = None,
    category_filter: str | None = None,
    limit: int | None = None,
    include_od: bool = False,
    include_not_reproduced: bool = False,
) -> list[dict]:
    """
    Read py-data.csv and return rows filtered by:
    - Repo must exist in workspaces/
    - Optional: --project (exact project name)
    - Optional: excluded project names
    - Optional: --test   (partial match on test name)
    - Optional: --category (exact category, e.g. NIO, NOD, OD)
    - Optional: --limit  (max number of tests to return)
    - By default, OD/OD-Vic/OD-Brit tests are SKIPPED (use --include-od to enable)
    - If the CSV has preprocessing columns, non-reproduced rows are skipped by default
      (use --include-not-reproduced to include them)

    Why skip OD by default:
      Reproducing OD flakiness requires running the full test suite in random order,
      which only works *because* we already know the test is OD — i.e., we'd be
      telling the model the answer. NIO and NOD can be reproduced category-agnostically.
    """
    tests = []
    normalized_skip_projects = {
        project.lower() for project in (skip_projects or set())
    }
    with open(data_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get("Project URL", "").strip()
            if not url:
                continue

            project_name = url.rstrip("/").split("/")[-1]

            # Must exist in workspaces/
            if project_name not in available_projects:
                continue
            if project_name.lower() in normalized_skip_projects:
                continue

            test_name = row.get(
                "Pytest Test Name "
                "(PathToFile::TestClass::TestMethod or "
                "PathToFile::TestMethod)", ""
            ).strip()
            category = row.get("Category", "").strip()

            if not test_name or not category:
                continue

            reproduced_value = row.get("Reproduced", "").strip().lower()
            if (
                not include_not_reproduced
                and reproduced_value
                and reproduced_value != "true"
            ):
                continue

            # ── Apply optional filters ──
            if project_filter and project_name.lower() != project_filter.lower():
                continue
            if test_filter and test_filter.lower() not in test_name.lower():
                continue
            if category_filter and category.upper() != category_filter.upper():
                continue

            # Skip OD tests by default (see docstring for why)
            if not include_od and category.upper().startswith("OD"):
                continue

            tests.append({
                "project_url": url,
                "project_name": project_name,
                "sha_detected": row.get("SHA Detected", "").strip(),
                "test_name": test_name,
                "category": category,
                "selected_profile": row.get("Selected Profile", "").strip() or None,
                "cpu_limit": row.get("CPU Limit", "").strip() or None,
                "memory_limit": row.get("Memory Limit", "").strip() or None,
            })

            if limit and len(tests) >= limit:
                break

    return tests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HA-FlakyRepair — Flaky Test Detection Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--project", "-p",
        metavar="NAME",
        help="Only run tests from this project (e.g. bottle-neck)",
    )
    parser.add_argument(
        "--test", "-t",
        metavar="PATTERN",
        help="Only run tests whose name contains this string (partial match)",
    )
    parser.add_argument(
        "--category", "-c",
        metavar="CATEGORY",
        help="Only run tests with this category (NIO, NOD, OD, OD-Vic, OD-Brit)",
    )
    parser.add_argument(
        "--include-od",
        action="store_true",
        default=False,
        help="Include OD/OD-Vic/OD-Brit tests (skipped by default — see README)",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        metavar="N",
        help="Stop after N tests (useful for quick debugging)",
    )
    parser.add_argument(
        "--input-csv",
        default=DATA_FILE,
        help="Path to the input dataset CSV (default: src/data/py-data.csv)",
    )
    parser.add_argument(
        "--include-not-reproduced",
        action="store_true",
        default=False,
        help="Include rows marked as not reproduced in a preprocessed CSV",
    )
    return parser.parse_args()


def run_detection_pipeline():
    """Main entry: loads CSV with filters, runs the LangGraph pipeline."""

    args = parse_args()

    # Load environment (GROQ_API_KEY, etc.)
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

    # Discover available workspaces
    available = get_available_workspaces()
    logger.info("Available workspaces (%d): %s", len(available), sorted(available))

    if not available:
        logger.error("No workspaces found in %s. Clone some repos first!", WORKSPACE_DIR)
        return

    # Load and filter the dataset
    tests = load_dataset(
        available,
        data_file=args.input_csv,
        project_filter=args.project,
        test_filter=args.test,
        category_filter=args.category,
        limit=args.limit,
        include_od=args.include_od,
        include_not_reproduced=args.include_not_reproduced,
    )

    if not tests:
        logger.error(
            "No matching tests found. Check your filters: "
            "--project=%s --test=%s --category=%s",
            args.project, args.test, args.category,
        )
        return

    logger.info("Running %d test(s):", len(tests))
    for t in tests:
        logger.info("  [%s] %s | %s", t["category"], t["project_name"], t["test_name"])

    # Build the LangGraph workflow
    app = build_graph()

    # Initialize the result logger
    result_logger = ResultLogger()

    # ── Process each test ──
    for i, test_info in enumerate(tests, 1):
        logger.info(
            "\n" + "=" * 60 +
            "\n🚀 [%d/%d] %s :: %s  (category: %s)" +
            "\n" + "=" * 60,
            i, len(tests),
            test_info["project_name"],
            test_info["test_name"],
            test_info["category"],
        )

        initial_state = {
            "dataset": "IDoFT",
            "language": "python",
            "build_system": "pytest",
            "project_url": test_info["project_url"],
            "sha_detected": test_info["sha_detected"],
            "module_path": ".",
            "test_name": test_info["test_name"],
            "category": [test_info["category"]],
            "passing_log": None,
            "failing_log": None,
            "is_flakiness_reproduced": False,
            "error_message": None,
            "pass_count": 0,
            "fail_count": 0,
            "iterations_requested": 0,
            "iterations_executed": 0,
            "outcome_profile": None,
            "execution_profiles": [],
            "selected_profile": test_info.get("selected_profile"),
            "cpu_limit": test_info.get("cpu_limit"),
            "memory_limit": test_info.get("memory_limit"),
            "flaky_type": None,
            "root_cause_analysis": None,
            "code_context": None,
            "current_patch": None,
            "validation_result": None,
            "rotation_count": 0,
            "trajectory": [],
        }

        try:
            final_state = app.invoke(initial_state)
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

    # Write the aggregated summary
    result_logger.write_summary()

    logger.info("\n" + "=" * 60)
    logger.info("🏁 Done. Results saved to: %s", result_logger.session_dir)
    logger.info("=" * 60)


if __name__ == "__main__":
    run_detection_pipeline()
