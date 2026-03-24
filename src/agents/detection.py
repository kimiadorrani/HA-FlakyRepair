"""
Detection Agent — LLM-powered flaky test classification and root cause analysis.

Uses Qwen via Groq to analyze pass/fail logs captured by the Docker runner.
"""

import os
import logging

from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate

from src.state import RepairState

logger = logging.getLogger(__name__)


def detection_agent_node(state: RepairState) -> dict:
    """
    LangGraph node: Analyzes test logs and determines flaky type + root cause.

    If the Docker runner reproduced flakiness (both pass and fail logs exist),
    we send them to the Qwen LLM for root cause analysis.
    If not reproduced, we record that and skip the LLM call.
    """
    test_name = state.get("test_name", "unknown")
    project_url = state.get("project_url", "unknown")
    categories = state.get("category", [])
    category_str = ", ".join(categories) if categories else "Unknown"
    is_reproduced = state.get("is_flakiness_reproduced", False)
    passing_log = state.get("passing_log")
    failing_log = state.get("failing_log")
    error_message = state.get("error_message")
    pass_count = state.get("pass_count", 0)
    fail_count = state.get("fail_count", 0)
    outcome_profile = state.get("outcome_profile", "unknown")

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

    # ── Case 2: Flakiness NOT reproduced ──
    if not is_reproduced:
        logger.info("Flakiness NOT reproduced. Skipping LLM analysis.")

        # Still classify based on dataset prior
        flaky_type = _classify_from_prior(categories)

        return {
            "flaky_type": f"{flaky_type} (not reproduced)",
            "root_cause_analysis": (
                "Could not reproduce flakiness in Docker. "
                f"Outcome profile: {outcome_profile}. "
                f"Pass count: {pass_count}. "
                f"Fail count: {fail_count}. "
                f"Dataset category prior: {category_str}. "
                f"Passing log available: {bool(passing_log)}. "
                f"Failing log available: {bool(failing_log)}."
            ),
            "trajectory": [{
                "agent": "DetectionAgent",
                "action": "classified_without_reproduction",
                "flaky_type": flaky_type,
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

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are an expert QA and Python test engineer.
Your goal is to detect the flaky-test behavior shown in the logs.

Possible flaky categories:
- NIO (Non-Idempotent-Outcome): Passes when run alone, fails when run multiple times
  in the same process.
- NOD (Non-Deterministic): Fails randomly depending on timing, scheduling, network,
  or randomness.
- OD (Order-Dependent): Fails only if it runs before or after another specific test.
- OD-Vic: Order-Dependent Victim.
- OD-Brit: Order-Dependent Brittle.

Analyze the logs and:
1. Identify which flaky category best matches the observed behavior
2. Identify the exact line of failure
3. Explain the root cause concisely

Do not suggest a fix. Base your answer only on the observed logs."""),
        ("user", """
**Repository:** {repo_url}
**Test Name:** {target_test}

--- THE TEST PASSES WITH THIS OUTPUT ---
{passing_log}

--- THE TEST FLAKES/FAILS WITH THIS OUTPUT ---
{failing_log}

Determine the most likely flaky category from the logs, then identify the failing line and root cause.
""")
    ])

    chain = prompt | llm

    try:
        response = chain.invoke({
            "repo_url": project_url,
            "target_test": test_name,
            "passing_log": passing_log or "N/A",
            "failing_log": failing_log or "N/A",
        })
        analysis = response.content
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        analysis = f"LLM analysis failed: {e}"

    # Determine flaky type from LLM response + dataset prior
    flaky_type = _classify_from_prior(categories)

    logger.info("Detection complete. Flaky type: %s", flaky_type)

    return {
        "flaky_type": flaky_type,
        "root_cause_analysis": analysis,
        "trajectory": [{
            "agent": "DetectionAgent",
            "action": "llm_root_cause_analysis",
            "flaky_type": flaky_type,
            "reproduced": True,
            "analysis_length": len(analysis),
        }]
    }


def _classify_from_prior(categories: list[str]) -> str:
    """Map dataset category labels to human-readable flaky type."""
    if not categories:
        return "Unknown"

    cat = categories[0]  # primary category
    category_map = {
        "NIO": "Non-Idempotent-Outcome (NIO)",
        "NOD": "Non-Deterministic (NOD)",
        "OD": "Order-Dependent (OD)",
        "OD-Vic": "Order-Dependent Victim (OD-Vic)",
        "OD-Brit": "Order-Dependent Brittle (OD-Brit)",
        "ID": "Implementation-Dependent (ID)",
        "UD": "Undefined (UD)",
    }
    return category_map.get(cat, f"Other ({cat})")
