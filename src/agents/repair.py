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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from src.config.models import get_model
from src.state import RepairState
from src.tools.repair_tools import (
    make_repair_tools, verify_fix, _find_reproducing_profile,
)
from src.tools.docker_infra import cleanup_project_container, _repo_container_name
from src.tracing.agent_trace import PipelineTrace, TraceCallback

logger = logging.getLogger(__name__)

REPAIR_TIMEOUT_SECONDS = 800

_SYSTEM_PROMPT = """You are an expert software engineer tasked with fixing a flaky test.

You have five tools:
- list_files(directory)          : list Python files in a directory of the repo
- read_file(file_path)           : read a file with line numbers (path relative to repo root)
- write_file(file_path, content) : overwrite a file (provide COMPLETE file content)
- run_verification()             : re-run the exact strategy that reproduced the flakiness
- get_diff()                     : show all changes made so far as a unified diff

IMPORTANT: Use ONLY the structured tool-calling interface above. Do NOT embed tool calls
as XML tags (e.g. <invoke>, <minimax:tool_call>) — they will not be executed.

Fix protocol:
1. Read the root cause analysis carefully.
2. Use list_files and read_file to understand the relevant code.
3. Apply a minimal fix using write_file.
4. Call run_verification() — it runs multiple strategies suited to the flaky type.
   The fix is verified only when the returned JSON shows "verified": true.
   If "verified" is false, read the per-strategy details to understand what still fails.
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
    flaky_type         = state.get("flaky_type") or ""
    # Container name is derived by _repo_container_name() in docker_infra
    container_name     = _repo_container_name(project_name, sha)

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
        tools, repair_log, last_verified = make_repair_tools(
            project_url, sha, test_name, execution_profiles, flaky_type
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

            invoke_kwargs = {
                "messages": [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(content=repair_prompt),
                ]
            }
            invoke_config = {
                "callbacks": [callback],
                "recursion_limit": 60,
                "run_name": f"repair/{project_name}/{test_name}",
                "tags": ["repair", project_name],
                "metadata": {
                    "session_id": session_id,
                    "project":    project_name,
                    "test_name":  test_name,
                    "model":      model_cfg.name,
                    "flaky_type": flaky_type,
                    "commit_sha": sha,
                },
            }

            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(agent.invoke, invoke_kwargs, invoke_config)
                    result = future.result(timeout=REPAIR_TIMEOUT_SECONDS)
                final_content = result["messages"][-1].content
            except FutureTimeoutError:
                logger.warning(
                    "Repair agent timed out after %ds for %s", REPAIR_TIMEOUT_SECONDS, test_name
                )
                final_content = json.dumps({
                    "patch_target":   "unknown",
                    "files_modified": [],
                    "fix_summary":    f"Timed out after {REPAIR_TIMEOUT_SECONDS}s without a verified fix.",
                    "is_fixed":       False,
                    "diff":           "",
                })
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
        pass  # container cleaned up after optional XML fallback below

    span        = trace.spans[0]
    token_usage = span.token_usage
    parsed      = _parse_repair_response(final_content)

    # ── XML tool-call fallback (MiniMax quirk) ───────────────────────────────
    # If the model emitted an un-executed <invoke name="write_file"> block and
    # no structured write happened, extract & apply the fix manually.
    if not parsed.get("files_modified") and "<invoke" in final_content and "write_file" in final_content:
        logger.warning("Attempting XML tool-call fallback for %s", test_name)
        fallback = _apply_xml_fallback(
            text=final_content,
            test_name=test_name,
            container_name=container_name,
            flaky_type=flaky_type,
            execution_profiles=execution_profiles,
            repair_log=repair_log,
        )
        if fallback:
            logger.info("XML fallback succeeded — files: %s", fallback["files_modified"])
            parsed = fallback
        else:
            logger.warning("XML fallback found no usable write_file calls.")

    cleanup_project_container(project_name, sha)

    # Cross-check parsed is_fixed against the most recent verification call.
    # last_verified holds [True/False] from the last run_verification() the
    # agent executed.  We do NOT scan all of repair_log because that would
    # incorrectly set is_fixed=False whenever any earlier attempt failed.
    is_fixed = parsed.get("is_fixed", False)
    if last_verified:
        # The agent ran at least one verification — trust its last result.
        is_fixed = last_verified[-1]
    elif repair_log:
        # Fallback: agent never called run_verification but wrote files;
        # check the last profile entry only.
        last_profile = repair_log[-1]
        if last_profile.get("fail_count", 0) > 0:
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


def _extract_xml_write_calls(text: str) -> list[tuple[str, str]]:
    """
    Extract (file_path, content) pairs from MiniMax-style XML tool calls.

    MiniMax sometimes emits tool calls as:
      <minimax:tool_call>
      <invoke name="write_file">
      <parameter name="file_path">path/to/file.py</parameter>
      <parameter name="content">...full content...</parameter>

    Closing tags are often absent (truncated). We extract what we can.
    Returns a list of (file_path, content) tuples.
    """
    results = []
    for invoke_match in re.finditer(r"<invoke\s+name=[\"']write_file[\"']>(.*?)(?=<invoke\s|$)", text, re.DOTALL):
        block = invoke_match.group(1)
        fp_match = re.search(r"<parameter\s+name=[\"']file_path[\"']>(.*?)</parameter>", block, re.DOTALL)
        ct_match = re.search(r"<parameter\s+name=[\"']content[\"']>(.*?)(?:</parameter>|$)", block, re.DOTALL)
        if ct_match:
            file_path = fp_match.group(1).strip() if fp_match else ""
            content   = ct_match.group(1)
            # Strip leading/trailing triple-quote wrapper MiniMax sometimes adds
            content = re.sub(r'^"""\s*', '', content)
            content = re.sub(r'\s*"""$', '', content)
            results.append((file_path, content))
    return results


def _apply_xml_fallback(
    text: str,
    test_name: str,
    container_name: str,
    flaky_type: str,
    execution_profiles: list[dict],
    repair_log: list[dict],
) -> dict | None:
    """
    If the agent emitted XML tool calls instead of structured calls (MiniMax quirk),
    extract write_file invocations, apply them via docker exec, run verification,
    and return a parsed result dict — or None if nothing usable was found.
    """
    import subprocess as _sp

    calls = _extract_xml_write_calls(text)
    if not calls:
        return None

    logger.warning("XML tool-call fallback: found %d write_file call(s) in agent output.", len(calls))

    files_modified = []
    for file_path, content in calls:
        # Infer file_path from test name if the model omitted it
        if not file_path:
            file_path = test_name.split("::")[0].strip()
            logger.warning("XML fallback: no file_path in call — inferred '%s' from test name.", file_path)

        safe_path = file_path.lstrip("/").replace("..", "").strip()
        full_path = f"/app/{safe_path}"
        try:
            r = _sp.run(
                ["docker", "exec", "-i", container_name, "sh", "-c", f"cat > {full_path}"],
                input=content, text=True, capture_output=True, timeout=30,
            )
            if r.returncode == 0:
                logger.info("XML fallback: wrote %s", safe_path)
                files_modified.append(safe_path)
            else:
                logger.error("XML fallback: write failed for %s: %s", safe_path, r.stderr[:200])
        except Exception as e:
            logger.error("XML fallback: exception writing %s: %s", safe_path, e)

    if not files_modified:
        return None

    # Run multi-strategy verification on the applied fix
    reproducing_profile = _find_reproducing_profile(execution_profiles)
    is_verified, profiles = verify_fix(container_name, test_name, flaky_type, reproducing_profile)
    for p in profiles:
        repair_log.append(p)

    # Get diff
    try:
        diff_result = _sp.run(
            ["docker", "exec", "-w", "/app", container_name, "git", "diff"],
            capture_output=True, text=True, timeout=30,
        )
        diff = diff_result.stdout.strip()
    except Exception:
        diff = ""

    return {
        "patch_target":   "test" if all("test" in f for f in files_modified) else "source",
        "files_modified": files_modified,
        "fix_summary":    "Fix extracted from XML tool call (model used non-standard format).",
        "is_fixed":       is_verified,
        "diff":           diff,
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
