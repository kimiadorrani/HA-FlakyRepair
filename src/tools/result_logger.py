"""
Result Logger — saves pipeline results to timestamped session directories.

Directory layout:
  results/
    2026-05-05_09-52-56/
      detection/
        plcx.json              detection output per project
      repair/
        plcx.json              repair output per project (only if repair ran)
      traces/
        detection/
          plcx.jsonl           one JSON line per test — detection agent spans
        repair/
          plcx.jsonl           one JSON line per test — repair agent spans
      summary.json             session-wide aggregates
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

from src.evaluation.detection import DetectionEvaluator
from src.tracing.writer import TraceWriter
from src.tracing.agent_trace import PipelineTrace

logger = logging.getLogger(__name__)

_BASE_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
RESULTS_DIR = os.path.join(_BASE_DIR, "results")


def _compact_profiles(profiles: list[dict]) -> list[dict]:
    """Strip full log text from execution profiles — keep only replay metadata."""
    return [
        {
            "name":            p.get("name"),
            "pass_count":      p.get("pass_count", 0),
            "fail_count":      p.get("fail_count", 0),
            "outcome_profile": p.get("outcome_profile"),
        }
        for p in (profiles or [])
    ]


def _append_json(filepath: str, project_name: str, record: dict) -> None:
    """Append a record to a per-project JSON file, creating it if absent."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    if os.path.exists(filepath):
        with open(filepath) as f:
            data = json.load(f)
    else:
        data = {"project": project_name, "tests": []}
    data["tests"].append(record)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _write_trace(traces_dir: str, project_name: str, session_id: str,
                 test_name: str, spans: list[dict]) -> None:
    """Append a span-dict list as one JSONL line."""
    if not spans:
        return
    os.makedirs(traces_dir, exist_ok=True)
    path = os.path.join(traces_dir, f"{project_name}.jsonl")
    entry = {
        "session_id": session_id,
        "test_name":  test_name,
        "project":    project_name,
        "spans":      spans,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


class ResultLogger:
    """Manages per-session result files, traces, and evaluation metrics."""

    def __init__(self, session_id: str | None = None):
        if session_id is None:
            session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_id  = session_id
        self.session_dir = os.path.join(RESULTS_DIR, session_id)
        os.makedirs(self.session_dir, exist_ok=True)

        self._detection_eval = DetectionEvaluator()
        self._trace_writer   = TraceWriter(self.session_dir)

        self._summary: dict[str, Any] = {
            "session_id":     session_id,
            "started_at":     datetime.now().isoformat(),
            "total_tests":    0,
            "reproduced":     0,
            "not_reproduced": 0,
            "errors":         0,
            "outcome_profiles": {
                "mixed": 0, "always_pass": 0, "always_fail": 0,
                "execution_error": 0, "unknown": 0,
            },
            "repair": {
                "attempted": 0,
                "fixed":     0,
                "not_fixed": 0,
                "errored":   0,
            },
            "token_usage": {
                "model":         None,
                "input_tokens":  0,
                "output_tokens": 0,
                "total_tokens":  0,
                "llm_calls":     0,
            },
            "projects": {},
        }
        logger.info("Results will be saved to: %s", self.session_dir)

    # ── detection ─────────────────────────────────────────────────────────

    def log_detection_result(self, project_name: str, state: dict) -> None:
        """Write detection output to detection/<project>.json and update counters."""
        filepath = os.path.join(self.session_dir, "detection", f"{project_name}.json")

        # Compact execution profiles (strip bulky log text)
        profiles = _compact_profiles(state.get("execution_profiles") or [])

        record = {
            "test_name":               state.get("test_name"),
            "category":                state.get("category"),
            "project_url":             state.get("project_url"),
            "sha_detected":            state.get("sha_detected"),
            "is_flakiness_reproduced": state.get("is_flakiness_reproduced", False),
            "pass_count":              state.get("pass_count", 0),
            "fail_count":              state.get("fail_count", 0),
            "outcome_profile":         state.get("outcome_profile"),
            "flaky_type":              state.get("flaky_type"),
            "root_cause_analysis":     state.get("root_cause_analysis"),
            "failing_log":             state.get("failing_log"),
            "error_message":           state.get("error_message"),
            "execution_profiles":      profiles,
            "trajectory":              state.get("trajectory", []),
            "token_usage":             state.get("token_usage", {}),
        }
        _append_json(filepath, project_name, record)
        logger.info("Detection saved: %s :: %s", project_name, state.get("test_name"))

        # traces
        detection_spans = [
            s for s in (state.get("pipeline_trace") or [])
            if s.get("agent") == "detection"
        ]
        _write_trace(
            os.path.join(self.session_dir, "traces", "detection"),
            project_name, self.session_id,
            state.get("test_name") or "", detection_spans,
        )

        # summary counters
        self._summary["total_tests"] += 1
        if state.get("is_flakiness_reproduced"):
            self._summary["reproduced"] += 1
        elif state.get("error_message"):
            self._summary["errors"] += 1
        else:
            self._summary["not_reproduced"] += 1

        outcome_profile = state.get("outcome_profile") or "unknown"
        self._summary["outcome_profiles"].setdefault(outcome_profile, 0)
        self._summary["outcome_profiles"][outcome_profile] += 1

        usage = state.get("token_usage") or {}
        self._update_token_usage(usage)
        self._update_project_counters(project_name, state, usage, outcome_profile)
        self._record_detection_eval(project_name, state)

    # ── repair ────────────────────────────────────────────────────────────

    def log_repair_result(self, project_name: str, state: dict) -> None:
        """Write repair output to repair/<project>.json and update counters."""
        filepath = os.path.join(self.session_dir, "repair", f"{project_name}.json")

        record = {
            "test_name":      state.get("test_name"),
            "sha_detected":   state.get("sha_detected"),
            "flaky_type":     state.get("flaky_type"),
            "is_fixed":       state.get("is_fixed", False),
            "patch_target":   state.get("patch_target"),
            "files_modified": state.get("files_modified", []),
            "fix_summary":    state.get("fix_summary"),
            "fix_attempts":   state.get("fix_attempts", 0),
            "patch":          state.get("patch"),
            "repair_error":   state.get("repair_error"),
            "trajectory":     state.get("trajectory", []),
            "token_usage":    state.get("token_usage", {}),
        }
        _append_json(filepath, project_name, record)
        logger.info(
            "Repair saved: %s :: %s  [fixed=%s]",
            project_name, state.get("test_name"), state.get("is_fixed"),
        )

        # traces
        repair_spans = [
            s for s in (state.get("pipeline_trace") or [])
            if s.get("agent") == "repair"
        ]
        _write_trace(
            os.path.join(self.session_dir, "traces", "repair"),
            project_name, self.session_id,
            state.get("test_name") or "", repair_spans,
        )

        # repair counters
        if state.get("repair_error"):
            self._summary["repair"]["errored"] += 1
        else:
            self._summary["repair"]["attempted"] += 1
            if state.get("is_fixed"):
                self._summary["repair"]["fixed"] += 1
            else:
                self._summary["repair"]["not_fixed"] += 1

        usage = state.get("token_usage") or {}
        self._update_token_usage(usage)

    # ── combined (detection + repair in one graph call) ───────────────────

    def log_result(self, project_name: str, state: dict) -> None:
        """Log both detection and (if present) repair output in one call."""
        self.log_detection_result(project_name, state)
        if state.get("fix_attempts", 0) > 0 or state.get("repair_error"):
            self.log_repair_result(project_name, state)

    # ── session summary ────────────────────────────────────────────────────

    def write_summary(self) -> None:
        """Write summary.json with aggregates, accuracy, and confusion matrix."""
        self._summary["finished_at"] = datetime.now().isoformat()
        self._summary["evaluation"] = {
            "detection": self._detection_eval.report(),
        }
        summary_path = os.path.join(self.session_dir, "summary.json")
        with open(summary_path, "w") as f:
            json.dump(self._summary, f, indent=2)
        logger.info("Summary written to %s", summary_path)

        det = self._summary["evaluation"]["detection"]
        if det:
            logger.info(
                "Detection accuracy: %.1f%%  (%d/%d correct)  "
                "reproduction rate: %.1f%%  |  "
                "Repair: %d/%d fixed",
                det["overall_accuracy"] * 100,
                det["correct"], det["total"],
                det["reproduction_rate"] * 100,
                self._summary["repair"]["fixed"],
                self._summary["repair"]["attempted"],
            )

    # ── private helpers ────────────────────────────────────────────────────

    def _update_token_usage(self, usage: dict) -> None:
        if usage.get("model"):
            self._summary["token_usage"]["model"] = usage["model"]
        self._summary["token_usage"]["input_tokens"]  += int(usage.get("input_tokens",  0))
        self._summary["token_usage"]["output_tokens"] += int(usage.get("output_tokens", 0))
        self._summary["token_usage"]["total_tokens"]  += int(usage.get("total_tokens",  0))
        self._summary["token_usage"]["llm_calls"]     += int(usage.get("llm_calls",     0))

    def _update_project_counters(
        self, project_name: str, state: dict,
        usage: dict, outcome_profile: str,
    ) -> None:
        if project_name not in self._summary["projects"]:
            self._summary["projects"][project_name] = {
                "total": 0, "reproduced": 0,
                "outcome_profiles": {
                    "mixed": 0, "always_pass": 0, "always_fail": 0,
                    "execution_error": 0, "unknown": 0,
                },
                "token_usage": {
                    "input_tokens": 0, "output_tokens": 0,
                    "total_tokens": 0, "llm_calls": 0,
                },
            }
        proj = self._summary["projects"][project_name]
        proj["total"] += 1
        if state.get("is_flakiness_reproduced"):
            proj["reproduced"] += 1
        proj["outcome_profiles"].setdefault(outcome_profile, 0)
        proj["outcome_profiles"][outcome_profile] += 1
        pu = proj["token_usage"]
        pu["input_tokens"]  += int(usage.get("input_tokens",  0))
        pu["output_tokens"] += int(usage.get("output_tokens", 0))
        pu["total_tokens"]  += int(usage.get("total_tokens",  0))
        pu["llm_calls"]     += int(usage.get("llm_calls",     0))

    def _record_detection_eval(self, project_name: str, state: dict) -> None:
        raw_category = state.get("category")
        actual = (
            raw_category[0]
            if isinstance(raw_category, list) and raw_category
            else str(raw_category or "")
        ).strip()
        predicted = str(state.get("flaky_type") or "Unknown").strip()
        self._detection_eval.record(
            project=project_name,
            test_name=str(state.get("test_name") or ""),
            actual_category=actual,
            predicted_type=predicted,
            reproduced=bool(state.get("is_flakiness_reproduced")),
        )
