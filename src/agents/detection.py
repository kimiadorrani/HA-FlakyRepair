"""
Detection Agent — ReAct agent for adaptive flaky test classification.

The agent receives the test coordinates (repo URL, SHA, test name) and
uses four Docker execution tools to gather evidence. It decides which
strategies to run based on intermediate results, stopping as soon as
it has both a passing and a failing observation.

Model selection: pass {"configurable": {"detection_model": "<key>"}} in
the LangGraph invoke config. Key must exist in models.json or built-ins.
Default: "minimax".

The system prompt uses symptom-based behavioral labels (no dataset
taxonomy names). Post-processing maps those labels to the dataset
categories (NIO, NOD, OD-Vic, OD-Brit).
"""

import json
import logging
import re
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from src.config.models import get_model
from src.state import RepairState
from src.tools.detection_tools import make_detection_tools
from src.tools.docker_infra import cleanup_project_container
from src.tracing.agent_trace import PipelineTrace, TraceCallback
from src.utils.retry import invoke_with_retry

logger = logging.getLogger(__name__)


# Symptom label → dataset taxonomy mapping (no labels leak into the prompt)
_PATTERN_TO_FLAKY_TYPE: dict[str, str] = {
    "STATE_POLLUTION":           "NIO",
    "ISOLATED_UNSTABLE":         "NOD",
    "EXECUTION_ORDER_SENSITIVE": "OD-Vic",
    "BRITTLE_SETUP_DEPENDENT":   "OD-Brit",
    "NOT_FLAKY":                 "Not flaky observed",
    "UNCLEAR":                   "Unknown",
}

_SYSTEM_PROMPT = """You are an expert test engineer investigating whether a Python test exhibits flaky behavior.

You have four tools to run the test inside Docker and observe its behavior:
- run_solo                   : run the test once in isolation — always start here
- run_repeated_in_process(n) : run the test N times sequentially in the same process
- run_isolated_reruns(n)     : run the test N times each in a fresh independent subprocess
- run_suite_randomized(seed) : run the full test directory with a random execution order

Investigation protocol — follow this order strictly:

A. If run_solo outcome is "always_pass":
   1. Call run_repeated_in_process(n=50).
   2. If still "always_pass": call run_isolated_reruns(n=20).
   3. If still "always_pass": call run_suite_randomized(seed=1), seed=2 … up to seed=5.

B. If run_solo outcome is "always_fail":
   1. Call run_suite_randomized(seed=1), seed=2 … up to seed=5.
      — The test may require state set up by other tests; it could pass in certain orderings.

Stop as soon as any result provides BOTH a passing_log AND a failing_log for the target test.
If no contradiction is found after exhausting all steps, classify as NOT_FLAKY.

Classify the observed behavior using ONLY these six patterns:

- STATE_POLLUTION           : run_solo PASSES, but the test fails after being run repeatedly
                              in the same process (dirty shared state accumulates across runs).

- ISOLATED_UNSTABLE         : the test passes in some fresh subprocesses and fails in others
                              with no consistent order dependency (random/environmental cause).

- EXECUTION_ORDER_SENSITIVE : run_solo PASSES, but the test FAILS in certain suite orderings
                              (another test that runs before it leaves polluting state).

- BRITTLE_SETUP_DEPENDENT   : run_solo FAILS, but the test PASSES in certain suite orderings
                              (the test needs state or side-effects set up by earlier tests).

- NOT_FLAKY                 : outcome is completely consistent across every strategy tried.

- UNCLEAR                   : both passing and failing outcomes were observed but the pattern
                              does not match any of the four flaky categories above.

Do not use any external label, dataset name, or category abbreviation.
Base your classification solely on what the tools return.

When your investigation is complete, output ONLY valid JSON with no extra text:
{
  "behavioral_pattern": "<one of the six patterns above>",
  "evidence_summary": "<what specifically differed between the passing and failing runs>",
  "failing_line": "<key error line or assertion, or N/A>",
  "root_cause_analysis": "<concise explanation of why the test behaves this way>"
}"""


def detection_agent_node(state: RepairState, config: RunnableConfig | None = None) -> dict:
    """LangGraph node: runs the ReAct detection agent for one test."""
    test_name    = state.get("test_name", "unknown")
    project_url  = state.get("project_url", "unknown")
    sha          = state.get("sha_detected", "")
    session_id   = state.get("session_id") or "unknown-session"
    project_name = project_url.rstrip("/").split("/")[-1]

    # ── Model selection (configurable per run) ──────────────────────────────
    model_key = (
        (config or {}).get("configurable", {}).get("detection_model")
        or state.get("detection_model")
        or "minimax"
    )
    try:
        model_cfg = get_model(model_key)
    except KeyError as e:
        logger.error("Model config error: %s", e)
        return _error_return(f"Model config error: {e}")

    logger.info("=== Detection Agent: %s  [model=%s] ===", test_name, model_cfg)

    # ── Step 1: set up Docker environment ───────────────────────────────────
    try:
        tools, execution_log = make_detection_tools(project_url, sha, test_name)
    except Exception as e:
        logger.error("Docker setup failed: %s", e)
        return _error_return(f"Docker setup error: {e}", session_id, test_name, project_name)

    # ── Step 2: create the ReAct agent and trace context ────────────────────
    llm   = model_cfg.make_llm()
    agent = create_react_agent(llm, tools)

    trace = PipelineTrace(
        session_id=session_id,
        test_name=test_name,
        project=project_name,
    )

    investigation_prompt = (
        f"Investigate the following test for flaky behavior.\n\n"
        f"Repository: {project_url}\n"
        f"Test: {test_name}\n\n"
        f"Follow the investigation protocol and classify the behavior."
    )

    final_content = ""
    try:
        with trace.start_span("detection", model=model_cfg.name) as span:
            callback = TraceCallback(span)
            try:
                result = invoke_with_retry(
                    agent,
                    {
                        "messages": [
                            SystemMessage(content=_SYSTEM_PROMPT),
                            HumanMessage(content=investigation_prompt),
                        ]
                    },
                    config={
                        "callbacks": [callback],
                        "recursion_limit": 40,
                        "run_name": f"detection/{project_name}/{test_name}",
                        "tags": ["detection", project_name],
                        "metadata": {
                            "session_id": session_id,
                            "project": project_name,
                            "test_name": test_name,
                            "model": model_cfg.name,
                            "commit_sha": sha,
                        },
                    },
                )
                final_content = result["messages"][-1].content
            except Exception as e:
                logger.error("ReAct agent failed: %s", e)
                final_content = json.dumps({
                    "behavioral_pattern": "UNCLEAR",
                    "evidence_summary": "Agent invocation failed.",
                    "failing_line": "N/A",
                    "root_cause_analysis": f"Agent error: {e}",
                })
    finally:
        cleanup_project_container(project_name, sha)

    # ── Step 3: parse classification and aggregate evidence ──────────────────
    span = trace.spans[0]  # single detection span
    token_usage = span.token_usage
    agent_trace = span.as_legacy_agent_trace()

    parsed = _parse_detection_response(final_content)
    flaky_type          = parsed["flaky_type"]
    root_cause_analysis = parsed["root_cause_analysis"]

    passing_log, failing_log, pass_count, fail_count = _aggregate_execution_log(execution_log)

    _flaky_patterns = {"STATE_POLLUTION", "ISOLATED_UNSTABLE", "EXECUTION_ORDER_SENSITIVE", "BRITTLE_SETUP_DEPENDENT"}
    is_reproduced   = parsed.get("behavioral_pattern") in _flaky_patterns
    outcome_profile = _derive_outcome(pass_count, fail_count)

    logger.info(
        "Detection complete. pattern=%s → type=%s | reproduced=%s | "
        "tokens in=%d out=%d total=%d (%d LLM calls) | duration=%.1fs",
        parsed.get("behavioral_pattern"), flaky_type, is_reproduced,
        token_usage["input_tokens"], token_usage["output_tokens"],
        token_usage["total_tokens"], token_usage["llm_calls"],
        span.duration_seconds or 0,
    )

    return {
        "flaky_type":              flaky_type,
        "root_cause_analysis":     root_cause_analysis,
        "passing_log":             passing_log,
        "failing_log":             failing_log,
        "is_flakiness_reproduced": is_reproduced,
        "pass_count":              pass_count,
        "fail_count":              fail_count,
        "outcome_profile":         outcome_profile,
        "execution_profiles":      execution_log,
        "agent_trace":             agent_trace,
        "token_usage":             token_usage,
        "pipeline_trace":          [span.to_dict()],   # appended via operator.add
        "error_message":           None,
        "trajectory": [{
            "agent":              "DetectionAgent",
            "action":             "react_investigation",
            "model":              model_cfg.name,
            "behavioral_pattern": parsed.get("behavioral_pattern"),
            "flaky_type":         flaky_type,
            "reproduced":         is_reproduced,
            "tool_calls":         len(execution_log),
            "pass_count":         pass_count,
            "fail_count":         fail_count,
            "duration_seconds":   span.duration_seconds,
            "token_usage":        token_usage,
        }],
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def _error_return(
    message: str,
    session_id: str = "",
    test_name: str = "",
    project_name: str = "",
) -> dict:
    return {
        "flaky_type":              "Error — could not execute",
        "root_cause_analysis":     message,
        "error_message":           message,
        "is_flakiness_reproduced": False,
        "execution_profiles":      [],
        "pipeline_trace":          [],
        "trajectory": [{
            "agent":  "DetectionAgent",
            "action": "docker_setup_failed",
            "error":  message,
        }],
    }


def _parse_detection_response(raw: str) -> dict[str, str]:
    """Extract and map the JSON classification from the agent's final message."""
    text = (raw or "").strip()
    
    # Strip <think> blocks (often output by models like DeepSeek, Qwen, or MiniMax)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    
    payload: dict | None = None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a markdown block first
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
                
        # Fallback to greedy regex
        if payload is None:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    payload = json.loads(match.group(0))
                except json.JSONDecodeError:
                    payload = None

    if payload is None:
        return {
            "behavioral_pattern": "UNCLEAR",
            "flaky_type":         "Unknown",
            "root_cause_analysis": text or "Agent returned an unparseable response.",
        }

    pattern     = str(payload.get("behavioral_pattern") or "UNCLEAR").strip().upper()
    flaky_type  = _PATTERN_TO_FLAKY_TYPE.get(pattern, "Unknown")
    failing_line = str(payload.get("failing_line") or "N/A").strip()
    explanation  = str(payload.get("root_cause_analysis") or "").strip()
    summary      = str(payload.get("evidence_summary") or "").strip()

    rca_parts = []
    if summary:
        rca_parts.append(f"Evidence: {summary}")
    if failing_line and failing_line != "N/A":
        rca_parts.append(f"Failing line: {failing_line}")
    if explanation:
        rca_parts.append(explanation)
    root_cause_analysis = "\n".join(rca_parts) or text or "No analysis returned."

    return {
        "behavioral_pattern":  pattern,
        "flaky_type":          flaky_type,
        "root_cause_analysis": root_cause_analysis,
    }


def _aggregate_execution_log(
    execution_log: list[dict],
) -> tuple[str | None, str | None, int, int]:
    passing_log = None
    failing_log = None
    pass_count  = 0
    fail_count  = 0
    for profile in execution_log:
        pass_count += int(profile.get("pass_count") or 0)
        fail_count += int(profile.get("fail_count") or 0)
        if not passing_log and profile.get("passing_log"):
            passing_log = profile["passing_log"]
        if not failing_log and profile.get("failing_log"):
            failing_log = profile["failing_log"]
    return passing_log, failing_log, pass_count, fail_count


def _derive_outcome(pass_count: int, fail_count: int) -> str:
    if pass_count > 0 and fail_count > 0:
        return "mixed"
    if pass_count > 0:
        return "always_pass"
    if fail_count > 0:
        return "always_fail"
    return "unknown"
