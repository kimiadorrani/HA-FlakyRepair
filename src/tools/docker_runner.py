"""
Docker-based Flaky Test Execution — Tool Functions for the Detection Agent.

Provides four LangChain tools that the ReAct detection agent can call
adaptively. Each tool runs a specific execution strategy inside Docker
and returns structured JSON evidence.

Setup (clone, image build, persistent container) happens once per test
via make_detection_tools(), which returns the bound tool list alongside
a shared execution log that accumulates all tool call results.
"""

import json
import os
import subprocess
import logging
import re
from typing import Optional, Tuple

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

WORKSPACE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "workspaces", "idoft"
)

_built_images: set[str] = set()
_prepared_checkouts: dict[str, str] = {}
_active_repo_containers: dict[str, str] = {}

MAX_LOG_CHARS = 4000


# ── Infrastructure helpers ─────────────────────────────────────────────────

def _image_tag(project_name: str) -> str:
    return f"flaky-{project_name.lower()}:latest"


def _clone_and_checkout(repo_url: str, commit_sha: str, project_dir: str) -> None:
    if not os.path.exists(project_dir):
        logger.info("Cloning %s …", repo_url)
        subprocess.run(
            ["git", "clone", "--depth", "100", repo_url, project_dir],
            check=True, capture_output=True
        )

    cached_sha = _prepared_checkouts.get(project_dir)
    if cached_sha == commit_sha:
        return

    sha_missing = subprocess.run(
        ["git", "cat-file", "-e", commit_sha], cwd=project_dir, capture_output=True
    ).returncode != 0
    if sha_missing:
        subprocess.run(
            ["git", "fetch", "origin", commit_sha],
            cwd=project_dir, capture_output=True,
        )
        still_missing = subprocess.run(
            ["git", "cat-file", "-e", commit_sha], cwd=project_dir, capture_output=True
        ).returncode != 0
        if still_missing:
            subprocess.run(
                ["git", "fetch", "origin", "--depth", "50", "--update-shallow"],
                cwd=project_dir, capture_output=True,
            )
        still_missing = subprocess.run(
            ["git", "cat-file", "-e", commit_sha], cwd=project_dir, capture_output=True
        ).returncode != 0
        if still_missing:
            subprocess.run(
                ["git", "fetch", "origin", "--unshallow"],
                cwd=project_dir, capture_output=True,
            )

    subprocess.run(
        ["git", "checkout", "-f", commit_sha],
        cwd=project_dir, check=True, capture_output=True
    )
    subprocess.run(["git", "clean", "-fdx"], cwd=project_dir, capture_output=True)
    _prepared_checkouts[project_dir] = commit_sha


def _build_project_image(project_name: str, project_dir: str) -> str:
    tag = _image_tag(project_name)
    if tag in _built_images:
        return tag

    logger.info("Building Docker image %s …", tag)
    dockerfile_content = """\
FROM python:3.8-slim
RUN apt-get update && apt-get install -y git build-essential pkg-config libhdf5-dev libxml2-dev libxslt-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN pip install --timeout 120 "pip<24.1"
RUN pip install --timeout 120 "pipenv==2020.11.15"
RUN pip install --timeout 120 "setuptools" "wheel" "poetry" "poetry-core"
RUN if [ -f requirements.txt ]; then pip install --timeout 120 -r requirements.txt; fi
RUN if [ -f setup.py ]; then pip install --no-build-isolation -e .; elif [ -f pyproject.toml ]; then pip install --no-build-isolation -e . || pip install --no-build-isolation .; fi
RUN python -c "import pytest" >/dev/null 2>&1 || pip install "pytest<8"
RUN pip install "pytest-repeat<0.9.4"
RUN pip install pytest-randomly
"""
    dockerfile_path = os.path.join(project_dir, "Dockerfile.flaky")
    try:
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content)
        result = subprocess.run(
            ["docker", "build", "-t", tag, "-f", "Dockerfile.flaky", "."],
            cwd=project_dir, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker build failed for {project_name}:\n{result.stderr}\n{result.stdout}"
            )
        _built_images.add(tag)
        logger.info("Image %s built.", tag)
    finally:
        if os.path.exists(dockerfile_path):
            os.remove(dockerfile_path)
    return tag


def _repo_container_name(project_name: str, commit_sha: str) -> str:
    safe_project = re.sub(r"[^a-z0-9_.-]+", "-", project_name.lower())
    safe_sha = re.sub(r"[^a-z0-9]+", "", commit_sha.lower())[:12]
    return f"flaky_repo_{safe_project}_{safe_sha or 'head'}"


def _ensure_repo_container(image_tag: str, project_name: str, commit_sha: str) -> str:
    container_name = _repo_container_name(project_name, commit_sha)
    if container_name in _active_repo_containers:
        return container_name

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)
    result = subprocess.run(
        ["docker", "run", "-d", "--name", container_name, image_tag, "tail", "-f", "/dev/null"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to start container for {project_name}:\n{result.stderr}"
        )
    _active_repo_containers[container_name] = container_name
    logger.info("Started container %s for %s @ %s.", container_name, project_name, commit_sha)
    return container_name


def cleanup_project_container(project_name: str, sha: str) -> None:
    """Remove the running container for this project+SHA. The image is kept for reuse."""
    container_name = _repo_container_name(project_name, sha)
    result = subprocess.run(
        ["docker", "rm", "-f", container_name], capture_output=True, check=False
    )
    _active_repo_containers.pop(container_name, None)
    if result.returncode == 0:
        logger.info("Removed container %s.", container_name)
    else:
        logger.debug("Container %s already gone or not found.", container_name)


def reset_docker_environment() -> None:
    """Remove all active containers and all built images. Call at end of session."""
    logger.info("Cleaning up Docker: removing containers and images.")
    for container_name in list(_active_repo_containers.values()):
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)
    _active_repo_containers.clear()

    for image_tag in list(_built_images):
        subprocess.run(["docker", "rmi", "-f", image_tag], capture_output=True, check=False)
        logger.info("Removed image %s.", image_tag)
    _built_images.clear()


def _docker_exec(
    container_name: str,
    cmd_list: list[str],
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container_name] + cmd_list,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=timeout,
    )


def _make_pytest_cmd(args: list[str], enable_randomly: bool = False) -> list[str]:
    if not enable_randomly:
        args = ["-p", "no:randomly"] + args
    return ["pytest"] + args


def _extract_pytest_count(log: Optional[str], label: str) -> int:
    if not log:
        return 0
    total = 0
    for match in re.finditer(rf"(\d+)\s+{label}\b", log):
        total += int(match.group(1))
    return total


def _derive_outcome_profile(pass_count: int, fail_count: int, error: Optional[str] = None) -> str:
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
    """Serialize a profile dict to a compact JSON string for the LLM."""
    return json.dumps({
        "strategy": profile["name"],
        "outcome": profile["outcome_profile"],
        "pass_count": profile["pass_count"],
        "fail_count": profile["fail_count"],
        "passing_log": _compact_log(profile.get("passing_log")),
        "failing_log": _compact_log(profile.get("failing_log")),
    })


# ── Execution strategies ───────────────────────────────────────────────────

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
        # try a single clean run to get a passing log
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
        return {"name": "repeated_in_process", "pass_count": 1 if passing_log else 0,
                "fail_count": fail_count, "outcome_profile": outcome,
                "passing_log": passing_log, "failing_log": res.stdout}
    except subprocess.TimeoutExpired:
        return {"name": "repeated_in_process", "pass_count": 0, "fail_count": 1,
                "outcome_profile": "always_fail", "passing_log": None,
                "failing_log": "Timeout on repeated run."}


def _run_isolated(container_name: str, test_name: str, n: int) -> dict:
    passing_log = None
    failing_log = None
    pass_count = 0
    fail_count = 0
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
    test_dir = os.path.dirname(test_file) or "."
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
        stdout = res.stdout or ""
        if res.returncode == 0:
            return {"name": f"suite_randomized_seed{seed}", "pass_count": 1, "fail_count": 0,
                    "outcome_profile": "always_pass", "passing_log": stdout, "failing_log": None}
        # Check if our specific test is the one that failed (exact match to avoid false positives)
        if re.search(rf"\b{re.escape(test_id)}\s+FAILED", stdout):
            return {"name": f"suite_randomized_seed{seed}", "pass_count": 0, "fail_count": 1,
                    "outcome_profile": "always_fail", "passing_log": None, "failing_log": stdout}
        # Other tests failed but not ours — still counts as passing for our target
        return {"name": f"suite_randomized_seed{seed}", "pass_count": 1, "fail_count": 0,
                "outcome_profile": "always_pass", "passing_log": stdout, "failing_log": None}
    except subprocess.TimeoutExpired:
        return {"name": f"suite_randomized_seed{seed}", "pass_count": 0, "fail_count": 1,
                "outcome_profile": "always_fail", "passing_log": None,
                "failing_log": f"Timeout on suite randomized run (seed={seed})."}


# ── Public factory ─────────────────────────────────────────────────────────

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
    project_dir = os.path.join(WORKSPACE_DIR, project_name)

    _clone_and_checkout(project_url, sha_detected, project_dir)
    image_tag = _build_project_image(project_name, project_dir)
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


# ── Repair helpers ─────────────────────────────────────────────────────────

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


def make_repair_tools(
    project_url: str,
    sha_detected: str,
    test_name: str,
    execution_profiles: list[dict],
) -> Tuple[list, list]:
    """
    Set up Docker environment for the repair agent and return (tools, repair_log).

    The container is a fresh instance of the already-built image — the host
    workspace is never touched. All file writes go to /app/ inside the container.

    tools      — five LangChain tools for file I/O and verification
    repair_log — mutable list; each verify call appends its profile dict
    """
    project_name = project_url.rstrip("/").split("/")[-1]
    project_dir  = os.path.join(WORKSPACE_DIR, project_name)

    _clone_and_checkout(project_url, sha_detected, project_dir)
    image_tag      = _build_project_image(project_name, project_dir)
    container_name = _ensure_repo_container(image_tag, project_name, sha_detected)

    reproducing_profile = _find_reproducing_profile(execution_profiles)
    repair_log: list[dict] = []

    @tool
    def list_files(directory: str = ".") -> str:
        """
        List Python source files inside the repo.
        directory is relative to the repo root (e.g. '.' or 'src/mypackage').
        """
        safe_dir = directory.lstrip("/").replace("..", "").strip() or "."
        result = _docker_exec(
            container_name,
            [
                "find", f"/app/{safe_dir}",
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
        Re-run the exact strategy that originally reproduced the flakiness.
        If fail_count is 0 in the result, the fix is verified.
        """
        if reproducing_profile:
            profile = _replay_profile(container_name, test_name, reproducing_profile)
        else:
            profile = _run_solo(container_name, test_name)
        repair_log.append(profile)
        return _tool_result(profile)

    @tool
    def get_diff() -> str:
        """
        Return the unified git diff of all changes made inside the container so far.
        Call this once your fix is verified before producing the final JSON output.
        """
        result = _docker_exec(container_name, ["git", "diff"], timeout=30)
        diff = result.stdout.strip()
        return diff if diff else "No changes detected."

    return [list_files, read_file, write_file, run_verification, get_diff], repair_log
