"""
Repair Tools — LangChain tool factory + verification logic for the Repair Agent.

Returns five tools bound to a specific test's Docker container:
  - list_files   : list Python files in the repo
  - read_file    : read a file with line numbers
  - write_file   : overwrite a file with complete new content
  - run_verification : multi-strategy post-fix verification
  - get_diff     : show all changes as a unified diff

Also exports:
  verify_fix()              — multi-strategy verification (called by run_verification tool)
  _find_reproducing_profile — select the best profile for replay

Usage:
    tools, repair_log, last_verified = make_repair_tools(
        project_url, sha, test_name, execution_profiles, flaky_type
    )
"""

import json
import logging
import os
import subprocess
from typing import Optional, Tuple
from langchain_core.tools import tool
from langsmith.run_helpers import get_current_run_tree

from src.tools.docker_infra import (
    WORKSPACE_DIR,
    _clone_and_checkout,
    _build_project_image,
    _ensure_repo_container,
    _docker_exec,
)
from src.tools.execution_strategies import (
    _run_solo, _run_repeated, _run_isolated, _run_suite_randomized,
    _compact_log,
)

logger = logging.getLogger(__name__)


# ── Verification helpers ───────────────────────────────────────────────────

def _find_reproducing_profile(execution_profiles: list[dict]) -> Optional[dict]:
    """Return the profile that best demonstrated flakiness (mixed > failing)."""
    for p in reversed(execution_profiles):
        if p.get("pass_count", 0) > 0 and p.get("fail_count", 0) > 0:
            return p
    for p in reversed(execution_profiles):
        if p.get("fail_count", 0) > 0:
            return p
    return None


def _replay_profile(container_name: str, test_name: str, profile: dict) -> dict:
    """Re-run the same strategy that originally reproduced the flakiness."""
    name = profile.get("name", "")
    if name == "solo":
        return _run_solo(container_name, test_name)
    if name == "repeated_in_process":
        return _run_repeated(container_name, test_name, n=50)
    if name == "isolated_reruns":
        return _run_isolated(container_name, test_name, n=20)
    if name.startswith("suite_randomized_seed"):
        seed_str = name.replace("suite_randomized_seed", "")
        seed = int(seed_str) if seed_str.isdigit() else 1
        return _run_suite_randomized(container_name, test_name, seed)
    return _run_solo(container_name, test_name)


def verify_fix(
    container_name: str,
    test_name: str,
    flaky_type: str,
    reproducing_profile: Optional[dict],
) -> tuple[bool, list[dict]]:
    """
    Multi-strategy post-fix verification.  Verification burden exceeds the
    original reproduction burden — one lucky replay is not sufficient evidence.

    Strategy per flaky type:
      NIO     : replay reproducing profile + run_repeated(30) + run_isolated(10)
      NOD     : run_isolated(20) across fresh subprocesses
      OD-Vic  : run_solo (must pass) + suite_randomized seeds 1,2,3
      OD-Brit : run_solo (must pass) + suite_randomized seeds 1,2
      Unknown : replay + run_isolated(10)

    Returns (is_verified, profiles_run).  Fast-fails on first strategy that
    shows fail_count > 0 to avoid burning time on a clearly broken fix.
    """
    profiles: list[dict] = []
    ft = (flaky_type or "").upper().replace("-", "")  # "ODVIC", "ODBRIT", "NIO", "NOD"

    def _add(profile: dict) -> bool:
        """Append profile, return True if it passed (fail_count == 0)."""
        profiles.append(profile)
        return profile.get("fail_count", 0) == 0

    # Step 1: always replay the original reproducing strategy first
    if reproducing_profile:
        if not _add(_replay_profile(container_name, test_name, reproducing_profile)):
            return False, profiles

    # Step 2: type-specific additional checks
    orig_name = (reproducing_profile or {}).get("name", "")

    if ft == "NIO":
        if orig_name != "repeated_in_process":
            if not _add(_run_repeated(container_name, test_name, n=30)):
                return False, profiles
        if not _add(_run_isolated(container_name, test_name, n=10)):
            return False, profiles

    elif ft == "NOD":
        if not _add(_run_isolated(container_name, test_name, n=20)):
            return False, profiles

    elif ft in ("ODVIC", "ODBRIT"):
        if not _add(_run_solo(container_name, test_name)):
            return False, profiles
        seeds = [1, 2, 3] if ft == "ODVIC" else [1, 2]
        for seed in seeds:
            if orig_name == f"suite_randomized_seed{seed}":
                continue  # already covered by the replay above
            if not _add(_run_suite_randomized(container_name, test_name, seed)):
                return False, profiles

    else:
        if not _add(_run_isolated(container_name, test_name, n=10)):
            return False, profiles

    return True, profiles


# ── Tool factory ───────────────────────────────────────────────────────────

def make_repair_tools(
    project_url: str,
    sha_detected: str,
    test_name: str,
    execution_profiles: list[dict],
    flaky_type: str = "",
    instance: str = "",
) -> Tuple[list, list, list]:
    """
    Set up Docker environment for the repair agent and return
    (tools, repair_log, last_verified).

    tools          — five LangChain tools for file I/O and verification
    repair_log     — mutable list; every verify call appends its profile dicts
    last_verified  — single-element list; [True/False] set by the most recent
                     run_verification() call (empty until first call)
    """
    project_name = project_url.rstrip("/").split("/")[-1]
    project_dir  = os.path.join(WORKSPACE_DIR, project_name)

    _clone_and_checkout(project_url, sha_detected, project_dir)
    image_tag      = _build_project_image(project_name, project_dir)
    container_name = _ensure_repo_container(image_tag, project_name, sha_detected, instance)

    reproducing_profile = _find_reproducing_profile(execution_profiles)
    repair_log: list[dict]  = []
    last_verified: list[bool] = []  # populated by run_verification tool

    @tool
    def list_files(directory: str = ".") -> str:
        """
        List Python source files inside the repo.
        directory is relative to the repo root (e.g. '.' or 'src/mypackage').
        """
        safe_dir = directory.lstrip("/").replace("..", "").strip() or "."
        # Use "/app" when safe_dir is "." — "find /app/." produces paths like
        # "/app/./foo.py" which match the "-not -path '*/.*'" filter (the
        # literal dot segment matches '.*'), silently excluding everything.
        find_path = "/app" if safe_dir == "." else f"/app/{safe_dir}"
        result = _docker_exec(
            container_name,
            [
                "find", find_path,
                "-type", "f", "-name", "*.py",
                "-not", "-path", "*/.*",
                "-not", "-path", "*/__pycache__/*",
            ],
            timeout=30,
        )
        lines = [ln.replace("/app/", "", 1) for ln in result.stdout.strip().splitlines()]
        return "\n".join(lines[:150]) or "No Python files found."

    @tool
    def read_file(file_path: str) -> str:
        """
        Read a file from the repo (line-numbered output).
        file_path is relative to the repo root (e.g. 'src/foo.py').
        """
        safe_path = file_path.lstrip("/").replace("..", "").strip()
        result = _docker_exec(container_name, ["cat", f"/app/{safe_path}"], timeout=30)
        if result.returncode != 0:
            return f"Error reading '{safe_path}': {result.stdout.strip()}"
        lines = result.stdout.splitlines()
        return "\n".join(f"{i+1:4}: {ln}" for i, ln in enumerate(lines))

    @tool
    def write_file(file_path: str, content: str) -> str:
        """
        Overwrite a file inside the repo with new content.
        file_path is relative to the repo root.
        Provide the COMPLETE file content, not just changed lines.
        """
        safe_path = file_path.lstrip("/").replace("..", "").strip()
        full_path = f"/app/{safe_path}"

        run = get_current_run_tree()
        if run:
            run.add_metadata({"file_path": safe_path, "content_length": len(content)})
            run.add_tags(["file_write"])

        try:
            result = subprocess.run(
                ["docker", "exec", "-i", container_name,
                 "sh", "-c", f"cat > {full_path}"],
                input=content, text=True,
                capture_output=True, timeout=30,
            )
            if result.returncode != 0:
                return f"Error writing '{safe_path}': {result.stderr.strip()}"
            return f"Wrote {safe_path} successfully."
        except subprocess.TimeoutExpired:
            return f"Timeout writing '{safe_path}'."

    @tool
    def run_verification() -> str:
        """
        Verify the fix using a multi-strategy check tailored to the flaky type.
        Runs the original reproducing strategy PLUS type-specific extra checks
        (repeated runs, isolated reruns, randomised suite orderings).
        The fix is verified only when ALL strategies show fail_count == 0.
        Returns a JSON object with keys: verified (bool), strategies_run (list),
        total_pass, total_fail, and per-strategy details.
        """
        is_verified, profiles = verify_fix(
            container_name, test_name, flaky_type, reproducing_profile
        )
        for p in profiles:
            repair_log.append(p)
        # Track result of this call so repair_agent_node can cross-check is_fixed
        # without being confused by failures from earlier (discarded) attempts.
        last_verified.clear()
        last_verified.append(is_verified)

        total_pass = sum(p.get("pass_count", 0) for p in profiles)
        total_fail = sum(p.get("fail_count", 0) for p in profiles)

        run = get_current_run_tree()
        if run:
            run.add_metadata({
                "verified": is_verified,
                "strategies_run": [p["name"] for p in profiles],
                "total_pass": total_pass,
                "total_fail": total_fail,
            })
            run.add_tags(["verified_yes"] if is_verified else ["verified_no"])

        return json.dumps({
            "verified":      is_verified,
            "strategies_run": [p["name"] for p in profiles],
            "total_pass":    total_pass,
            "total_fail":    total_fail,
            "details": [
                {
                    "strategy":   p["name"],
                    "outcome":    p.get("outcome_profile"),
                    "pass_count": p.get("pass_count", 0),
                    "fail_count": p.get("fail_count", 0),
                    "failing_log": _compact_log(p.get("failing_log"))
                                   if p.get("fail_count", 0) > 0 else None,
                }
                for p in profiles
            ],
        })

    @tool
    def get_diff() -> str:
        """
        Return the unified git diff of all changes made inside the container so far.
        Call this once your fix is verified before producing the final JSON output.
        """
        result = _docker_exec(container_name, ["git", "diff"], timeout=30)
        diff = result.stdout.strip()
        return diff if diff else "No changes detected."

    return [list_files, read_file, write_file, run_verification, get_diff], repair_log, last_verified
