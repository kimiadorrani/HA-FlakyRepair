"""
Docker Infrastructure — low-level helpers shared by all tool factories.

Handles:
  - Git clone and SHA checkout
  - Docker image build (one image per project, cached)
  - Container start / stop / cleanup (one persistent container per project+SHA)
  - Raw docker-exec wrapper
  - pytest command builder
"""

import logging
import os
import re
import subprocess
import threading

logger = logging.getLogger(__name__)

WORKSPACE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "workspaces", "idoft"
)

# Module-level caches (survive across tool factory calls in the same process)
_built_images: set[str] = set()
_prepared_checkouts: dict[str, str] = {}
_active_repo_containers: dict[str, str] = {}
_docker_lock = threading.Lock()


# ── Image / container naming ───────────────────────────────────────────────

def _image_tag(project_name: str) -> str:
    return f"flaky-{project_name.lower()}:latest"


def _repo_container_name(project_name: str, commit_sha: str, instance: str = "") -> str:
    safe_project = re.sub(r"[^a-z0-9_.-]+", "-", project_name.lower())
    safe_sha = re.sub(r"[^a-z0-9]+", "", commit_sha.lower())[:12]
    name = f"flaky_repo_{safe_project}_{safe_sha or 'head'}"
    # An optional instance suffix lets several containers run off the same
    # (project, SHA) image in parallel — used when repairing sibling tests of one
    # project concurrently. Default "" keeps the original single-container name.
    if instance:
        safe_inst = re.sub(r"[^a-z0-9]+", "", instance.lower())[:16]
        name = f"{name}_{safe_inst}"
    return name


# ── Git helpers ────────────────────────────────────────────────────────────

def _clone_and_checkout(repo_url: str, commit_sha: str, project_dir: str) -> None:
    with _docker_lock:
        already_done = _prepared_checkouts.get(project_dir) == commit_sha
        if not os.path.exists(project_dir):
            already_done = False

    if not os.path.exists(project_dir):
        logger.info("Cloning %s …", repo_url)
        subprocess.run(
            ["git", "clone", "--depth", "100", repo_url, project_dir],
            check=True, capture_output=True,
        )

    if already_done:
        return

    sha_missing = subprocess.run(
        ["git", "cat-file", "-e", commit_sha], cwd=project_dir, capture_output=True
    ).returncode != 0

    if sha_missing:
        subprocess.run(["git", "fetch", "origin", commit_sha],
                       cwd=project_dir, capture_output=True)
        if subprocess.run(["git", "cat-file", "-e", commit_sha],
                          cwd=project_dir, capture_output=True).returncode != 0:
            subprocess.run(["git", "fetch", "origin", "--depth", "50", "--update-shallow"],
                           cwd=project_dir, capture_output=True)
        if subprocess.run(["git", "cat-file", "-e", commit_sha],
                          cwd=project_dir, capture_output=True).returncode != 0:
            subprocess.run(["git", "fetch", "origin", "--unshallow"],
                           cwd=project_dir, capture_output=True)

    subprocess.run(["git", "checkout", "-f", commit_sha],
                   cwd=project_dir, check=True, capture_output=True)
    subprocess.run(["git", "clean", "-fdx"], cwd=project_dir, capture_output=True)
    with _docker_lock:
        _prepared_checkouts[project_dir] = commit_sha


# ── Image build ────────────────────────────────────────────────────────────

def _build_project_image(project_name: str, project_dir: str) -> str:
    tag = _image_tag(project_name)
    with _docker_lock:
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
            cwd=project_dir, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker build failed for {project_name}:\n{result.stderr}\n{result.stdout}"
            )
        with _docker_lock:
            _built_images.add(tag)
        logger.info("Image %s built.", tag)
    finally:
        if os.path.exists(dockerfile_path):
            os.remove(dockerfile_path)
    return tag


# ── Container lifecycle ────────────────────────────────────────────────────

def _ensure_repo_container(image_tag: str, project_name: str, commit_sha: str,
                           instance: str = "") -> str:
    container_name = _repo_container_name(project_name, commit_sha, instance)
    with _docker_lock:
        if container_name in _active_repo_containers:
            return container_name

    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)
    result = subprocess.run(
        ["docker", "run", "-d", "--name", container_name, image_tag, "tail", "-f", "/dev/null"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to start container for {project_name}:\n{result.stderr}")
    with _docker_lock:
        _active_repo_containers[container_name] = container_name
    logger.info("Started container %s for %s @ %s.", container_name, project_name, commit_sha)
    return container_name


def reset_container_files(project_name: str, commit_sha: str) -> None:
    """Revert all in-container file changes via git checkout -- . (keeps container running)."""
    container_name = _repo_container_name(project_name, commit_sha)
    if container_name not in _active_repo_containers:
        return
    result = subprocess.run(
        ["docker", "exec", "-w", "/app", container_name, "git", "checkout", "--", "."],
        capture_output=True,
    )
    if result.returncode == 0:
        logger.info("Container %s file state reset.", container_name)
    else:
        logger.warning("Could not reset container files: %s", result.stderr.decode()[:200])


def cleanup_project_container(project_name: str, sha: str, instance: str = "") -> None:
    """Remove the running container for this project+SHA. The image is kept for reuse."""
    container_name = _repo_container_name(project_name, sha, instance)
    result = subprocess.run(
        ["docker", "rm", "-f", container_name], capture_output=True, check=False
    )
    with _docker_lock:
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


# ── Exec / command helpers ─────────────────────────────────────────────────

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
