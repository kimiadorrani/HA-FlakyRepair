"""
Result Logger — saves detection results to timestamped directories.

Structure:
  results/
    2026-03-24_11-52-15/
      bottle-neck.json
      devtracker.json
      summary.json
"""

import os
import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Project base directory
_BASE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".."
)
RESULTS_DIR = os.path.join(_BASE_DIR, "results")


class ResultLogger:
    """Manages per-session, per-project result files."""

    def __init__(self, session_id: str | None = None):
        if session_id is None:
            session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_id = session_id
        self.session_dir = os.path.join(RESULTS_DIR, session_id)
        os.makedirs(self.session_dir, exist_ok=True)
        logger.info("Results will be saved to: %s", self.session_dir)

        # In-memory aggregation for summary
        self._summary: dict[str, Any] = {
            "session_id": session_id,
            "started_at": datetime.now().isoformat(),
            "total_tests": 0,
            "reproduced": 0,
            "not_reproduced": 0,
            "errors": 0,
            "outcome_profiles": {
                "mixed": 0,
                "always_pass": 0,
                "always_fail": 0,
                "execution_error": 0,
                "unknown": 0,
            },
            "projects": {}
        }

    def _project_file(self, project_name: str) -> str:
        return os.path.join(self.session_dir, f"{project_name}.json")

    def log_result(self, project_name: str, state: dict) -> None:
        """Append a test result to the project's JSON file."""
        filepath = self._project_file(project_name)

        # Load existing results if the file already exists
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                data = json.load(f)
        else:
            data = {"project": project_name, "tests": []}

        # Extract the relevant fields from the state
        test_result = {
            "test_name": state.get("test_name"),
            "category": state.get("category"),
            "sha_detected": state.get("sha_detected"),
            "is_flakiness_reproduced": state.get("is_flakiness_reproduced", False),
            "pass_count": state.get("pass_count", 0),
            "fail_count": state.get("fail_count", 0),
            "outcome_profile": state.get("outcome_profile"),
            "execution_profiles": state.get("execution_profiles", []),
            "flaky_type": state.get("flaky_type"),
            "root_cause_analysis": state.get("root_cause_analysis"),
            "error_message": state.get("error_message"),
            "passing_log": state.get("passing_log"),
            "failing_log": state.get("failing_log"),
            "trajectory": state.get("trajectory", []),
        }

        data["tests"].append(test_result)

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

        logger.info(
            "Saved result for %s::%s → %s",
            project_name, state.get("test_name"), filepath
        )

        # Update summary
        self._summary["total_tests"] += 1
        if state.get("is_flakiness_reproduced"):
            self._summary["reproduced"] += 1
        elif state.get("error_message"):
            self._summary["errors"] += 1
        else:
            self._summary["not_reproduced"] += 1

        outcome_profile = state.get("outcome_profile") or "unknown"
        if outcome_profile not in self._summary["outcome_profiles"]:
            self._summary["outcome_profiles"][outcome_profile] = 0
        self._summary["outcome_profiles"][outcome_profile] += 1

        if project_name not in self._summary["projects"]:
            self._summary["projects"][project_name] = {
                "total": 0,
                "reproduced": 0,
                "outcome_profiles": {
                    "mixed": 0,
                    "always_pass": 0,
                    "always_fail": 0,
                    "execution_error": 0,
                    "unknown": 0,
                },
            }
        self._summary["projects"][project_name]["total"] += 1
        if state.get("is_flakiness_reproduced"):
            self._summary["projects"][project_name]["reproduced"] += 1
        if outcome_profile not in self._summary["projects"][project_name]["outcome_profiles"]:
            self._summary["projects"][project_name]["outcome_profiles"][outcome_profile] = 0
        self._summary["projects"][project_name]["outcome_profiles"][outcome_profile] += 1

    def write_summary(self) -> None:
        """Write the aggregated summary file."""
        self._summary["finished_at"] = datetime.now().isoformat()
        summary_path = os.path.join(self.session_dir, "summary.json")
        with open(summary_path, "w") as f:
            json.dump(self._summary, f, indent=2)
        logger.info("Summary written to %s", summary_path)
