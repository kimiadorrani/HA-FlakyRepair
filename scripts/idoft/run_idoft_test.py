import argparse
import csv
import fcntl
import json
import os
import re
import subprocess
import threading
from pathlib import Path

DEFAULT_PYTHON_IMAGE = "python:3.9-slim"
_DOCKER_LOCK = threading.Lock()
_ACTIVE_DOCKER_CONTAINERS = {}  # (repo_dir_str, image) -> container_name

TEST_NAME_COL = (
    "Pytest Test Name "
    "(PathToFile::TestClass::TestMethod or PathToFile::TestMethod)"
)


def _sanitize_container_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).lower()


def _docker_container_name(base_dir: Path, image: str) -> str:
    repo_part = _sanitize_container_name(base_dir.name)
    image_part = _sanitize_container_name(image.split(":")[0].split("/")[-1])
    return f"idoft_{repo_part}_{image_part}"


def ensure_persistent_docker_container(base_dir: Path, image: str) -> str:
    key = (str(base_dir), image)
    with _DOCKER_LOCK:
        cached = _ACTIVE_DOCKER_CONTAINERS.get(key)
        if cached:
            return cached

        print(f"Pulling image {image}...")
        subprocess.run(["docker", "pull", image], capture_output=True)

        container_name = _docker_container_name(base_dir, image)
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

        result = subprocess.run(
            [
                "docker", "run", "-d", "--name", container_name,
                "-v", f"{base_dir.resolve()}:/workspace",
                "-w", "/workspace",
                image,
                "tail", "-f", "/dev/null",
            ],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to start Docker container from {image}: {result.stderr}"
            )

        print(f"Installing system build tools in {container_name}...")
        setup_cmd = (
            "apt-get update -qq && "
            "apt-get install -y -qq git build-essential curl libssl-dev "
            "libffi-dev python3-dev 2>/dev/null | tail -1"
        )
        subprocess.run(
            ["docker", "exec", container_name, "bash", "-c", setup_cmd],
            capture_output=True, text=True,
        )

        _ACTIVE_DOCKER_CONTAINERS[key] = container_name
        return container_name


def cleanup_container(repo_dir: Path) -> None:
    repo_dir_str = str(repo_dir)
    with _DOCKER_LOCK:
        to_remove = [k for k in _ACTIVE_DOCKER_CONTAINERS if k[0] == repo_dir_str]
    for k in to_remove:
        with _DOCKER_LOCK:
            name = _ACTIVE_DOCKER_CONTAINERS.pop(k, "")
        if name:
            print(f"Removing Docker container: {name}")
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def prepare_repo(row: dict, workspaces_dir: Path) -> tuple[Path, bool]:
    project_name = row["Project URL"].rstrip("/").split("/")[-1]
    repo_dir = workspaces_dir / project_name
    commit_sha = row.get("SHA Detected", "").strip()

    if not repo_dir.exists():
        raise FileNotFoundError(
            f"Repository {project_name} not found in {workspaces_dir}. "
            "Clone it first with src.data.idoft.preprocess.fetch_workspaces."
        )
    repo_dir = repo_dir.resolve()

    checkout = commit_sha if commit_sha else "HEAD"
    checkout_success = False

    if commit_sha:
        sha_missing = subprocess.run(
            ["git", "cat-file", "-e", commit_sha], cwd=repo_dir, capture_output=True
        ).returncode != 0
        if sha_missing:
            print(f"  SHA {commit_sha[:7]} missing locally, fetching from origin...")
            subprocess.run(
                ["git", "fetch", "origin", commit_sha],
                cwd=repo_dir, capture_output=True,
            )
            # For shallow repos: fetch with depth to get the commit + history
            still_missing = subprocess.run(
                ["git", "cat-file", "-e", commit_sha], cwd=repo_dir, capture_output=True
            ).returncode != 0
            if still_missing:
                subprocess.run(
                    ["git", "fetch", "origin", "--depth", "50", "--update-shallow"],
                    cwd=repo_dir, capture_output=True,
                )

    result = subprocess.run(
        ["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True
    )
    if result.returncode == 0:
        checkout_success = True
    else:
        print(f"  Warning: failed to checkout {checkout} — using current HEAD.")

    actual_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True
    ).stdout.strip()
    print(f"  Checked out: {actual_sha[:12]} (wanted {checkout})")

    subprocess.run(["git", "clean", "-fdx"], cwd=repo_dir, capture_output=True)
    return repo_dir, checkout_success


def detect_python_image(repo_dir: Path) -> str:
    raw_version = ""

    for vf in [repo_dir / ".python-version", repo_dir / "runtime.txt"]:
        if vf.exists():
            raw_version = vf.read_text().strip().lstrip("python-").lstrip("v")
            break

    if not raw_version:
        for cfg in [repo_dir / "setup.cfg", repo_dir / "pyproject.toml"]:
            if cfg.exists():
                text = cfg.read_text()
                m = re.search(r"python_requires\s*[=><!~]+\s*['\"]?(\d+\.\d+)", text)
                if not m:
                    m = re.search(r"requires-python\s*=\s*['\"]?[>=!~]*\s*(\d+\.\d+)", text)
                if m:
                    raw_version = m.group(1)
                    break

    m = re.match(r"(\d+)\.?(\d*)", raw_version)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2)) if m.group(2) else 0
        if major == 3:
            if minor >= 12: return "python:3.12-slim"
            if minor >= 11: return "python:3.11-slim"
            if minor >= 10: return "python:3.10-slim"
            return "python:3.9-slim"  # 3.9 for <=3.9: still has collections.MutableMapping, supports modern packages

    return DEFAULT_PYTHON_IMAGE  # python:3.8-slim


def build_install_command(repo_dir: Path, container_name: str) -> list[str]:
    cmds = ["pip install --upgrade pip setuptools wheel -q"]

    # Install any requirements files we find
    for req in ["requirements.txt", "requirements-dev.txt", "requirements-test.txt",
                 "test-requirements.txt", "dev-requirements.txt"]:
        if (repo_dir / req).exists():
            cmds.append(f"pip install -r {req} --ignore-requires-python -q || true")

    # Install the package itself
    if (repo_dir / "setup.py").exists():
        cmds.append("pip install -e . --ignore-requires-python -q || pip install . -q || true")
    elif (repo_dir / "pyproject.toml").exists():
        cmds.append("pip install -e . -q || pip install . -q || true")

    # Force-reinstall pinned test infrastructure AFTER project deps so that anything
    # the project requirements pulled in (newer coverage, iniconfig, etc.) gets
    # downgraded to versions that work on Python 3.9.
    # coverage>=7.6 uses Python 3.10 match syntax; iniconfig>=2.0 uses str|None (3.10+).
    cmds.append(
        "pip install "
        "'pytest<8' 'iniconfig<2.0' 'coverage<7.6' 'pytest-cov<5' "
        "'pytest-mock<4' pytest-randomly "
        "--force-reinstall -q 2>/dev/null || true"
    )

    install_cmd = " && ".join(cmds)
    return ["docker", "exec", container_name, "bash", "-c", install_cmd]


def _pytest_cmd(container_name: str, test_name: str, count: int | None = None) -> list[str]:
    flags = f"--count={count} " if count else ""
    return ["docker", "exec", container_name, "bash", "-c",
            f"pytest -v --tb=short {flags}{test_name}"]


def update_results_csv(row: dict, result: dict) -> None:
    results_csv_path = Path("datasets/idoft/idoft-reproduction-results.csv")
    fieldnames = [
        "Project", "Project URL", "Category", "SHA Detected",
        "Checkout OK", "Test Name",
        "Iterations Requested", "Iterations Executed",
        "Pass Count", "Fail Count", "Reproduced", "Status", "Error", "Result JSON",
    ]

    project_url = row.get("Project URL", "")
    project_name = project_url.rstrip("/").split("/")[-1]
    test_name = row.get(TEST_NAME_COL, "")
    key = (project_url.strip(), test_name.strip())

    fail_c = result.get("fail_count", 0)
    pass_c = result.get("pass_count", 0)
    reproduced = result.get("reproduced", False)

    if "error" in result and result["error"]:
        status = "error"
    elif reproduced:
        status = "reproduced"
    elif fail_c > 0 and pass_c == 0:
        status = "always_fail"
    elif pass_c > 0 and fail_c == 0:
        status = "always_pass"
    else:
        status = "unknown"

    output_row = {
        "Project": project_name,
        "Project URL": project_url,
        "Category": row.get("Category", ""),
        "SHA Detected": row.get("SHA Detected", ""),
        "Checkout OK": str(result.get("checkout_ok", False)).lower(),
        "Test Name": test_name,
        "Iterations Requested": result.get("iterations_requested", ""),
        "Iterations Executed": result.get("iterations_executed", ""),
        "Pass Count": result.get("pass_count", ""),
        "Fail Count": result.get("fail_count", ""),
        "Reproduced": str(reproduced).lower() if "reproduced" in result else "",
        "Status": status,
        "Error": result.get("error", ""),
        "Result JSON": result.get("result_json", ""),
    }

    # Use an exclusive lock for the full read-modify-write so concurrent workers
    # don't race against each other.
    with open(results_csv_path, "a+", encoding="utf-8", newline="") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        content = f.read()

        if not content.strip():
            existing_rows = []
        else:
            existing_rows = list(csv.DictReader(content.splitlines()))

        # Upsert: replace existing row with same key, or append
        replaced = False
        new_rows = []
        for existing in existing_rows:
            if (existing.get("Project URL", "").strip(), existing.get("Test Name", "").strip()) == key:
                new_rows.append(output_row)
                replaced = True
            else:
                new_rows.append(existing)
        if not replaced:
            new_rows.append(output_row)

        f.seek(0)
        f.truncate()
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(new_rows)
        fcntl.flock(f, fcntl.LOCK_UN)


def _run_subprocess(cmd: list[str], work_dir: Path) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, cwd=work_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=300, check=False,
        )
        return r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        return 1, "Timeout"


def run_nio(
    container_name: str,
    work_dir: Path,
    iterations: int,
    row: dict,
    results_dir: Path,
    checkout_ok: bool = False,
) -> dict:
    """NIO strategy: run test N times in the same process, then once solo for a pass baseline."""
    test_name = row[TEST_NAME_COL].strip()
    project_name = row["Project URL"].rstrip("/").split("/")[-1]

    # Step 1: repeated in-process run
    batch_rc, batch_log = _run_subprocess(
        _pytest_cmd(container_name, test_name, count=iterations), work_dir
    )

    fail_count = 0
    pass_count = 0
    first_fail_log = ""
    first_pass_log = ""

    if batch_rc != 0:
        # Some iterations failed — test is flaky (or always fails in-process)
        fail_count = max(1, batch_log.count(" FAILED"))
        first_fail_log = batch_log
        print(f"[{project_name}] in-process batch: {fail_count} failures detected")

        # Step 2: solo run to get a passing baseline
        solo_rc, solo_log = _run_subprocess(
            _pytest_cmd(container_name, test_name), work_dir
        )
        if solo_rc == 0:
            pass_count = 1
            first_pass_log = solo_log
            print(f"[{project_name}] solo run: PASS → flakiness reproduced!")
        else:
            print(f"[{project_name}] solo run: also FAIL (always_fail, not flaky)")
    else:
        pass_count = iterations
        first_pass_log = batch_log
        print(f"[{project_name}] in-process batch: all {iterations} passed (always_pass)")

    return _save_result(
        row, results_dir, iterations, iterations, pass_count, fail_count,
        first_pass_log, first_fail_log, checkout_ok,
    )


def _save_result(
    row: dict,
    results_dir: Path,
    iterations_requested: int,
    iterations_executed: int,
    pass_count: int,
    fail_count: int,
    first_pass_log: str,
    first_fail_log: str,
    checkout_ok: bool,
) -> dict:
    project_name = row["Project URL"].rstrip("/").split("/")[-1]
    results_dir.mkdir(parents=True, exist_ok=True)
    safe_test = _sanitize_container_name(row[TEST_NAME_COL][:60])
    output_path = results_dir / f"{project_name}-{safe_test}.json"

    res_dict = {
        "iterations_requested": iterations_requested,
        "iterations_executed": iterations_executed,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "reproduced": pass_count > 0 and fail_count > 0,
        "checkout_ok": checkout_ok,
        "first_pass_log": first_pass_log,
        "first_fail_log": first_fail_log,
        "result_json": str(output_path),
    }

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(res_dict, f, indent=2)

    update_results_csv(row, res_dict)
    return res_dict


def run_repeated(
    cmd: list[str],
    work_dir: Path,
    iterations: int,
    row: dict,
    results_dir: Path,
    checkout_ok: bool = False,
) -> dict:
    pass_count = 0
    fail_count = 0
    first_pass_log = ""
    first_fail_log = ""
    project_name = row["Project URL"].rstrip("/").split("/")[-1]

    for i in range(iterations):
        rc, out = _run_subprocess(cmd, work_dir)
        if rc == 0:
            pass_count += 1
            if not first_pass_log:
                first_pass_log = out
        else:
            fail_count += 1
            if not first_fail_log:
                first_fail_log = out
            if out == "Timeout":
                print(f"[{project_name}] [{i+1}/{iterations}] TIMEOUT")
                break

        status = "PASS" if rc == 0 else "FAIL"
        print(f"[{project_name}] [{i+1}/{iterations}] {status}")

        if pass_count > 0 and fail_count > 0:
            print(f"[{project_name}] Reproduced flakiness in {i+1} iterations!")
            break

    return _save_result(
        row, results_dir, iterations, pass_count + fail_count,
        pass_count, fail_count, first_pass_log, first_fail_log, checkout_ok,
    )


def execute_row(
    row_index: int,
    row: dict,
    workspaces_dir: Path,
    results_dir: Path,
    iterations: int,
) -> dict:
    project_name = row["Project URL"].rstrip("/").split("/")[-1]
    category = row.get("Category", "").upper()
    try:
        repo_dir, checkout_ok = prepare_repo(row, workspaces_dir)
        image = detect_python_image(repo_dir)
        container_name = ensure_persistent_docker_container(repo_dir, image)

        print(f"[{project_name}] Installing dependencies...")
        install_cmd = build_install_command(repo_dir, container_name)
        install_result = subprocess.run(
            install_cmd, cwd=repo_dir, capture_output=True, text=True, timeout=360
        )
        if install_result.returncode != 0:
            print(f"[{project_name}] Warning: install had errors:\n{install_result.stdout[-500:]}")

        print(f"Running IDOFT test [{row_index}] ({category}) {project_name} :: {row[TEST_NAME_COL]}")

        if "NIO" in category:
            # NIO: in-process repeated run + solo baseline
            return run_nio(container_name, repo_dir, iterations, row, results_dir, checkout_ok=checkout_ok)
        else:
            # NOD / generic: independent reruns
            cmd = _pytest_cmd(container_name, row[TEST_NAME_COL].strip())
            return run_repeated(cmd, repo_dir, iterations, row, results_dir, checkout_ok=checkout_ok)

    except Exception as e:
        print(f"[{project_name}] Error: {e}")
        error_result = {
            "error": str(e),
            "fail_count": 0,
            "pass_count": 0,
            "iterations_executed": 0,
            "reproduced": False,
            "iterations_requested": iterations,
            "checkout_ok": False,
        }
        update_results_csv(row, error_result)
        return error_result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--row-index", type=int, required=True)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument(
        "--input-csv",
        default="datasets/idoft/raw/py-data.csv",
        help="Path to the input CSV (raw or preprocessed)",
    )
    parser.add_argument("--skip-od", action="store_true", default=True)
    args = parser.parse_args()

    metadata_path = Path(args.input_csv)
    workspaces_dir = Path("workspaces/idoft")
    results_dir = Path("results/idoft")

    with open(metadata_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [
            r for r in reader
            if not (args.skip_od and r.get("Category", "").upper().startswith("OD"))
        ]

    if 0 <= args.row_index < len(rows):
        execute_row(args.row_index, rows[args.row_index], workspaces_dir, results_dir, args.iterations)
    else:
        print(f"Invalid row index: {args.row_index} (max {len(rows)-1})")
