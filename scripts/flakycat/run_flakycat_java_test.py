#!/usr/bin/env python3
"""
Run one FLAKYCAT Java test repeatedly from the exported metadata CSV.

The script intentionally uses `workspaces/flakycat/` so Java project checkouts
do not mix with IDOFT Python workspaces.

Examples:
  .venv/bin/python scripts/flakycat/run_flakycat_java_test.py --project fastjson --limit 20
  .venv/bin/python scripts/flakycat/run_flakycat_java_test.py --row-index 0 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_CSV = ROOT_DIR / "datasets" / "flakycat" / "flakycat-java-tests.csv"
DEFAULT_WORKSPACES_DIR = ROOT_DIR / "workspaces" / "flakycat"
DEFAULT_RESULTS_DIR = ROOT_DIR / "results" / "flakycat"
ORDER_DEPENDENT_CATEGORIES = {"od-vic", "od", "test order dependency"}
DEFAULT_MAVEN_IMAGE = "maven:3.9-eclipse-temurin-8"
DEFAULT_GRADLE_IMAGE = "gradle:8.5-jdk8"
DEFAULT_JAVA_IMAGE = "eclipse-temurin:8-jdk"
_JAVA_CHECK: bool | None = None
_ACTIVE_DOCKER_CONTAINERS: dict[tuple[str, str, str], str] = {}
_DOCKER_LOCK = threading.Lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reproduce one FLAKYCAT Java flaky test.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV), help="FLAKYCAT metadata CSV path")
    parser.add_argument("--workspaces-dir", default=str(DEFAULT_WORKSPACES_DIR), help="FLAKYCAT workspace root")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR), help="Result output directory")
    parser.add_argument("--row-index", type=int, help="Zero-based row index in the exported CSV")
    parser.add_argument("--project", help="Project-name filter")
    parser.add_argument("--test", help="Substring filter for test method/FQN")
    parser.add_argument("--category", help="Category filter")
    parser.add_argument("--iterations", type=int, default=20, help="Repeated executions")
    parser.add_argument("--checkout", choices=["auto", "commit", "parent"], default="auto")
    parser.add_argument(
        "--runtime",
        choices=["auto", "host", "docker"],
        default="auto",
        help="How to run the Java build tool",
    )
    parser.add_argument(
        "--include-order-dependent",
        action="store_true",
        help="Allow order-dependent categories such as OD-Vic and Test order dependency",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running tests")
    return parser.parse_args()


def repo_key(repo_url: str) -> str:
    parsed = urlparse(repo_url.rstrip("/"))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Not a GitHub repo URL: {repo_url}")
    return f"{parts[0]}__{parts[1].removesuffix('.git')}"


def load_rows(input_csv: Path) -> list[dict[str, str]]:
    with input_csv.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def is_order_dependent(row: dict[str, str]) -> bool:
    return row.get("Category", "").strip().lower() in ORDER_DEPENDENT_CATEGORIES


def select_row(rows: list[dict[str, str]], args: argparse.Namespace) -> dict[str, str]:
    if args.row_index is not None:
        try:
            row = rows[args.row_index]
        except IndexError as exc:
            raise SystemExit(f"Row index out of range: {args.row_index}") from exc
        if is_order_dependent(row) and not args.include_order_dependent:
            raise SystemExit(
                "Selected row is order-dependent. Re-run with --include-order-dependent to allow it."
            )
        return row

    matches = []
    for row in rows:
        if is_order_dependent(row) and not args.include_order_dependent:
            continue
        if args.project and args.project.lower() != row.get("Project", "").lower():
            continue
        if args.category and args.category.lower() != row.get("Category", "").lower():
            continue
        if args.test:
            haystack = f"{row.get('Test Method', '')} {row.get('Fully Qualified Test Name', '')}".lower()
            if args.test.lower() not in haystack:
                continue
        matches.append(row)

    if not matches:
        if args.include_order_dependent:
            raise SystemExit("No FLAKYCAT row matched the provided filters.")
        raise SystemExit(
            "No non-order-dependent FLAKYCAT row matched the provided filters. "
            "Use --include-order-dependent if you want OD rows."
        )
    if len(matches) > 1:
        print(f"Matched {len(matches)} rows; using the first one. Add --row-index or --test to be more specific.")
    return matches[0]


def choose_checkout_sha(row: dict[str, str], mode: str) -> str:
    if mode == "commit":
        return row.get("Commit SHA", "").strip()
    if mode == "parent":
        return row.get("Parent SHA", "").strip() or row.get("Commit SHA", "").strip()
    return row.get("Checkout SHA", "").strip() or row.get("Commit SHA", "").strip()


def prepare_repo(row: dict[str, str], workspaces_dir: Path, checkout_mode: str) -> Path:
    repo_url = row.get("Project URL", "").strip()
    if not repo_url:
        raise SystemExit("Selected row has no Project URL, so it cannot be cloned automatically.")
    key = repo_key(repo_url)
    repo_dir = workspaces_dir / key
    if not repo_dir.exists():
        raise SystemExit(
            f"Workspace repo not found: {repo_dir}\n"
            "Run: .venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces"
        )

    checkout = choose_checkout_sha(row, checkout_mode)
    if not checkout:
        raise SystemExit("Selected row has no checkout SHA.")

    # Remove stale locks
    for lock in [".git/index.lock", ".git/HEAD.lock"]:
        lock_path = repo_dir / lock
        if lock_path.exists():
            print(f"Removing stale git lock: {lock_path}")
            lock_path.unlink(missing_ok=True)

    subprocess.run(["git", "fetch", "--all"], cwd=repo_dir, check=False, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-f", checkout], cwd=repo_dir, check=True)
    subprocess.run(["git", "clean", "-fdx"], cwd=repo_dir, check=False, capture_output=True, text=True)

    # Patch build files to fix dead repositories (Bintray/JCenter)
    patch_build_files(repo_dir)

    return repo_dir


def patch_build_files(repo_dir: Path) -> None:
    """Inject repositories and fix common build issues in Gradle/Maven."""
    # Patch Gradle files
    for path in repo_dir.rglob("*.gradle"):
        if not path.is_file(): continue
        try:
            content = path.read_text(encoding="utf-8")
            modified = False
            
            # 1. Replace jcenter() with mavenCentral()
            if "jcenter()" in content and "mavenCentral()" not in content:
                print(f"Patching {path.name}: replacing jcenter() with mavenCentral()")
                content = content.replace("jcenter()", "mavenCentral()")
                modified = True
            
            # 2. Add google() and mavenCentral() to repositories
            if "repositories {" in content:
                 # Add google() if missing (crucial for Android and modern plugins)
                 if "google()" not in content:
                     print(f"Patching {path.name}: adding google() repository")
                     content = content.replace("repositories {", "repositories {\n        google()")
                     modified = True
                 # Ensure mavenCentral() is there
                 if "mavenCentral()" not in content:
                     print(f"Patching {path.name}: adding mavenCentral() repository")
                     content = content.replace("repositories {", "repositories {\n        mavenCentral()")
                     modified = True
            
            # 3. Comment out strict Java version checks (e.g. in OkHttp)
            if "throw new IllegalStateException(\"Unexpected Java version" in content:
                print(f"Patching {path.name}: commenting out strict Java version check")
                content = content.replace("throw new IllegalStateException(\"Unexpected Java version", "// throw new IllegalStateException(\"Unexpected Java version")
                modified = True

            if modified:
                path.write_text(content, encoding="utf-8")
        except Exception as e:
            print(f"Failed to patch {path}: {e}")

    # Patch Maven files
    for path in repo_dir.rglob("pom.xml"):
        if not path.is_file(): continue
        try:
            content = path.read_text(encoding="utf-8")
            if ("bintray" in content.lower() or "jcenter" in content.lower()) and "mavenCentral" not in content:
                if "<repositories>" in content:
                    print(f"Patching {path.name}: adding Maven Central repository")
                    repo_xml = """<repository>
            <id>central</id>
            <name>Maven Central</name>
            <url>https://repo1.maven.org/maven2</url>
        </repository>"""
                    content = content.replace("<repositories>", f"<repositories>\n        {repo_xml}")
                    path.write_text(content, encoding="utf-8")
        except Exception as e:
            print(f"Failed to patch {path}: {e}")

    # Patch Gradle Wrapper (fix old http URLs)
    for path in repo_dir.rglob("gradle-wrapper.properties"):
        if not path.is_file(): continue
        try:
            content = path.read_text(encoding="utf-8")
            if "http://services.gradle.org" in content:
                print(f"Patching {path.name}: upgrading to https for services.gradle.org")
                content = content.replace("http://services.gradle.org", "https://services.gradle.org")
                path.write_text(content, encoding="utf-8")
        except Exception:
            pass


def build_command(row: dict[str, str], repo_dir: Path, runtime: str) -> tuple[list[str], Path, str]:
    module_path = row.get("Module Path", "").strip()
    work_dir = repo_dir / module_path if module_path else repo_dir
    test_fqn = row.get("Fully Qualified Test Name", "").strip()
    test_method = row.get("Test Method", "").strip()
    test_file = row.get("Test File", "").strip()
    if (work_dir / "pom.xml").exists():
        build_tool = "maven"
    elif (work_dir / "build.gradle").exists() or (work_dir / "build.gradle.kts").exists():
        build_tool = "gradle"
    else:
        raise SystemExit(f"Could not determine build tool for {work_dir}")

    if test_fqn and "." in test_fqn and test_method and "#" not in test_fqn:
        selector = f"{test_fqn}#{test_method}"
    elif test_fqn and "." in test_fqn:
        selector = test_fqn
    elif test_file and test_method:
        class_name = Path(test_file).stem
        selector = f"{class_name}#{test_method}"
    else:
        selector = test_method

    if build_tool == "maven":
        if runtime == "docker":
            cmd, _image = build_docker_runtime_command(repo_dir, work_dir, selector, build_tool)
            return cmd, work_dir, build_tool
        executable = "./mvnw" if (work_dir / "mvnw").exists() else "mvn"
        return [executable, "-q", f"-Dtest={selector}", "-DfailIfNoTests=false", "test"], work_dir, build_tool

    gradle_test = selector.replace("#", ".")
    if runtime == "docker":
        cmd, _image = build_docker_runtime_command(repo_dir, work_dir, selector, build_tool)
        return cmd, work_dir, build_tool
    executable = "./gradlew" if (work_dir / "gradlew").exists() else "gradle"
    return [executable, "test", "--tests", gradle_test], work_dir, build_tool


def build_docker_maven_command(selector: str) -> list[str]:
    return ["mvn", "-q", f"-Dtest={selector}", "-DfailIfNoTests=false", "test"]


def build_docker_gradle_command(work_dir: Path, selector: str) -> list[str]:
    if (work_dir / "gradlew").exists():
        return ["bash", "-lc", f"chmod +x ./gradlew && ./gradlew test --tests '{selector}'"]
    return ["gradle", "test", "--tests", selector]


def _sanitize_container_name(text: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", text.lower()).strip("-") or "flakycat"


def _docker_container_name(work_dir: Path, image: str) -> str:
    repo_part = _sanitize_container_name(work_dir.name)
    image_part = _sanitize_container_name(image.split(":")[0].split("/")[-1])
    return f"flakycat_{repo_part}_{image_part}"


def ensure_persistent_docker_container(base_dir: Path, image: str) -> str:
    key = (str(base_dir), image, os.getcwd())
    with _DOCKER_LOCK:
        cached = _ACTIVE_DOCKER_CONTAINERS.get(key)
        if cached:
            return cached

        container_name = _docker_container_name(base_dir, image)
        # Force remove if exists (e.g. from a previous crashed run)
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        result = subprocess.run(
            [
                "docker", "run", "-d", "--name", container_name,
                "-v", f"{base_dir}:/workspace",
                "-w", "/workspace",
                image,
                "tail", "-f", "/dev/null",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(result.stdout.strip() or f"Failed to start Docker container from {image}")

        _ACTIVE_DOCKER_CONTAINERS[key] = container_name
        return container_name


def cleanup_persistent_docker_container(base_dir: Path, image: str) -> None:
    key = (str(base_dir), image, os.getcwd())
    with _DOCKER_LOCK:
        container_name = _ACTIVE_DOCKER_CONTAINERS.pop(key, "")
        if not container_name:
            return
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )


def cleanup_all_for_repo(repo_dir: Path) -> None:
    repo_dir_str = str(repo_dir)
    with _DOCKER_LOCK:
        to_remove = [k for k in _ACTIVE_DOCKER_CONTAINERS.keys() if k[0] == repo_dir_str]
    for k in to_remove:
        with _DOCKER_LOCK:
            container_name = _ACTIVE_DOCKER_CONTAINERS.pop(k, "")
        if container_name:
            print(f"Cleaning up Docker container: {container_name}")
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )


def build_docker_runtime_command(repo_dir: Path, work_dir: Path, selector: str, build_tool: str) -> tuple[list[str], str]:
    if build_tool == "maven":
        image = DEFAULT_MAVEN_IMAGE
        inner_cmd = build_docker_maven_command(selector)
    else:
        image = DEFAULT_JAVA_IMAGE if (work_dir / "gradlew").exists() else DEFAULT_GRADLE_IMAGE
        inner_cmd = build_docker_gradle_command(work_dir, selector.replace("#", "."))

    container_name = ensure_persistent_docker_container(repo_dir, image)
    
    # Calculate relative path from repo root to module
    try:
        rel_path = work_dir.relative_to(repo_dir)
        if str(rel_path) == ".":
            return ["docker", "exec", container_name, *inner_cmd], image
        # Wrap command in a shell to CD into the module
        shell_cmd = ["bash", "-lc", f"cd {rel_path} && {' '.join(inner_cmd)}"]
        return ["docker", "exec", container_name, *shell_cmd], image
    except ValueError:
        # Fallback if work_dir is not under repo_dir
        return ["docker", "exec", container_name, *inner_cmd], image


def has_working_java() -> bool:
    global _JAVA_CHECK
    if _JAVA_CHECK is not None:
        return _JAVA_CHECK
    java_path = shutil.which("java")
    if not java_path:
        _JAVA_CHECK = False
        return _JAVA_CHECK
    try:
        result = subprocess.run(
            [java_path, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _JAVA_CHECK = False
        return _JAVA_CHECK
    _JAVA_CHECK = result.returncode == 0
    return _JAVA_CHECK


def resolve_runtime(preferred: str, work_dir: Path, build_tool: str) -> str:
    if preferred in {"host", "docker"}:
        return preferred

    if build_tool == "maven":
        if has_working_java() and ((work_dir / "mvnw").exists() or shutil.which("mvn")):
            return "host"
        return "docker"

    if has_working_java() and ((work_dir / "gradlew").exists() or shutil.which("gradle")):
        return "host"
    return "docker"


def classify_nonretryable_failure(log: str) -> str:
    text = (log or "").lower()
    if not text:
        return ""

    docker_markers = (
        "failed to connect to the docker api",
        "cannot connect to the docker daemon",
        "cannot connect to the docker api",
        "docker.sock",
        "error during connect",
        "permission denied while trying to connect to the docker api",
    )
    if any(marker in text for marker in docker_markers):
        return "docker_unavailable"

    dependency_markers = (
        "dependencyresolutionexception",
        "pluginresolutionexception",
        "could not resolve",
        "could not transfer artifact",
        "non-resolvable parent pom",
        "received status code 501 from server",
        "blocked mirror for repositories",
    )
    if any(marker in text for marker in dependency_markers):
        return "dependency_error"

    runtime_markers = (
        "unable to locate a java runtime",
        "required command not found",
        "command not found",
        "no such file or directory",
    )
    if any(marker in text for marker in runtime_markers):
        return "runtime_error"

    storage_markers = (
        "no space left on device",
        "disk quota exceeded",
    )
    if any(marker in text for marker in storage_markers):
        return "storage_error"

    return ""


def run_repeated(cmd: list[str], work_dir: Path, iterations: int, dry_run: bool, row: dict[str, str]) -> dict[str, object]:
    if dry_run:
        return {
            "command": cmd,
            "work_dir": str(work_dir),
            "iterations_requested": iterations,
            "iterations_executed": 0,
            "pass_count": 0,
            "fail_count": 0,
            "dry_run": True,
        }

    pass_count = 0
    fail_count = 0
    first_pass_log = ""
    first_fail_log = ""

    for index in range(iterations):
        try:
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=600,
            )
        except FileNotFoundError as exc:
            return {
                "command": cmd,
                "work_dir": str(work_dir),
                "iterations_requested": iterations,
                "iterations_executed": pass_count + fail_count,
                "pass_count": pass_count,
                "fail_count": fail_count,
                "reproduced": False,
                "error": f"Required command not found: {exc.filename}",
                "first_pass_log": first_pass_log,
                "first_fail_log": first_fail_log,
            }
        except subprocess.TimeoutExpired:
            fail_count += 1
            if not first_fail_log:
                first_fail_log = "Timeout while running Java test command."
            break
        if result.returncode == 0:
            pass_count += 1
            if not first_pass_log:
                first_pass_log = result.stdout
        else:
            fail_count += 1
            if not first_fail_log:
                first_fail_log = result.stdout
                terminal_reason = classify_nonretryable_failure(first_fail_log)
                if terminal_reason:
                    print(f"[{row.get('Project')}] [{index + 1}/{iterations}] FAIL (non-retryable: {terminal_reason})")
                    break
        print(f"[{row.get('Project')}] [{index + 1}/{iterations}] {'PASS' if result.returncode == 0 else 'FAIL'}")

    return {
        "command": cmd,
        "work_dir": str(work_dir),
        "iterations_requested": iterations,
        "iterations_executed": pass_count + fail_count,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "reproduced": bool(pass_count and fail_count),
        "first_pass_log": first_pass_log,
        "first_fail_log": first_fail_log,
    }


def execute_row(
    row: dict[str, str],
    workspaces_dir: Path,
    results_dir: Path,
    iterations: int,
    checkout_mode: str = "auto",
    runtime_preference: str = "auto",
    dry_run: bool = False,
) -> tuple[dict[str, object], Path]:
    repo_dir = prepare_repo(row, workspaces_dir, checkout_mode)
    module_path = row.get("Module Path", "").strip()
    work_dir = repo_dir / module_path if module_path else repo_dir
    build_tool = "maven" if (work_dir / "pom.xml").exists() else "gradle"
    runtime = resolve_runtime(runtime_preference, work_dir, build_tool)
    cleanup_image = ""
    cmd, work_dir, build_tool = build_command(row, repo_dir, runtime)
    if runtime == "docker":
        cleanup_image = DEFAULT_MAVEN_IMAGE if build_tool == "maven" else (
            DEFAULT_JAVA_IMAGE if (work_dir / "gradlew").exists() else DEFAULT_GRADLE_IMAGE
        )

    print(f"Project: {row.get('Project')}")
    print(f"Category: {row.get('Category')}")
    print(f"Checkout: {choose_checkout_sha(row, checkout_mode)}")
    print(f"Work dir: {work_dir}")
    print(f"Build tool: {build_tool}")
    print(f"Runtime: {runtime}")
    print("Command:")
    print(" ".join(cmd))

    try:
        result = run_repeated(cmd, work_dir, iterations, dry_run, row)
        result["row"] = row
        result["runtime"] = runtime
        result["build_tool"] = build_tool

        results_dir.mkdir(parents=True, exist_ok=True)
        output_path = results_dir / f"{row.get('Project', 'unknown')}-{row.get('Test Method', 'test')}.json"
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
        print(f"Wrote result: {output_path}")
        return result, output_path
    finally:
        if runtime == "docker" and cleanup_image:
            cleanup_persistent_docker_container(work_dir, cleanup_image)


def main() -> int:
    args = parse_args()
    rows = load_rows(Path(args.input_csv))
    row = select_row(rows, args)
    result, _ = execute_row(
        row=row,
        workspaces_dir=Path(args.workspaces_dir),
        results_dir=Path(args.results_dir),
        iterations=args.iterations,
        checkout_mode=args.checkout,
        runtime_preference=args.runtime,
        dry_run=args.dry_run,
    )
    return 0 if result.get("dry_run") or result.get("reproduced") else 1


def warm_up_repo(
    row: dict[str, str],
    workspaces_dir: Path,
    checkout_mode: str = "auto",
    runtime_preference: str = "auto",
) -> None:
    """Run a pre-build step (compile) for the repository to speed up individual test runs."""
    try:
        repo_dir = prepare_repo(row, workspaces_dir, checkout_mode)
    except SystemExit as e:
        print(f"Skipping warm-up: {e}")
        return

    module_path = row.get("Module Path", "").strip()
    work_dir = repo_dir / module_path if module_path else repo_dir

    if (work_dir / "pom.xml").exists():
        build_tool = "maven"
    elif (work_dir / "build.gradle").exists() or (work_dir / "build.gradle.kts").exists():
        build_tool = "gradle"
    else:
        print(f"Skipping warm-up: Could not determine build tool for {work_dir}")
        return

    runtime = resolve_runtime(runtime_preference, work_dir, build_tool)

    print(f"Warming up project: {row.get('Project')} ({build_tool} on {runtime})")
    
    if build_tool == "maven":
        # -DskipTests to only compile and install dependencies
        cmd = ["mvn", "compile", "test-compile", "-DskipTests", "-q"]
        if runtime == "docker":
            container_name = ensure_persistent_docker_container(repo_dir, DEFAULT_MAVEN_IMAGE)
            rel_path = work_dir.relative_to(repo_dir)
            if str(rel_path) != ".":
                cmd = ["docker", "exec", container_name, "bash", "-lc", f"cd {rel_path} && mvn compile test-compile -DskipTests -q -DfailIfNoTests=false"]
            else:
                cmd = ["docker", "exec", container_name, "mvn", "compile", "test-compile", "-DskipTests", "-q", "-DfailIfNoTests=false"]
        else:
            executable = "./mvnw" if (work_dir / "mvnw").exists() else "mvn"
            cmd = [executable, "compile", "test-compile", "-DskipTests", "-q", "-DfailIfNoTests=false"]
    else:
        # Gradle: classes and testClasses tasks
        cmd = ["./gradlew", "classes", "testClasses", "-x", "test"]
        if runtime == "docker":
            image = DEFAULT_JAVA_IMAGE if (work_dir / "gradlew").exists() else DEFAULT_GRADLE_IMAGE
            container_name = ensure_persistent_docker_container(repo_dir, image)
            rel_path = work_dir.relative_to(repo_dir)
            if (work_dir / "gradlew").exists():
                 inner_cmd = "./gradlew classes testClasses -x test"
            else:
                 inner_cmd = "gradle classes testClasses -x test"
            
            if str(rel_path) != ".":
                cmd = ["docker", "exec", container_name, "bash", "-lc", f"cd {rel_path} && {inner_cmd}"]
            else:
                cmd = ["docker", "exec", container_name, "bash", "-lc", inner_cmd]
        else:
            executable = "./gradlew" if (work_dir / "gradlew").exists() else "gradle"
            cmd = [executable, "classes", "testClasses", "-x", "test"]

    print(f"Executing warm-up command: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, cwd=work_dir, check=False, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        print("Warm-up timed out after 10 minutes.")
    except Exception as e:
        print(f"Warm-up failed: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
