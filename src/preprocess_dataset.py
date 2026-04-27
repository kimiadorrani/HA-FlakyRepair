"""
Preprocess the raw Python flaky-test dataset into a reproducible-only CSV.

Workflow:
1. Fetch missing non-OD repositories into `workspaces/`
2. Run the reproducibility pipeline on the source CSV
3. Export only reproduced rows into a clean CSV

Example:
  .venv/bin/python -m src.preprocess_dataset
  .venv/bin/python -m src.main --input-csv src/data/preprocessed/py-data-reproducible.csv
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.data.preprocess.export_reproducible_csv import (
    append_processed_row,
    existing_output_keys,
    processed_row_data_from_state,
    reproduced_rows_from_session,
)
from src.data.preprocess.fetch_workspaces import clone_missing_repos, iter_unique_repos
from src.main import DATA_FILE, PROJECT_ROOT, get_available_workspaces, load_dataset
from src.orchestrator import build_graph
from src.tools.docker_runner import reset_docker_environment
from src.tools.result_logger import ResultLogger


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


PREPROCESSED_DIR = Path(PROJECT_ROOT) / "src" / "data" / "preprocessed"
PREPROCESS_REPORT = PREPROCESSED_DIR / "preprocess-report.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch missing repos, run reproducibility preprocessing, and export a clean CSV."
    )
    parser.add_argument(
        "--input-csv",
        default=DATA_FILE,
        help="Source dataset CSV path",
    )
    parser.add_argument(
        "--output-csv",
        default=str(PREPROCESSED_DIR / "py-data-reproducible.csv"),
        help="Destination path for the clean reproducible CSV",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional cap on number of dataset rows to process",
    )
    parser.add_argument(
        "--project",
        help="Optional project filter while preprocessing",
    )
    parser.add_argument(
        "--test",
        help="Optional partial test-name filter while preprocessing",
    )
    return parser.parse_args()


def preprocess_dataset() -> None:
    args = parse_args()

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

    input_csv = Path(args.input_csv)
    output_csv = Path(args.output_csv)
    PREPROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    repos = iter_unique_repos(input_csv, include_od=False)
    existing_before = get_available_workspaces()
    missing = [(name, url) for name, url in repos if name not in existing_before]

    logger.info("Non-OD repos in source CSV: %d", len(repos))
    logger.info("Already present in workspaces/: %d", len(repos) - len(missing))
    logger.info("Missing repos to clone: %d", len(missing))

    cloned, failed = clone_missing_repos(missing)
    logger.info("Clone pass finished. Cloned=%d Failed=%d", cloned, failed)

    available = get_available_workspaces()
    tests = load_dataset(
        available_projects=available,
        data_file=str(input_csv),
        project_filter=args.project,
        test_filter=args.test,
        category_filter=None,
        limit=None,
        include_od=False,
    )
    already_exported = existing_output_keys(output_csv)
    pending_tests = [
        test for test in tests
        if (
            test["project_name"],
            test["sha_detected"],
            test["test_name"],
            test["category"],
        ) not in already_exported
    ]
    if args.limit is not None:
        pending_tests = pending_tests[:args.limit]

    logger.info("Already recorded rows in clean CSV: %d", len(already_exported))
    logger.info("Running reproducibility preprocessing on %d pending test(s)", len(pending_tests))
    app = build_graph()
    result_logger = ResultLogger()
    appended_rows = 0
    appended_reproduced_rows = 0
    current_project_batch: tuple[str, str] | None = None
    repo_summary: dict[str, dict[str, object]] = {}

    for i, test_info in enumerate(pending_tests, 1):
        project_batch = (test_info["project_name"], test_info["sha_detected"])
        if project_batch != current_project_batch:
            if current_project_batch is not None:
                logger.info(
                    "Finished batch for %s @ %s. Resetting Docker before the next project.",
                    current_project_batch[0],
                    current_project_batch[1],
                )
            else:
                logger.info("Preparing Docker for the first project batch.")
            reset_docker_environment()
            current_project_batch = project_batch

        logger.info(
            "[%d/%d] %s :: %s (%s)",
            i,
            len(pending_tests),
            test_info["project_name"],
            test_info["test_name"],
            test_info["category"],
        )

        project_summary = repo_summary.setdefault(
            test_info["project_name"],
            {
                "tests_tried": 0,
                "tests_reproduced": 0,
                "categories_tried": Counter(),
                "categories_reproduced": Counter(),
            },
        )
        project_summary["tests_tried"] += 1
        project_summary["categories_tried"][test_info["category"]] += 1

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
        except Exception as exc:
            logger.error("Pipeline crashed for %s: %s", test_info["test_name"], exc)
            initial_state["error_message"] = f"Pipeline crash: {exc}"
            final_state = initial_state

        result_logger.log_result(test_info["project_name"], final_state)
        row_key = (
            test_info["project_name"],
            test_info["sha_detected"],
            test_info["test_name"],
            test_info["category"],
        )
        if row_key not in already_exported:
            row_data = processed_row_data_from_state(final_state)
            if append_processed_row(input_csv, output_csv, row_key, row_data):
                already_exported.add(row_key)
                appended_rows += 1
                if final_state.get("is_flakiness_reproduced"):
                    appended_reproduced_rows += 1
                logger.info(
                    "Recorded preprocessing outcome in %s for %s (%s)",
                    output_csv,
                    test_info["test_name"],
                    row_data.get("Preprocess Status", "unknown"),
                )

        if final_state.get("is_flakiness_reproduced"):
            project_summary["tests_reproduced"] += 1
            project_summary["categories_reproduced"][test_info["category"]] += 1

    if current_project_batch is not None:
        logger.info(
            "Finished batch for %s @ %s. Resetting Docker after preprocessing.",
            current_project_batch[0],
            current_project_batch[1],
        )
        reset_docker_environment()

    result_logger.write_summary()
    recorded_rows = len(existing_output_keys(output_csv))
    reproduced_rows = reproduced_rows_from_session(Path(result_logger.session_dir))
    total_reproduced_seen = sum(1 for row in reproduced_rows.values())

    report = {
        "session_id": result_logger.session_id,
        "input_csv": str(input_csv),
        "output_csv": str(output_csv),
        "total_candidate_tests": len(tests),
        "pending_tests_processed": len(pending_tests),
        "non_od_repos_in_source": len(repos),
        "already_present_repos": len(repos) - len(missing),
        "missing_repos_before_clone": len(missing),
        "cloned_repos": cloned,
        "failed_repo_clones": failed,
        "already_exported_rows_before_run": recorded_rows - appended_rows,
        "rows_appended_this_run": appended_rows,
        "reproduced_rows_appended_this_run": appended_reproduced_rows,
        "recorded_rows_total": recorded_rows,
        "reproduced_rows_seen_in_session": total_reproduced_seen,
        "repo_summary": {
            project_name: {
                "tests_tried": summary["tests_tried"],
                "tests_reproduced": summary["tests_reproduced"],
                "categories_tried": dict(summary["categories_tried"]),
                "categories_reproduced": dict(summary["categories_reproduced"]),
            }
            for project_name, summary in sorted(repo_summary.items())
        },
    }
    with PREPROCESS_REPORT.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    logger.info("Preprocessing session: %s", result_logger.session_id)
    logger.info("Recorded %d total row(s) in %s", recorded_rows, output_csv)
    logger.info("Wrote preprocessing report to %s", PREPROCESS_REPORT)


if __name__ == "__main__":
    preprocess_dataset()
