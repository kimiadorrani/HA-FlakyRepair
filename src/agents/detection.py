"""
Detection Agent — LLM-powered flaky test classification and root cause analysis.

Uses Qwen via Groq to analyze rerun logs captured by the Docker runner and
classify flakiness without using the dataset label as a prior.
"""

import json
import logging
import re

from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate

from src.state import RepairState

logger = logging.getLogger(__name__)

MAX_LOG_CHARS = 4000


def detection_agent_node(state: RepairState) -> dict:
    """
    LangGraph node: Analyzes test logs and determines flaky type + root cause.

    If the Docker runner reproduced flakiness (both pass and fail logs exist),
    we send them to the Qwen LLM for root cause analysis.
    If not reproduced, we record that and skip the LLM call.
    """
    test_name = state.get("test_name", "unknown")
    project_url = state.get("project_url", "unknown")
    is_reproduced = state.get("is_flakiness_reproduced", False)
    passing_log = state.get("passing_log")
    failing_log = state.get("failing_log")
    error_message = state.get("error_message")
    logger.info("=== Detection Agent: %s ===", test_name)

    # ── Case 1: Error during Docker execution ──
    if error_message and not is_reproduced:
        logger.warning("Docker runner encountered an error: %s", error_message)
        return {
            "flaky_type": "Error — could not execute",
            "root_cause_analysis": f"Docker runner error: {error_message}",
            "trajectory": [{
                "agent": "DetectionAgent",
                "action": "skipped_analysis_due_to_error",
                "error": error_message,
            }]
        }

    execution_profiles = state.get("execution_profiles") or []
    protocol_logs = _build_protocol_logs(execution_profiles)

    # ── Case 2: Flakiness NOT reproduced ──
    if not is_reproduced:
        logger.info("Flakiness NOT reproduced. Skipping LLM analysis.")

        return {
            "flaky_type": "Not flaky observed",
            "root_cause_analysis": (
                "Could not reproduce flaky behavior during reruns. "
                f"Passing log available: {bool(passing_log)}. "
                f"Failing log available: {bool(failing_log)}."
            ),
            "trajectory": [{
                "agent": "DetectionAgent",
                "action": "classified_without_reproduction",
                "flaky_type": "Not flaky observed",
                "reproduced": False,
            }]
        }

    # ── Case 3: Flakiness REPRODUCED → LLM analysis ──
    logger.info("Flakiness REPRODUCED. Calling Qwen LLM for root cause analysis…")

    llm = ChatGroq(
        model="qwen/qwen3-32b",
        temperature=0.2,
        max_tokens=2048,
    )
    prompt_passing_log = _compact_log_for_llm(passing_log)
    prompt_failing_log = _compact_log_for_llm(failing_log)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are an expert QA and Python test engineer.
Your goal is to decide from rerun evidence whether the target test shows flaky behavior,
and if it does, which flaky category best matches the evidence.

Allowed flaky categories:
- NIO: Non-Idempotent Outcome
- NOD: Non-Deterministic
- Unknown: flaky behavior is present but the category is unclear

Important constraints:
- You are NOT given the dataset label. Do not assume any prior category.
- Base your answer only on the observed logs below.
- If the evidence does not support a flaky classification, say it is not flaky.
- Do not suggest a fix.

Respond with strict JSON using exactly these keys:
{{
  "is_flaky": true or false,
  "flaky_category": "NIO" | "NOD" | "Unknown" | "Not flaky",
  "failing_line": "<short snippet or N/A>",
  "root_cause_analysis": "<concise explanation>"
}}"""),
        ("user", """
**Repository:** {repo_url}
**Test Name:** {target_test}

**Protocol Logs**
{protocol_logs}

Determine the most likely flaky category from the logs, then identify the failing line and root cause.
""")
    ])

    chain = prompt | llm

    try:
        response = chain.invoke({
            "repo_url": project_url,
            "target_test": test_name,
            "protocol_logs": protocol_logs or _build_protocol_logs([
                {
                    "name": "aggregated_passing_log",
                    "passing_log": prompt_passing_log,
                },
                {
                    "name": "aggregated_failing_log",
                    "failing_log": prompt_failing_log,
                },
            ]),
        })
        analysis = response.content
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        analysis = json.dumps({
            "is_flaky": False,
            "flaky_category": "Unknown",
            "failing_line": "N/A",
            "root_cause_analysis": f"LLM analysis failed: {e}",
        })

    parsed = _parse_detection_response(analysis)
    flaky_type = parsed["flaky_type"]
    root_cause_analysis = parsed["root_cause_analysis"]

    logger.info("Detection complete. Flaky type: %s", flaky_type)

    return {
        "flaky_type": flaky_type,
        "root_cause_analysis": root_cause_analysis,
        "trajectory": [{
            "agent": "DetectionAgent",
            "action": "llm_root_cause_analysis",
            "flaky_type": flaky_type,
            "reproduced": True,
            "analysis_length": len(root_cause_analysis),
        }]
    }


def _build_protocol_logs(execution_profiles: list[dict]) -> str:
    """Serialize protocol evidence without leaking structured pass/fail summaries."""
    sections: list[str] = []
    for profile in execution_profiles:
        name = str(profile.get("name") or "unknown_protocol")
        passing_log = _compact_log_for_llm(profile.get("passing_log"))
        failing_log = _compact_log_for_llm(profile.get("failing_log"))
        sections.append(
            "\n".join(
                [
                    f"=== Protocol: {name} ===",
                    "--- Passing Output ---",
                    passing_log,
                    "--- Failing Output ---",
                    failing_log,
                ]
            )
        )
    return "\n\n".join(sections)


def _compact_log_for_llm(log_text: str | None, max_chars: int = MAX_LOG_CHARS) -> str:
    """Trim large logs to a head/tail excerpt that stays under model limits."""
    text = (log_text or "").strip()
    if not text:
        return "N/A"
    if len(text) <= max_chars:
        return text

    keep_each = max_chars // 2
    head = text[:keep_each].rstrip()
    tail = text[-keep_each:].lstrip()
    omitted = len(text) - len(head) - len(tail)
    return (
        f"{head}\n\n"
        f"... [trimmed {omitted} characters] ...\n\n"
        f"{tail}"
    )


def _parse_detection_response(raw_response: str) -> dict[str, str]:
    """Parse the LLM classification response, with a safe fallback."""
    text = (raw_response or "").strip()
    payload: dict[str, object] | None = None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                payload = None

    if payload is None:
        return {
            "flaky_type": "Unknown",
            "root_cause_analysis": text or "The detection model returned an empty response.",
        }

    is_flaky = bool(payload.get("is_flaky"))
    flaky_category = str(payload.get("flaky_category") or "Unknown").strip()
    failing_line = str(payload.get("failing_line") or "N/A").strip()
    explanation = str(payload.get("root_cause_analysis") or "").strip()

    if not is_flaky or flaky_category.lower() == "not flaky":
        flaky_type = "Not flaky observed"
    else:
        flaky_type = flaky_category

    if failing_line and failing_line != "N/A":
        root_cause_analysis = f"Failing line: {failing_line}\n{explanation}".strip()
    else:
        root_cause_analysis = explanation or text or "No analysis returned."

    return {
        "flaky_type": flaky_type,
        "root_cause_analysis": root_cause_analysis,
    }
