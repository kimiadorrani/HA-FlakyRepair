"""
Result Logger — saves detection results to timestamped session directories.

Structure:
  results/
    2026-03-24_11-52-15/
      bottle-neck.json         per-project test results
      summary.json             session-wide aggregates + accuracy
      traces/
        bottle-neck.jsonl      one JSON line per test trace (all agent spans)
"""

import os
import json
import logging
from datetime import datetime
from typing import Any

from src.evaluation.detection import DetectionEvaluator
from src.tracing.writer import TraceWriter
from src.tracing.agent_trace import PipelineTrace

logger = logging.getLogger(__name__)

_BASE_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
RESULTS_DIR = os.path.join(_BASE_DIR, "results")


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
            "session_id":  session_id,
            "started_at":  datetime.now().isoformat(),
            "total_tests": 0,
            "reproduced":  0,
            "not_reproduced": 0,
            "errors":      0,
            "outcome_profiles": {
                "mixed": 0, "always_pass": 0, "always_fail": 0,
                "execution_error": 0, "unknown": 0,
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

    # ── per-test result ────────────────────────────────────────────────────

    def log_result(self, project_name: str, state: dict) -> None:
        """Append one test result to the project JSON file and update aggregates."""
        os.makedirs(self.session_dir, exist_ok=True)
        filepath = os.path.join(self.session_dir, f"{project_name}.json")

        if os.path.exists(filepath):
            with open(filepath) as f:
                data = json.load(f)
        else:
            data = {"project": project_name, "tests": []}

        test_result = {
            "test_name":              state.get("test_name"),
            "category":               state.get("category"),
            "sha_detected":           state.get("sha_detected"),
            "is_flakiness_reproduced": state.get("is_flakiness_reproduced", False),
            "pass_count":             state.get("pass_count", 0),
            "fail_count":             state.get("fail_count", 0),
            "outcome_profile":        state.get("outcome_profile"),
            "flaky_type":             state.get("flaky_type"),
            "root_cause_analysis":    state.get("root_cause_analysis"),
            "error_message":          state.get("error_message"),
            "trajectory":             state.get("trajectory", []),
            "token_usage":            state.get("token_usage", {}),
        }
        data["tests"].append(test_result)

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

        logger.info(
            "Saved result for %s::%s → %s",
            project_name, state.get("test_name"), filepath,
        )

        # ── write trace ────────────────────────────────────────────────────
        pipeline_trace_spans = state.get("pipeline_trace") or []
        if pipeline_trace_spans:
            trace = PipelineTrace(
                session_id=self.session_id,
                test_name=state.get("test_name") or "",
                project=project_name,
            )
            # Spans are stored as dicts in state; re-wrap so TraceWriter gets
            # a proper PipelineTrace with the right session_id.
            # We overwrite trace.spans with a dummy list carrying the dict payload
            # by writing the trace dict directly via the writer.
            import json as _json
            trace_dict = {
                "session_id": self.session_id,
                "test_name":  state.get("test_name") or "",
                "project":    project_name,
                "spans":      pipeline_trace_spans,
            }
            traces_dir = os.path.join(self.session_dir, "traces")
            os.makedirs(traces_dir, exist_ok=True)
            path = os.path.join(traces_dir, f"{project_name}.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(trace_dict, default=str) + "\n")

        # ── update summary counters ────────────────────────────────────────
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
        if usage.get("model"):
            self._summary["token_usage"]["model"] = usage["model"]
        self._summary["token_usage"]["input_tokens"]  += int(usage.get("input_tokens",  0))
        self._summary["token_usage"]["output_tokens"] += int(usage.get("output_tokens", 0))
        self._summary["token_usage"]["total_tokens"]  += int(usage.get("total_tokens",  0))
        self._summary["token_usage"]["llm_calls"]     += int(usage.get("llm_calls",     0))

        # ── per-project counters ───────────────────────────────────────────
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

        # ── detection evaluation record ────────────────────────────────────
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

        # Print a concise accuracy line to the console
        det = self._summary["evaluation"]["detection"]
        if det:
            logger.info(
                "Detection accuracy: %.1f%%  (%d/%d correct)  "
                "reproduction rate: %.1f%%",
                det["overall_accuracy"] * 100,
                det["correct"], det["total"],
                det["reproduction_rate"] * 100,
            )
