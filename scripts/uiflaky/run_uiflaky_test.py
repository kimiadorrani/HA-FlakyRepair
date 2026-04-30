import argparse
import csv
import json
import os
import subprocess
import threading
import fcntl
from pathlib import Path
from typing import Optional, Tuple, List, Dict

# Configuration
DEFAULT_NODE_IMAGE = "node:16-bullseye"
_DOCKER_LOCK = threading.Lock()
_ACTIVE_DOCKER_CONTAINERS = {}  # (base_dir, image, cwd) -> container_name

def _sanitize_container_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).lower()

def _docker_container_name(base_dir: Path, image: str) -> str:
    repo_part = _sanitize_container_name(base_dir.name)
    image_part = _sanitize_container_name(image.split(":")[0].split("/")[-1])
    return f"uiflaky_{repo_part}_{image_part}"

def ensure_persistent_docker_container(base_dir: Path, image: str) -> str:
    key = (str(base_dir), image, os.getcwd())
    with _DOCKER_LOCK:
        cached = _ACTIVE_DOCKER_CONTAINERS.get(key)
        if cached:
            return cached

        # Ensure image is present
        print(f"Pulling image {image}...")
        subprocess.run(["docker", "pull", image], capture_output=True)

        container_name = _docker_container_name(base_dir, image)
        # Force remove if exists
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        
        result = subprocess.run(
            [
                "docker", "run", "-d", "--name", container_name,
                "-v", f"{base_dir.resolve()}:/workspace",
                "-w", "/workspace",
                image,
                "tail", "-f", "/dev/null",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"Docker Error: {result.stderr}")
            raise SystemExit(result.stdout.strip() or f"Failed to start Docker container from {image}")

        # Install build tools and package managers
        print(f"Installing build tools and package managers in container {container_name}...")
        # Use npm -g for pnpm as it handles symlinks to /usr/local/bin better
        setup_cmd = (
            "apt-get update && apt-get install -y python2 python3 python-is-python3 make g++ libpng-dev libjpeg-dev libgif-dev curl && "
            "npm install -g yarn || true"
        )
        setup_result = subprocess.run(
            ["docker", "exec", container_name, "bash", "-c", setup_cmd],
            capture_output=True,
            text=True
        )
        if setup_result.returncode != 0:
            print(f"Warning: Setup command in container {container_name} failed: {setup_result.stderr}")

        _ACTIVE_DOCKER_CONTAINERS[key] = container_name
        return container_name

def cleanup_all_for_repo(repo_dir: Path) -> None:
    repo_dir_str = str(repo_dir)
    with _DOCKER_LOCK:
        to_remove = [k for k in _ACTIVE_DOCKER_CONTAINERS.keys() if k[0] == repo_dir_str]
    for k in to_remove:
        with _DOCKER_LOCK:
            container_name = _ACTIVE_DOCKER_CONTAINERS.pop(k, "")
        if container_name:
            print(f"Cleaning up Docker container: {container_name}")
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

def prepare_repo(row: dict, workspaces_dir: Path) -> tuple[Path, bool]:
    project_name = row['Project']
    repo_dir = workspaces_dir / project_name.replace('/', '__')
    commit_sha = row['Commit SHA']

    if not repo_dir.exists():
        raise FileNotFoundError(f"Repository {project_name} not found in {workspaces_dir}")
    repo_dir = repo_dir.resolve()

    # For UI-FLAKY, the commit is the FIX. We want the state BEFORE the fix.
    checkout = f"{commit_sha}^" if commit_sha else "HEAD"
    checkout_success = False

    if commit_sha:
        # Step 1: fetch the fix commit itself if missing
        sha_missing = subprocess.run(
            ["git", "cat-file", "-e", commit_sha], cwd=repo_dir, capture_output=True
        ).returncode != 0
        if sha_missing:
            subprocess.run(["git", "fetch", "origin", commit_sha], cwd=repo_dir, capture_output=True)

        # Step 2: check if the parent (sha^) is available — fetch just sha + its parent if not
        parent_missing = subprocess.run(
            ["git", "cat-file", "-e", f"{commit_sha}^"], cwd=repo_dir, capture_output=True
        ).returncode != 0

        if parent_missing:
            print(f"  Fetching {commit_sha[:7]} + parent from origin (depth 2)...")
            subprocess.run(
                ["git", "fetch", "origin", "--depth", "2", commit_sha],
                cwd=repo_dir, capture_output=True
            )

    result = subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, capture_output=True, text=True)
    if result.returncode == 0:
        checkout_success = True
    else:
        print(f"  Warning: failed to checkout {checkout} — running at current HEAD instead.")

    actual_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True
    ).stdout.strip()
    print(f"  Checked out: {actual_sha[:12]} (wanted {checkout})")

    subprocess.run(["git", "clean", "-fdx"], cwd=repo_dir, capture_output=True)
    return repo_dir, checkout_success

def detect_node_image(repo_dir: Path) -> str:
    import re as _re
    package_json = repo_dir / "package.json"
    nvmrc = repo_dir / ".nvmrc"
    node_version_file = repo_dir / ".node-version"

    raw_version = ""
    # Prefer explicit version files
    for vf in [nvmrc, node_version_file]:
        if vf.exists():
            raw_version = vf.read_text().strip().lstrip("v")
            break

    if not raw_version and package_json.exists():
        try:
            engines = json.loads(package_json.read_text()).get("engines", {})
            raw_version = str(engines.get("node", ""))
        except Exception:
            pass

    # Extract leading major version number
    m = _re.search(r"(\d+)", raw_version)
    major = int(m.group(1)) if m else 0

    if major >= 22: return "node:22-bookworm"
    if major >= 20: return "node:20-bullseye"
    if major >= 18: return "node:18-bullseye"
    if major == 16: return "node:16-bullseye"
    if major == 14: return "node:14-bullseye"
    if major == 12: return "node:12-buster"
    if major == 10: return "node:10-buster"

    # Fallback: newer repos with yarn/pnpm get node:18, older get node:16
    if (repo_dir / "pnpm-lock.yaml").exists():
        return "node:18-bullseye"
    if (repo_dir / "yarn.lock").exists():
        return "node:16-bullseye"
    return "node:16-bullseye"

def build_install_command(repo_dir: Path, container_name: str) -> list[str]:
    install_cmd = "yarn install --ignore-engines --non-interactive"
    try:
        data = json.loads((repo_dir / "package.json").read_text())
        scripts = data.get("scripts", {})
        has_workspaces = bool(data.get("workspaces"))
        has_lerna = (repo_dir / "lerna.json").exists()

        if has_workspaces or has_lerna:
            if scripts.get("build"):
                install_cmd += " && yarn build"
            elif has_lerna:
                # Build each workspace package's CJS output (faster than full build)
                install_cmd += " && npx lerna run build:cjs --ignore-scripts || npx lerna run build || true"
        elif scripts.get("build"):
            install_cmd += " && yarn build"
    except Exception:
        pass
    return ["docker", "exec", container_name, "bash", "-lc", install_cmd]

def build_test_command(row: dict, repo_dir: Path, container_name: str) -> list[str]:
    test_files = row['Test Files'].split(';')
    test_files = [tf.strip() for tf in test_files if tf.strip()]
    test_files_str = " ".join(test_files)
    
    # Prefer local binaries over npx so that project-pinned runner versions
    # (and their config file format, e.g. mocha's test/mocha.opts) are used.
    inner_cmd = f'''
export NODE_ENV=test
export BABEL_ENV=test
MOCHA_BIN=$([ -f node_modules/.bin/mocha ] && echo node_modules/.bin/mocha || echo npx mocha)
JEST_BIN=$([ -f node_modules/.bin/jest ] && echo node_modules/.bin/jest || echo npx jest)
if grep -qi '"jest"' package.json; then
    $JEST_BIN {test_files_str}
elif grep -qi '"mocha"' package.json; then
    $MOCHA_BIN {test_files_str}
elif grep -qi '"cypress"' package.json; then
    npx cypress run --spec {test_files_str}
elif grep -qi '"karma"' package.json; then
    node_modules/.bin/karma start 2>/dev/null || npx karma start
else
    yarn test -- {test_files_str}
fi
'''
    return ["docker", "exec", container_name, "bash", "-lc", inner_cmd]

def update_results_csv(row: dict, result: dict):
    results_csv_path = Path("datasets/uiflaky/uiflaky-reproduction-results.csv")
    fieldnames = [
        'Project', 'Project URL', 'Category', 'Commit SHA', 'Checkout SHA',
        'Checkout OK', 'Test Files', 'Title', 'Iterations Requested', 'Iterations Executed',
        'Pass Count', 'Fail Count', 'Reproduced', 'Status', 'Error', 'Result JSON'
    ]
    
    # Process-safe append using fcntl
    with open(results_csv_path, 'a', encoding='utf-8', newline='') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        
        # Check if file is empty to write header
        f.seek(0, 2) # Move to end
        if f.tell() == 0:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        output_row = {
            'Project': row.get('Project', ''),
            'Project URL': row.get('Project URL', ''),
            'Category': row.get('Category', ''),
            'Commit SHA': row.get('Commit SHA', ''),
            'Checkout SHA': f"{row.get('Commit SHA', '')}^" if row.get('Commit SHA') else 'HEAD',
            'Checkout OK': str(result.get('checkout_ok', '')).lower(),
            'Test Files': row.get('Test Files', ''),
            'Title': row.get('Title', ''),
            'Iterations Requested': result.get('iterations_requested', ''),
            'Iterations Executed': result.get('iterations_executed', ''),
            'Pass Count': result.get('pass_count', ''),
            'Fail Count': result.get('fail_count', ''),
            'Reproduced': str(result.get('reproduced', '')).lower() if 'reproduced' in result else '',
            'Status': 'reproduced' if result.get('reproduced') else ('always_fail' if result.get('fail_count', 0) > 0 else 'always_pass') if 'fail_count' in result else 'error',
            'Error': result.get('error', ''),
            'Result JSON': result.get('result_json', '')
        }
        writer.writerow(output_row)
        fcntl.flock(f, fcntl.LOCK_UN)

def run_repeated(cmd: list[str], work_dir: Path, iterations: int, row: dict, results_dir: Path, checkout_ok: bool = False) -> dict:
    pass_count = 0
    fail_count = 0
    first_pass_log = ""
    first_fail_log = ""
    project_name = row['Project']

    for i in range(iterations):
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=1200, # UI tests can be slow
            check=False,
        )
        if result.returncode == 0:
            pass_count += 1
            if not first_pass_log: first_pass_log = result.stdout
        else:
            fail_count += 1
            if not first_fail_log: first_fail_log = result.stdout
            
        status = "PASS" if result.returncode == 0 else "FAIL"
        print(f"[{project_name}] [{i+1}/{iterations}] {status}")
        
        # Stop early if we have both PASS and FAIL (reproduced!)
        if pass_count > 0 and fail_count > 0:
            print(f"[{project_name}] Reproduced flakiness in {i+1} iterations!")
            break

    res_dict = {
        "iterations_requested": iterations,
        "iterations_executed": pass_count + fail_count,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "reproduced": pass_count > 0 and fail_count > 0,
        "checkout_ok": checkout_ok,
        "first_pass_log": first_pass_log,
        "first_fail_log": first_fail_log,
    }
    
    # Save JSON
    safe_title = _sanitize_container_name(row['Title'][:50])
    output_path = results_dir / f"{row['Project'].replace('/', '__')}-{safe_title}.json"
    res_dict['result_json'] = str(output_path)
    with output_path.open('w', encoding='utf-8') as f:
        json.dump(res_dict, f, indent=2)
        
    update_results_csv(row, res_dict)
    return res_dict

def execute_row(row_index: int, row: dict, workspaces_dir: Path, results_dir: Path, iterations: int):
    try:
        repo_dir, checkout_ok = prepare_repo(row, workspaces_dir)
        image = detect_node_image(repo_dir)
        container_name = ensure_persistent_docker_container(repo_dir, image)

        print(f"[{row['Project']}] Running install...")
        install_cmd = build_install_command(repo_dir, container_name)
        subprocess.run(install_cmd, cwd=repo_dir, check=False)

        cmd = build_test_command(row, repo_dir, container_name)

        print(f"Running UI-FLAKY test for {row['Project']} :: {row['Title']}")
        results_dir.mkdir(parents=True, exist_ok=True)
        return run_repeated(cmd, repo_dir, iterations, row, results_dir, checkout_ok=checkout_ok)

    except Exception as e:
        print(f"Error executing row: {e}")
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
    parser.add_argument("--iterations", type=int, default=10)
    args = parser.parse_args()

    metadata_path = Path("datasets/uiflaky/preprocessed/uiflaky-metadata.csv")
    workspaces_dir = Path("workspaces/uiflaky")
    results_dir = Path("results/uiflaky")

    with open(metadata_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        if 0 <= args.row_index < len(rows):
            execute_row(args.row_index, rows[args.row_index], workspaces_dir, results_dir, args.iterations)
        else:
            print(f"Invalid row index: {args.row_index}")
