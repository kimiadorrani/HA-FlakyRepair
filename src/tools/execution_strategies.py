"""
Execution Strategies — pytest invocation patterns used by both agents.

Each function runs one strategy inside a Docker container and returns
a profile dict with: name, pass_count, fail_count, outcome_profile,
passing_log, failing_log.
"""

import json
import logging
import os
import re
import subprocess
from typing import Optional

from langsmith.run_helpers import get_current_run_tree

from src.tools.docker_infra import _docker_exec, _make_pytest_cmd

logger = logging.getLogger(__name__)

MAX_LOG_CHARS = 4000


# ── Log / result helpers ───────────────────────────────────────────────────

def _extract_pytest_count(log: Optional[str], label: str) -> int:
    if not log:
        return 0
    total = 0
    for match in re.finditer(rf"(\d+)\s+{label}\b", log):
        total += int(match.group(1))
    return total


def _derive_outcome_profile(
    pass_count: int, fail_count: int, error: Optional[str] = None
) -> str:
    if error:
        return "execution_error"
    if pass_count > 0 and fail_count > 0:
        return "mixed"
    if pass_count > 0:
        return "always_pass"
    if fail_count > 0:
        return "always_fail"
    return "unknown"


def _compact_log(log_text: Optional[str], max_chars: int = 2000) -> Optional[str]:
    text = (log_text or "").strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"... [{omitted} chars trimmed] ...\n\n{text[-max_chars:].lstrip()}"


def _tool_result(profile: dict) -> str:
    """Serialize a profile dict to a compact JSON string for the LLM and add LangSmith metadata."""
    
    # Enrich the LangSmith tool span with structured execution data
    run = get_current_run_tree()
    if run:
        run.add_metadata({
            "strategy":   profile["name"],
            "outcome":    profile["outcome_profile"],
            "pass_count": profile["pass_count"],
            "fail_count": profile["fail_count"],
        })
        if profile.get("outcome_profile") in ["always_pass", "mixed"]:
            run.add_tags(["passed"])
        if profile.get("outcome_profile") in ["always_fail", "mixed"]:
            run.add_tags(["failed"])

    return json.dumps({
        "strategy":   profile["name"],
        "outcome":    profile["outcome_profile"],
        "pass_count": profile["pass_count"],
        "fail_count": profile["fail_count"],
        "passing_log": _compact_log(profile.get("passing_log")),
        "failing_log": _compact_log(profile.get("failing_log")),
    })


# ── Strategies ─────────────────────────────────────────────────────────────

def _run_solo(container_name: str, test_name: str) -> dict:
    try:
        res = _docker_exec(
            container_name,
            _make_pytest_cmd(["-v", "--tb=short", test_name]),
            timeout=120,
        )
        if res.returncode == 0:
            return {"name": "solo", "pass_count": 1, "fail_count": 0,
                    "outcome_profile": "always_pass", "passing_log": res.stdout, "failing_log": None}
        return {"name": "solo", "pass_count": 0, "fail_count": 1,
                "outcome_profile": "always_fail", "passing_log": None, "failing_log": res.stdout}
    except subprocess.TimeoutExpired:
        return {"name": "solo", "pass_count": 0, "fail_count": 1,
                "outcome_profile": "always_fail", "passing_log": None,
                "failing_log": "Timeout on solo run."}


def _run_repeated(container_name: str, test_name: str, n: int) -> dict:
    try:
        res = _docker_exec(
            container_name,
            _make_pytest_cmd([f"--count={n}", "-v", "--tb=short", test_name]),
            timeout=600,
        )
        if res.returncode == 0:
            return {"name": "repeated_in_process", "pass_count": n, "fail_count": 0,
                    "outcome_profile": "always_pass", "passing_log": res.stdout, "failing_log": None}
        fail_count = _extract_pytest_count(res.stdout, "failed") or 1
        passing_log = None
        try:
            single = _docker_exec(
                container_name,
                _make_pytest_cmd(["-v", "--tb=short", test_name]),
                timeout=120,
            )
            if single.returncode == 0:
                passing_log = single.stdout
        except Exception:
            pass
        outcome = _derive_outcome_profile(1 if passing_log else 0, fail_count)
        return {"name": "repeated_in_process",
                "pass_count": 1 if passing_log else 0,
                "fail_count": fail_count, "outcome_profile": outcome,
                "passing_log": passing_log, "failing_log": res.stdout}
    except subprocess.TimeoutExpired:
        return {"name": "repeated_in_process", "pass_count": 0, "fail_count": 1,
                "outcome_profile": "always_fail", "passing_log": None,
                "failing_log": "Timeout on repeated run."}


def _run_isolated(container_name: str, test_name: str, n: int) -> dict:
    passing_log = None
    failing_log = None
    pass_count  = 0
    fail_count  = 0
    for _ in range(n):
        try:
            res = _docker_exec(
                container_name,
                _make_pytest_cmd(["-v", "--tb=short", test_name]),
                timeout=180,
            )
            if res.returncode == 0:
                pass_count += 1
                if not passing_log:
                    passing_log = res.stdout
            else:
                fail_count += 1
                if not failing_log:
                    failing_log = res.stdout
            if passing_log and failing_log:
                break
        except subprocess.TimeoutExpired:
            fail_count += 1
            if not failing_log:
                failing_log = "Timeout on isolated run."
            break
    return {"name": "isolated_reruns", "pass_count": pass_count, "fail_count": fail_count,
            "outcome_profile": _derive_outcome_profile(pass_count, fail_count),
            "passing_log": passing_log, "failing_log": failing_log}


def _run_suite_randomized(container_name: str, test_name: str, seed: int) -> dict:
    """Run the test's parent directory with a random seed to catch order-dependent failures."""
    test_file = test_name.split("::")[0]
    test_dir  = os.path.dirname(test_file) or "."
    try:
        res = _docker_exec(
            container_name,
            _make_pytest_cmd(
                [f"--randomly-seed={seed}", "-v", "--tb=short", test_dir],
                enable_randomly=True,
            ),
            timeout=300,
        )
        test_id = test_name.split("::", 1)[-1] if "::" in test_name else test_name
        stdout  = res.stdout or ""
        if res.returncode == 0:
            return {"name": f"suite_randomized_seed{seed}", "pass_count": 1, "fail_count": 0,
                    "outcome_profile": "always_pass", "passing_log": stdout, "failing_log": None}
        if re.search(rf"\b{re.escape(test_id)}\s+FAILED", stdout):
            return {"name": f"suite_randomized_seed{seed}", "pass_count": 0, "fail_count": 1,
                    "outcome_profile": "always_fail", "passing_log": None, "failing_log": stdout}
        # Other tests failed but not ours — counts as passing for our target
        return {"name": f"suite_randomized_seed{seed}", "pass_count": 1, "fail_count": 0,
                "outcome_profile": "always_pass", "passing_log": stdout, "failing_log": None}
    except subprocess.TimeoutExpired:
        return {"name": f"suite_randomized_seed{seed}", "pass_count": 0, "fail_count": 1,
                "outcome_profile": "always_fail", "passing_log": None,
                "failing_log": f"Timeout on suite randomized run (seed={seed})."}
