"""
Repair Agent — ReAct agent for fixing flaky tests.

Receives detection output (root cause, execution profiles, failing log) and
uses file I/O tools + a verification tool to apply and validate a minimal fix.

All changes are made inside the Docker container — the host workspace is
never modified. The final unified diff is stored in state as `patch`.
"""

import json
import logging
import re

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from src.config.models import get_model
from src.state import RepairState
from src.tools.docker_runner import make_repair_tools, cleanup_project_container
from src.tracing.agent_trace import PipelineTrace, TraceCallback

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an expert software engineer tasked with fixing a flaky test.

You have five tools:
- list_files(directory)          : list Python files in a directory of the repo
- read_file(file_path)           : read a file with line numbers (path relative to repo root)
- write_file(file_path, content) : overwrite a file (provide COMPLETE file content)
- run_verification()             : re-run the exact strategy that reproduced the flakiness
- get_diff()                     : show all changes made so far as a unified diff

Fix protocol:
1. Read the root cause analysis carefully.
2. Use list_files and read_file to understand the relevant code.
3. Apply a minimal fix using write_file.
4. Call run_verification() — if fail_count is 0 the fix is verified; stop here.
5. If still flaky, revise the fix and verify again (up to 3 total attempts).
6. When done (verified or attempts exhausted), call get_diff() and produce output.

Fix principles:
- Apply the smallest change that eliminates the root cause.
- No sleeps, no retries, no broad try/except to mask failures.
- When the bug is in source code (shared mutable state, missing teardown), fix the source.
- When the test is poorly isolated (missing fixture reset, leaking side-effects), fix the test.
- Never change test assertions, expected values, or test logic.
- write_file requires the COMPLETE updated file content, not a patch snippet.

When done, output ONLY valid JSON with no extra text:
{
  "patch_target": "source" | "test" | "both",
  "files_modified": ["relative/path/to/file.py"],
  "fix_summary": "<one sentence: what was changed and why>",
  "is_fixed": true | false,
  "diff": "<full output of get_diff()>"
}"""


def repair_agent_node(state: RepairState, config: RunnableConfig | None = None) -> dict:
    """LangGraph node: runs the ReAct repair agent for one test."""
    test_name          = state.get("test_name", "unknown")
    project_url        = state.get("project_url", "unknown")
    sha                = state.get("sha_detected", "")
    session_id         = state.get("session_id") or "unknown-session"
    project_name       = project_url.rstrip("/").split("/")[-1]
    root_cause         = state.get("root_cause_analysis") or ""
    failing_log        = state.get("failing_log") or ""
    execution_profiles = state.get("execution_profiles") or []

    model_key = (
        (config or {}).get("configurable", {}).get("repair_model")
        or state.get("repair_model")
        or "minimax"
    )
    try:
        model_cfg = get_model(model_key)
    except KeyError as e:
        return _error_return(f"Model config error: {e}")

    logger.info("=== Repair Agent: %s  [model=%s] ===", test_name, model_cfg)

    try:
        tools, repair_log = make_repair_tools(
            project_url, sha, test_name, execution_profiles
        )
    except Exception as e:
        logger.error("Docker setup failed for repair: %s", e)
        return _error_return(f"Docker setup error: {e}")

    llm   = model_cfg.make_llm()
    agent = create_react_agent(llm, tools)

    trace = PipelineTrace(
        session_id=session_id,
        test_name=test_name,
        project=project_name,
    )

    repair_prompt = (
        f"Fix the following flaky test.\n\n"
        f"Repository: {project_url}\n"
        f"Test: {test_name}\n\n"
        f"Root cause analysis:\n{root_cause}\n\n"
        f"Failing log (excerpt):\n{failing_log[:2000] if failing_log else 'N/A'}\n\n"
        f"Apply a minimal fix, verify it eliminates the flakiness, then output your result as JSON."
    )

    final_content = ""
    try:
        with trace.start_span("repair", model=model_cfg.name) as span:
            callback = TraceCallback(span)
            try:
                result = agent.invoke(
                    {
                        "messages": [
                            SystemMessage(content=_SYSTEM_PROMPT),
                            HumanMessage(content=repair_prompt),
                        ]
                    },
                    config={
                        "callbacks": [callback],
                        "recursion_limit": 60,
                        "run_name": f"repair/{project_name}/{test_name}",
                        "tags": ["repair", project_name],
                        "metadata": {
                            "session_id": session_id,
                            "project":    project_name,
                            "test_name":  test_name,
                            "model":      model_cfg.name,
                        },
                    },
                )
                final_content = result["messages"][-1].content
            except Exception as e:
                logger.error("Repair ReAct agent failed: %s", e)
                final_content = json.dumps({
                    "patch_target":   "unknown",
                    "files_modified": [],
                    "fix_summary":    f"Agent error: {e}",
                    "is_fixed":       False,
                    "diff":           "",
                })
    finally:
        cleanup_project_container(project_name, sha)

    span        = trace.spans[0]
    token_usage = span.token_usage
    parsed      = _parse_repair_response(final_content)

    # Cross-check parsed is_fixed against the actual repair_log:
    # if the last verification run showed 0 failures, the fix worked regardless
    # of what the model said in its JSON.
    is_fixed = parsed.get("is_fixed", False)
    if repair_log:
        last = repair_log[-1]
        if last.get("fail_count", 0) == 0 and last.get("pass_count", 0) > 0:
            is_fixed = True
        elif last.get("fail_count", 0) > 0:
            is_fixed = False

    logger.info(
        "Repair complete. fixed=%s | files=%s | attempts=%d | tokens=%d",
        is_fixed,
        parsed.get("files_modified"),
        len(repair_log),
        token_usage.get("total_tokens", 0),
    )

    return {
        "patch":          parsed.get("diff") or "",
        "patch_target":   parsed.get("patch_target"),
        "files_modified": parsed.get("files_modified", []),
        "fix_summary":    parsed.get("fix_summary"),
        "is_fixed":       is_fixed,
        "fix_attempts":   len(repair_log),
        "repair_error":   None,
        "pipeline_trace": [span.to_dict()],
        "trajectory": [{
            "agent":            "RepairAgent",
            "action":           "react_repair",
            "model":            model_cfg.name,
            "is_fixed":         is_fixed,
            "patch_target":     parsed.get("patch_target"),
            "files_modified":   parsed.get("files_modified", []),
            "fix_attempts":     len(repair_log),
            "duration_seconds": span.duration_seconds,
            "token_usage":      token_usage,
        }],
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def _error_return(message: str) -> dict:
    return {
        "patch":          None,
        "patch_target":   None,
        "files_modified": [],
        "fix_summary":    None,
        "is_fixed":       False,
        "fix_attempts":   0,
        "repair_error":   message,
        "pipeline_trace": [],
        "trajectory": [{
            "agent":  "RepairAgent",
            "action": "setup_failed",
            "error":  message,
        }],
    }


def _parse_repair_response(raw: str) -> dict:
    text = (raw or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    payload = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        if payload is None:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    payload = json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

    if payload is None:
        return {
            "patch_target":   "unknown",
            "files_modified": [],
            "fix_summary":    "Could not parse agent response.",
            "is_fixed":       False,
            "diff":           text,
        }

    return {
        "patch_target":   str(payload.get("patch_target") or "unknown"),
        "files_modified": list(payload.get("files_modified") or []),
        "fix_summary":    str(payload.get("fix_summary") or ""),
        "is_fixed":       bool(payload.get("is_fixed", False)),
        "diff":           str(payload.get("diff") or ""),
    }
