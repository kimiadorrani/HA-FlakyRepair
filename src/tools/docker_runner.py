"""
Docker-based Flaky Test Runner — LangGraph Tool Node.

Each project gets its own Docker image (built once, reused for all tests of that project).
Each test execution happens in a fresh container that is destroyed after use.
"""

import os
import subprocess
import logging
import re
from typing import Optional, Tuple

from src.state import RepairState

logger = logging.getLogger(__name__)

# Base directory where repos are cloned
WORKSPACE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "workspaces", "idoft"
)

# Track which project images have already been built during this session
_built_images: set[str] = set()
_prepared_checkouts: dict[str, str] = {}
_active_repo_containers: dict[str, str] = {}

def _image_tag(project_name: str) -> str:
    """Return the Docker image tag for a project."""
    return f"flaky-{project_name.lower()}:latest"


def _clone_and_checkout(repo_url: str, commit_sha: str, project_dir: str) -> None:
    """Clone the repo (if needed) and checkout the flaky commit."""
    if not os.path.exists(project_dir):
        logger.info("Cloning %s …", repo_url)
        subprocess.run(
            ["git", "clone", repo_url, project_dir],
            check=True, capture_output=True
        )

    cached_sha = _prepared_checkouts.get(project_dir)
    if cached_sha == commit_sha:
        logger.info("Workspace %s already prepared at %s, reusing checkout.", project_dir, commit_sha)
        return

    subprocess.run(["git", "fetch", "--all"], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-f", commit_sha],
        cwd=project_dir, check=True, capture_output=True
    )
    subprocess.run(["git", "clean", "-fdx"], cwd=project_dir, capture_output=True)
    _prepared_checkouts[project_dir] = commit_sha


def _cleanup_docker_state() -> None:
    """
    Remove Docker images, stopped containers, build cache, and volumes so repeated
    runs do not fill the local disk. This is intentionally aggressive because the
    workspace uses Docker only for this project.
    """
    logger.info("Cleaning Docker state to free disk space before the next run …")
    subprocess.run(
        ["docker", "system", "prune", "-af", "--volumes"],
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["docker", "builder", "prune", "-af"],
        capture_output=True,
        text=True,
        check=False,
    )
    _built_images.clear()


def reset_docker_environment() -> None:
    """
    Reset in-memory Docker runner caches between project batches without pruning
    local Docker images/layers. This preserves base-image cache and avoids
    repeated Docker Hub pulls during long preprocessing sessions.
    """
    logger.info("Resetting Docker runner caches between project batches.")
    for container_name in list(_active_repo_containers.values()):
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            check=False,
        )
    _active_repo_containers.clear()
    _built_images.clear()


def _build_project_image(project_name: str, project_dir: str) -> str:
    """
    Build a per-project Docker image with the project's own dependencies
    baked in. Returns the image tag.
    """
    tag = _image_tag(project_name)

    if tag in _built_images:
        logger.info("Image %s already built, reusing.", tag)
        return tag

    logger.info("Building Docker image %s for %s …", tag, project_name)

    # Create an in-memory Dockerfile tailored to this project.
    # We install project dependencies first and avoid forcing extra pytest
    # plugins globally, because some older repos pin older pytest versions.
    dockerfile_content = """\
FROM python:3.8-slim
RUN apt-get update && apt-get install -y git build-essential pkg-config libhdf5-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN pip install "pip<24.1"
RUN pip install "pipenv==2020.11.15"
RUN if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
RUN if [ -f setup.py ]; then pip install -e .; elif [ -f pyproject.toml ]; then pip install -e . || pip install .; fi
RUN python -c "import pytest" >/dev/null 2>&1 || pip install "pytest<8"
RUN pip install "pytest-repeat<0.9.4"
RUN pip install pytest-randomly
"""

    # Write a temporary Dockerfile inside the project dir
    dockerfile_path = os.path.join(project_dir, "Dockerfile.flaky")
    try:
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content)

        result = subprocess.run(
            ["docker", "build", "-t", tag, "-f", "Dockerfile.flaky", "."],
            cwd=project_dir,
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise Exception(
                f"Docker build failed for {project_name}:\n{result.stderr}\n{result.stdout}"
            )

        _built_images.add(tag)
        logger.info("Image %s built successfully.", tag)
    finally:
        # Clean up the temp Dockerfile
        if os.path.exists(dockerfile_path):
            os.remove(dockerfile_path)

    return tag


def _repo_container_name(project_name: str, commit_sha: str) -> str:
    safe_project = re.sub(r"[^a-z0-9_.-]+", "-", project_name.lower())
    safe_sha = re.sub(r"[^a-z0-9]+", "", commit_sha.lower())[:12]
    return f"flaky_repo_{safe_project}_{safe_sha or 'head'}"


def _ensure_repo_container(
    image_tag: str,
    project_name: str,
    commit_sha: str,
) -> str:
    """Start or reuse one long-lived container for the whole repo batch."""
    container_name = _repo_container_name(project_name, commit_sha)
    cached_container = _active_repo_containers.get(container_name)
    if cached_container:
        return cached_container

    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        text=True,
        check=False,
    )
    result = subprocess.run(
        ["docker", "run", "-d", "--name", container_name, image_tag, "tail", "-f", "/dev/null"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise Exception(
            f"Failed to start persistent repo container for {project_name}:\n"
            f"{result.stderr}\n{result.stdout}"
        )

    _active_repo_containers[container_name] = container_name
    logger.info("Started persistent repo container %s for %s @ %s.", container_name, project_name, commit_sha)
    return container_name


def _docker_run(
    image_tag: str,
    container_name: str,
    cmd_list: list[str],
    timeout: int = 120,
    cpu_limit: Optional[str] = None,
    memory_limit: Optional[str] = None,
    env_vars: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """
    Run a command in a fresh, ephemeral container.
    The container auto-removes after execution.
    """
    full_cmd = ["docker", "run", "--rm", "--name", container_name]
    if cpu_limit:
        full_cmd.extend(["--cpus", cpu_limit])
    if memory_limit:
        full_cmd.extend(["--memory", memory_limit])
    if env_vars:
        for key, value in env_vars.items():
            full_cmd.extend(["-e", f"{key}={value}"])
    full_cmd.append(image_tag)
    full_cmd.extend(cmd_list)

    return subprocess.run(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout
    )


def _docker_exec(
    container_name: str,
    cmd_list: list[str],
    timeout: int = 120,
    env_vars: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run a command inside an existing persistent container."""
    full_cmd = ["docker", "exec"]
    if env_vars:
        for key, value in env_vars.items():
            full_cmd.extend(["-e", f"{key}={value}"])
    full_cmd.append(container_name)
    full_cmd.extend(cmd_list)
    return subprocess.run(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def _extract_pytest_count(log: Optional[str], label: str) -> int:
    """Best-effort parser for pytest summary counts in captured output."""
    if not log:
        return 0

    total = 0
    for match in re.finditer(rf"(\d+)\s+{label}\b", log):
        total += int(match.group(1))
    return total


def _derive_outcome_profile(
    pass_count: int,
    fail_count: int,
    error_message: Optional[str] = None,
) -> str:
    """Classify the observed execution profile before any LLM analysis."""
    if error_message:
        return "execution_error"
    if pass_count > 0 and fail_count > 0:
        return "mixed"
    if pass_count > 0:
        return "always_pass"
    if fail_count > 0:
        return "always_fail"
    return "unknown"


def _make_pytest_cmd(
    args: list[str],
    thread_stress: bool = False,
    enable_randomly: bool = False,
) -> list[str]:
    """
    Build the pytest command list with optional modifiers:
    - thread_stress: wraps pytest in a Python one-liner that sets an aggressive
      thread-switch interval to surface race conditions.
    """
    if not enable_randomly:
        args = ["-p", "no:randomly"] + args
    if thread_stress:
        args_repr = repr(args)
        return [
            "python", "-c",
            (
                "import sys; sys.setswitchinterval(0.000001); "
                f"from pytest import main; "
                f"raise SystemExit(main({args_repr}))"
            ),
        ]
    return ["pytest"] + args


def _attempt_single_test_run(
    repo_container_name: str,
    test_name: str,
    timeout_seconds: int,
) -> dict[str, object]:
    passing_log = None
    failing_log = None
    pass_count = 0
    fail_count = 0

    try:
        res = _docker_exec(
            repo_container_name,
            _make_pytest_cmd(["-v", "--tb=short", test_name]),
            timeout=timeout_seconds,
        )
        if res.returncode == 0:
            passing_log = res.stdout
            pass_count = 1
        else:
            failing_log = res.stdout
            fail_count = 1
    except subprocess.TimeoutExpired:
        failing_log = "Timeout Error on solo baseline run."
        fail_count = 1

    return {
        "name": "solo_baseline",
        "iterations_requested": 1,
        "iterations_executed": 1 if (pass_count or fail_count) else 0,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "outcome_profile": _derive_outcome_profile(pass_count, fail_count),
        "passing_log": passing_log,
        "failing_log": failing_log,
    }


def _attempt_repeated_in_process(
    repo_container_name: str,
    test_name: str,
    iterations: int,
    timeout_seconds: int,
) -> dict[str, object]:
    passing_log = None
    failing_log = None
    pass_count = 0
    fail_count = 0

    try:
        res = _docker_exec(
            repo_container_name,
            _make_pytest_cmd([f"--count={iterations}", "-v", test_name]),
            timeout=timeout_seconds * 2,
        )
        if res.returncode == 0:
            passing_log = res.stdout
            pass_count = iterations
        else:
            failing_log = res.stdout
            fail_count = _extract_pytest_count(res.stdout, "failed") or 1
    except subprocess.TimeoutExpired:
        failing_log = "Timeout Error on repeated in-process run."
        fail_count = 1

    return {
        "name": "repeated_in_process",
        "iterations_requested": iterations,
        "iterations_executed": iterations,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "outcome_profile": _derive_outcome_profile(pass_count, fail_count),
        "passing_log": passing_log,
        "failing_log": failing_log,
    }


def _attempt_isolated_reruns(
    repo_container_name: str,
    test_name: str,
    iterations: int,
    timeout_seconds: int,
) -> dict[str, object]:
    passing_log = None
    failing_log = None
    pass_count = 0
    fail_count = 0
    iterations_executed = 0

    for i in range(iterations):
        try:
            res = _docker_exec(
                repo_container_name,
                _make_pytest_cmd(["-v", "--tb=short", test_name]),
                timeout=timeout_seconds,
            )
            iterations_executed += 1
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
            iterations_executed += 1
            fail_count += 1
            if not failing_log:
                failing_log = f"Timeout Error on isolated rerun {i + 1}."
            break

    return {
        "name": "isolated_reruns",
        "iterations_requested": iterations,
        "iterations_executed": iterations_executed,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "outcome_profile": _derive_outcome_profile(pass_count, fail_count),
        "passing_log": passing_log,
        "failing_log": failing_log,
    }


def _attempt_randomized_file_context(
    repo_container_name: str,
    test_name: str,
    iterations: int,
    timeout_seconds: int,
) -> dict[str, object]:
    test_path = test_name.split("::")[0]
    passing_log = None
    failing_log = None
    pass_count = 0
    fail_count = 0
    iterations_executed = 0

    for i in range(iterations):
        try:
            res = _docker_exec(
                repo_container_name,
                _make_pytest_cmd(
                    [f"--randomly-seed={i + 1}", "-v", "--tb=short", test_path],
                    enable_randomly=True,
                ),
                timeout=timeout_seconds * 2,
            )
            iterations_executed += 1
            if res.returncode == 0:
                pass_count += 1
                if not passing_log:
                    passing_log = res.stdout
            else:
                stdout = res.stdout or ""
                fail_count += 1
                if not failing_log:
                    failing_log = stdout
            if passing_log and failing_log:
                break
        except subprocess.TimeoutExpired:
            iterations_executed += 1
            fail_count += 1
            if not failing_log:
                failing_log = f"Timeout Error on randomized file-context rerun {i + 1}."
            break

    return {
        "name": "randomized_file_context",
        "iterations_requested": iterations,
        "iterations_executed": iterations_executed,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "outcome_profile": _derive_outcome_profile(pass_count, fail_count),
        "passing_log": passing_log,
        "failing_log": failing_log,
    }


def _aggregate_attempts(
    attempts: list[dict[str, object]],
) -> Tuple[Optional[str], Optional[str], int, int, str]:
    final_passing_log = None
    final_failing_log = None
    total_pass_count = 0
    total_fail_count = 0

    for attempt in attempts:
        total_pass_count += int(attempt.get("pass_count", 0) or 0)
        total_fail_count += int(attempt.get("fail_count", 0) or 0)
        if not final_passing_log and attempt.get("passing_log"):
            final_passing_log = str(attempt["passing_log"])
        if not final_failing_log and attempt.get("failing_log"):
            final_failing_log = str(attempt["failing_log"])

    return (
        final_passing_log,
        final_failing_log,
        total_pass_count,
        total_fail_count,
        _derive_outcome_profile(total_pass_count, total_fail_count),
    )


def _execute_test_strategy(
    image_tag: str,
    project_name: str,
    commit_sha: str,
    test_name: str,
    category: str,
    iterations: int = 100,
    cpu_limit: Optional[str] = None,
    memory_limit: Optional[str] = None,
    thread_stress: bool = False,
    python_hash_seed: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], int, int, int, int]:
    """
    Legacy strategy helper retained for reference.

    The active second-phase rerun path is category-agnostic and does not call
    this function anymore.
    """
    passing_log = None
    failing_log = None
    pass_count = 0
    fail_count = 0
    iterations_requested = iterations
    iterations_executed = 0
    timeout_seconds = 120
    env_vars = {"PYTHONHASHSEED": python_hash_seed} if python_hash_seed else None

    container_base = f"flaky_run_{project_name.lower()}"
    repo_container_name = _ensure_repo_container(image_tag, project_name, commit_sha)

    if "NIO" in category:
        # NIO: run the same test N times in sequence within one process
        container_name = f"{container_base}_nio"
        try:
            logger.info("Running NIO strategy (--count=%d) for %s", iterations, test_name)
            res = _docker_exec(
                repo_container_name,
                _make_pytest_cmd([f"--count={iterations}", "-v", test_name], thread_stress=thread_stress),
                timeout=timeout_seconds * 2,
                env_vars=env_vars,
            )
            if res.returncode == 0:
                passing_log = res.stdout
                pass_count = iterations
                iterations_executed = iterations
                logger.info("NIO multi-run: all passed")
            else:
                failing_log = res.stdout
                fail_count = _extract_pytest_count(res.stdout, "failed")
                if fail_count == 0:
                    fail_count = 1
                iterations_executed = iterations
                logger.info("NIO multi-run: failure detected")
                # Get a clean single-run pass
                container_name_pass = f"{container_base}_nio_pass"
                try:
                    pass_res = _docker_exec(
                        repo_container_name,
                        _make_pytest_cmd(["-v", test_name], thread_stress=thread_stress),
                        timeout=timeout_seconds,
                        env_vars=env_vars,
                    )
                    if pass_res.returncode == 0:
                        passing_log = pass_res.stdout
                        pass_count = 1
                except (subprocess.TimeoutExpired, Exception):
                    pass
        except subprocess.TimeoutExpired:
            failing_log = "Timeout Error: NIO execution exceeded timeout."
            fail_count = 1

    elif "NOD" in category:
        # NOD: run the test many times, each in its own container
        for i in range(iterations):
            container_name = f"{container_base}_nod_{i}"
            try:
                res = _docker_exec(
                    repo_container_name,
                    _make_pytest_cmd(["-v", test_name], thread_stress=thread_stress),
                    timeout=timeout_seconds,
                    env_vars=env_vars,
                )
                if res.returncode == 0:
                    logger.info("NOD iteration %d: passed", i + 1)
                    pass_count += 1
                    if not passing_log:
                        passing_log = res.stdout
                else:
                    logger.info("NOD iteration %d: failed", i + 1)
                    fail_count += 1
                    if not failing_log:
                        failing_log = res.stdout

                if passing_log and failing_log:
                    logger.info("Captured both pass and fail for NOD. Stopping early.")
                    break
            except subprocess.TimeoutExpired:
                failing_log = f"Timeout Error on NOD iteration {i + 1}."
                fail_count += 1
                break

    elif "OD" in category:
        # OD strategy:
        # Step 1 — Run the target test ALONE to establish a passing baseline.
        #           An OD-Vic test should pass when run in isolation.
        # Step 2 — Run the FULL test directory with random ordering many times.
        #           The test should eventually fail when the "brittle" test runs before it.
        iterations_requested = iterations + 1
        test_dir = os.path.dirname(test_name.split("::")[0]) or "."

        # Step 1: solo run to get passing_log
        solo_container = f"{container_base}_od_solo"
        try:
            logger.info("OD solo run (baseline): %s", test_name)
            solo_res = _docker_exec(
                repo_container_name,
                _make_pytest_cmd(["-v", "--tb=short", test_name], thread_stress=thread_stress),
                timeout=timeout_seconds,
                env_vars=env_vars,
            )
            if solo_res.returncode == 0:
                passing_log = solo_res.stdout
                pass_count += 1
                iterations_executed += 1
                logger.info("OD solo run: PASSED (good baseline)")
            else:
                # Test fails even solo — likely a broken environment or wrong SHA
                failing_log = solo_res.stdout
                fail_count += 1
                iterations_executed += 1
                logger.warning(
                    "OD solo run: FAILED already — test may not be reproducible "
                    "in this Docker environment. Output:\n%s",
                    solo_res.stdout[-1000:]
                )
        except subprocess.TimeoutExpired:
            logger.warning("OD solo run timed out.")
            fail_count += 1
            iterations_executed += 1

        # Step 2: full-directory shuffled runs to catch ordering failure
        for i in range(iterations):
            container_name = f"{container_base}_od_{i}"
            try:
                res = _docker_exec(
                    repo_container_name,
                    ["pytest", f"--randomly-seed={i + 1}", "-v", "--tb=short", test_dir],
                    timeout=timeout_seconds * 3,
                    env_vars=env_vars,
                )
                if res.returncode == 0:
                    logger.info("OD iteration %d: all passed", i + 1)
                    pass_count += 1
                    iterations_executed += 1
                    if not passing_log:
                        passing_log = res.stdout
                else:
                    iterations_executed += 1
                    # Check if OUR specific test is the one that failed
                    test_id = test_name.split("::", 1)[-1] if "::" in test_name else test_name
                    if test_id in (res.stdout or "") and "FAILED" in (res.stdout or ""):
                        logger.info("OD iteration %d: target test FAILED (ordering issue found)", i + 1)
                        fail_count += 1
                        if not failing_log:
                            failing_log = res.stdout
                    else:
                        logger.info("OD iteration %d: other tests failed (not our target)", i + 1)

                if passing_log and failing_log:
                    logger.info("Captured both pass and fail for OD. Stopping early.")
                    break
            except subprocess.TimeoutExpired:
                failing_log = "Timeout Error on OD iteration."
                fail_count += 1
                iterations_executed += 1
                break

    else:
        # Generic fallback: run the test a few times
        iterations_requested = min(iterations, 5)
        for i in range(min(iterations, 5)):
            container_name = f"{container_base}_gen_{i}"
            try:
                res = _docker_exec(
                    repo_container_name,
                    _make_pytest_cmd(["-v", test_name], thread_stress=thread_stress),
                    timeout=timeout_seconds,
                    env_vars=env_vars,
                )
                if res.returncode == 0:
                    pass_count += 1
                    iterations_executed += 1
                    if not passing_log:
                        passing_log = res.stdout
                else:
                    fail_count += 1
                    iterations_executed += 1
                    if not failing_log:
                        failing_log = res.stdout
                if passing_log and failing_log:
                    break
            except subprocess.TimeoutExpired:
                failing_log = f"Timeout Error on generic iteration {i + 1}."
                fail_count += 1
                iterations_executed += 1
                break

    if "NIO" in category:
        if iterations_executed == 0 and failing_log:
            iterations_executed = 1
    elif "NOD" in category:
        iterations_executed = pass_count + fail_count

    return (
        passing_log,
        failing_log,
        pass_count,
        fail_count,
        iterations_requested,
        iterations_executed,
    )


def _run_profiled_test_strategy(
    image_tag: str,
    project_name: str,
    commit_sha: str,
    test_name: str,
    iterations: int = 100,
) -> Tuple[Optional[str], Optional[str], int, int, str, list[dict[str, object]]]:
    """
    Run a category-agnostic evidence collection protocol for every test.
    """
    repo_container_name = _ensure_repo_container(image_tag, project_name, commit_sha)
    timeout_seconds = 120

    attempts = [
        _attempt_single_test_run(repo_container_name, test_name, timeout_seconds),
        _attempt_repeated_in_process(repo_container_name, test_name, iterations, timeout_seconds),
        _attempt_isolated_reruns(repo_container_name, test_name, min(iterations, 25), timeout_seconds),
        _attempt_randomized_file_context(repo_container_name, test_name, min(iterations, 10), timeout_seconds),
    ]

    (
        final_passing_log,
        final_failing_log,
        final_pass_count,
        final_fail_count,
        final_outcome_profile,
    ) = _aggregate_attempts(attempts)

    return (
        final_passing_log,
        final_failing_log,
        final_pass_count,
        final_fail_count,
        final_outcome_profile,
        attempts,
    )


def _run_selected_profile(
    image_tag: str,
    project_name: str,
    commit_sha: str,
    test_name: str,
    iterations: int,
    profile_name: Optional[str],
    cpu_limit: Optional[str],
    memory_limit: Optional[str],
) -> Tuple[Optional[str], Optional[str], int, int, str, list[dict[str, object]]]:
    """
    Run the test once using baseline-only execution.
    """
    del profile_name, cpu_limit, memory_limit
    return _run_profiled_test_strategy(
        image_tag=image_tag,
        project_name=project_name,
        commit_sha=commit_sha,
        test_name=test_name,
        iterations=iterations,
    )


# ──────────────────────────────────────────────
# LangGraph Node Function
# ──────────────────────────────────────────────
def run_test_in_docker(state: RepairState) -> dict:
    """
    LangGraph node: Executes the flaky test inside Docker and captures logs.
    """
    project_url = state["project_url"]
    sha = state["sha_detected"]
    test_name = state["test_name"]

    project_name = project_url.rstrip("/").split("/")[-1]
    project_dir = os.path.join(WORKSPACE_DIR, project_name)

    logger.info(
        "=== Docker Runner: %s | test: %s | protocol: category-agnostic ===",
        project_name, test_name
    )

    error_message = None
    passing_log = None
    failing_log = None
    pass_count = 0
    fail_count = 0
    iterations_requested = 0
    iterations_executed = 0
    execution_profiles: list[dict[str, object]] = []
    is_reproduced = False
    selected_profile = state.get("selected_profile")
    cpu_limit = state.get("cpu_limit")
    memory_limit = state.get("memory_limit")

    try:
        # 0. Clone & checkout
        _clone_and_checkout(project_url, sha, project_dir)

        # 1. Build per-project Docker image (reused if already built)
        image_tag = _build_project_image(project_name, project_dir)

        # 2. Run category-agnostic evidence collection protocol
        if selected_profile or cpu_limit or memory_limit:
            logger.info(
                "Ignoring preselected stress profile for %s and running baseline only: profile=%s cpu=%s memory=%s",
                test_name,
                selected_profile or "custom",
                cpu_limit or "none",
                memory_limit or "none",
            )
            (
                passing_log,
                failing_log,
                pass_count,
                fail_count,
                _,
                execution_profiles,
            ) = _run_selected_profile(
                image_tag,
                project_name,
                sha,
                test_name,
                iterations=100,
                profile_name=selected_profile,
                cpu_limit=cpu_limit,
                memory_limit=memory_limit,
            )
        else:
            (
                passing_log,
                failing_log,
                pass_count,
                fail_count,
                _,
                execution_profiles,
            ) = _run_profiled_test_strategy(
                image_tag, project_name, sha, test_name
            )

        if execution_profiles:
            last_attempt = execution_profiles[-1]
            iterations_requested = int(last_attempt.get("iterations_requested", 0) or 0)
            iterations_executed = int(last_attempt.get("iterations_executed", 0) or 0)

        if passing_log and failing_log:
            is_reproduced = True
            logger.info("✅ Flakiness REPRODUCED for %s", test_name)
        else:
            logger.warning("⚠️  Could NOT reproduce flakiness for %s", test_name)

    except Exception as e:
        error_message = str(e)
        logger.error("Docker runner error: %s", error_message)

    outcome_profile = _derive_outcome_profile(pass_count, fail_count, error_message)

    return {
        "passing_log": passing_log,
        "failing_log": failing_log,
        "is_flakiness_reproduced": is_reproduced,
        "error_message": error_message,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "iterations_requested": iterations_requested,
        "iterations_executed": iterations_executed,
        "outcome_profile": outcome_profile,
        "execution_profiles": execution_profiles,
        "selected_profile": selected_profile,
        "cpu_limit": cpu_limit,
        "memory_limit": memory_limit,
        "trajectory": [{
            "agent": "DockerRunner",
            "action": "execute_test_in_docker",
            "project": project_name,
            "test": test_name,
            "protocol": "category_agnostic_second_phase",
            "reproduced": is_reproduced,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "iterations_requested": iterations_requested,
            "iterations_executed": iterations_executed,
            "outcome_profile": outcome_profile,
            "execution_profiles": execution_profiles,
            "error": error_message,
        }]
    }
