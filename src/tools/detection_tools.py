"""
Detection Tools — LangChain tool factory for the Detection Agent.

Returns four tools bound to a specific test's Docker container:
  - run_solo
  - run_repeated_in_process
  - run_isolated_reruns
  - run_suite_randomized

Usage:
    tools, execution_log = make_detection_tools(project_url, sha, test_name)
    # pass tools to create_react_agent; execution_log accumulates all profiles
"""

import logging
import os
from typing import Tuple

from langchain_core.tools import tool

from src.tools.docker_infra import (
    WORKSPACE_DIR,
    _clone_and_checkout,
    _build_project_image,
    _ensure_repo_container,
)
from src.tools.execution_strategies import (
    _run_solo, _run_repeated, _run_isolated, _run_suite_randomized,
    _tool_result,
)

logger = logging.getLogger(__name__)


def make_detection_tools(
    project_url: str,
    sha_detected: str,
    test_name: str,
) -> Tuple[list, list]:
    """
    Set up Docker environment and return (tools, execution_log).

    tools         — four LangChain tools bound to this test's container
    execution_log — shared mutable list; each tool call appends its profile dict
    """
    project_name = project_url.rstrip("/").split("/")[-1]
    project_dir  = os.path.join(WORKSPACE_DIR, project_name)

    _clone_and_checkout(project_url, sha_detected, project_dir)
    image_tag      = _build_project_image(project_name, project_dir)
    container_name = _ensure_repo_container(image_tag, project_name, sha_detected)

    execution_log: list[dict] = []

    @tool
    def run_solo() -> str:
        """
        Run the target test exactly once in isolation.
        Always call this first. Returns outcome, pass/fail counts, and logs.
        """
        profile = _run_solo(container_name, test_name)
        execution_log.append(profile)
        return _tool_result(profile)

    @tool
    def run_repeated_in_process(n: int = 50) -> str:
        """
        Run the target test N times sequentially in the same process using --count=N.
        Use this when run_solo always passes to detect state-pollution flakiness.
        Default n=50 for reliable detection.
        """
        profile = _run_repeated(container_name, test_name, n)
        execution_log.append(profile)
        return _tool_result(profile)

    @tool
    def run_isolated_reruns(n: int = 50) -> str:
        """
        Run the target test N times each in a fresh independent subprocess.
        Use this when run_repeated_in_process always passes to detect random flakiness.
        Default n=50 for reliable detection.
        """
        profile = _run_isolated(container_name, test_name, n)
        execution_log.append(profile)
        return _tool_result(profile)

    @tool
    def run_suite_randomized(seed: int) -> str:
        """
        Run the full test directory with a random execution order using the given seed.
        Use this when all isolated strategies always pass, to detect order-dependent flakiness.
        Call multiple times with different seeds (1, 2, 3, ...) up to 10 before giving up.
        """
        profile = _run_suite_randomized(container_name, test_name, seed)
        execution_log.append(profile)
        return _tool_result(profile)

    return [run_solo, run_repeated_in_process, run_isolated_reruns, run_suite_randomized], execution_log
