#!/usr/bin/env python3
"""
Post-hoc regression check for the repair stage.

For every confirmed-fixed test, this reconstructs the fix from its stored patch
and checks whether applying it breaks OTHER tests in the same test directory
(a regression), which the original pipeline never measured.

Method (per fixed test):
  1. Clone the project at its detected SHA and build the same Docker image the
     pipeline used.
  2. BASELINE — run the target test's directory K times (different seeds) with
     no patch applied. A test is "stably passing" if it passes in ALL K runs.
     This filters out the projects' own flaky tests so pre-existing flakiness
     is not miscounted as a regression. Baseline is cached per (project, SHA).
  3. Apply the stored patch (git apply).
  4. POST — run the directory K times again.
  5. A regression is a stably-passing test (other than the target) that now
     FAILS in ALL K post runs (consistent) — reported separately from tests
     that fail only intermittently (likely flaky, not a true regression).

Usage:
  python3 -m scripts.regression_check --session results/<dir> [--sample N]
  python3 -m scripts.regression_check --session results/<dir> --baseline-runs 3
  python3 -m scripts.regression_check --session results/<dir> --project foo --limit 5

Nothing is written into the thesis. It prints a summary and writes a JSON report.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import random
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

# Make `src` importable when run as a script from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.tools.docker_infra import (  # noqa: E402
    WORKSPACE_DIR,
    _clone_and_checkout,
    _build_project_image,
    _ensure_repo_container,
    _docker_exec,
    _make_pytest_cmd,
    reset_container_files,
    cleanup_project_container,
)

MERGED_CSV = os.path.join(
    _REPO_ROOT, "datasets", "idoft", "idoft-merged-reproduction-results.csv"
)


# ── Data loading ─────────────────────────────────────────────────────────────

def load_url_map(csv_path: str) -> dict[str, str]:
    """project name -> repository URL, from the merged reproduction CSV."""
    mapping: dict[str, str] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("Project") or "").strip()
            url = (row.get("Project URL") or "").strip()
            if name and url and name not in mapping:
                mapping[name] = url
    return mapping


def collect_fixed_tests(session_dir: str, url_map: dict[str, str]) -> list[dict]:
    """Every confirmed-fixed test in the session that has a usable patch."""
    fixed: list[dict] = []
    for path in sorted(glob.glob(os.path.join(session_dir, "repair", "*.json"))):
        rec = json.load(open(path, encoding="utf-8"))
        project = rec.get("project") or ""
        for t in rec.get("tests", []):
            if not t.get("is_fixed"):
                continue
            url = url_map.get(project)
            if not url:
                continue  # cannot clone without a URL
            test_name = t.get("test_name") or ""
            # Prefer the FAITHFUL diff captured as the get_diff() tool output in
            # the trace (a real `git diff`), falling back to the model's
            # self-reported patch (often fabricated, rarely applies cleanly).
            trace_diff = load_trace_diff(session_dir, project, test_name)
            patch  = trace_diff or (t.get("patch") or "")
            source = "trace" if trace_diff else "reported"
            if "diff --git" not in patch and "@@" not in patch:
                continue  # no applicable patch text
            fixed.append({
                "project": project,
                "url": url,
                "sha": t.get("sha_detected") or "",
                "test_name": test_name,
                "flaky_type": t.get("flaky_type") or "",
                "patch": patch,
                "patch_source": source,
                "patch_target": t.get("patch_target"),
            })
    return fixed


def load_trace_diff(session_dir: str, project: str, test_name: str) -> str | None:
    """Return the last full (untruncated) get_diff() output for this test, if any.

    The trace stores the get_diff tool output, which is the real `git diff`.
    Long outputs are truncated (marked with a '...[+N chars]' suffix); those are
    unusable, so only complete diffs are returned.
    """
    path = os.path.join(session_dir, "traces", "repair", f"{project}.jsonl")
    if not os.path.exists(path):
        return None
    best = None
    for line in open(path, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("test_name") != test_name:
            continue
        for sp in rec.get("spans", []):
            for ev in sp.get("tool_events", []):
                if ev.get("tool") != "get_diff":
                    continue
                out = ev.get("output")
                if (isinstance(out, str) and out.lstrip().startswith("diff --git")
                        and "chars]" not in out[-60:]):
                    best = out  # keep the last complete diff
    return best


# ── Suite execution ──────────────────────────────────────────────────────────

def _test_dir(test_name: str) -> str:
    test_file = test_name.split("::")[0]
    return os.path.dirname(test_file) or "."


def _target_key(test_name: str) -> tuple[str, str]:
    """(file, method-name) used to exclude the target test from regressions."""
    parts = test_name.split("::")
    return parts[0], (parts[-1] if len(parts) > 1 else "")


def run_suite(container: str, directory: str, seed: int, timeout: int) -> dict[str, str] | None:
    """Run `directory` once and return {test_key: 'pass'|'fail'|'skip'} from JUnit XML.

    Returns None if the run could not be collected at all (import/collection error),
    so the caller can skip an untrustworthy comparison.
    """
    xml_path = "/app/_reg.xml"
    cmd = _make_pytest_cmd(
        [directory, f"--randomly-seed={seed}", f"--junit-xml={xml_path}",
         "-q", "--tb=no", "-o", "junit_family=xunit2"],
        enable_randomly=True,
    )
    try:
        _docker_exec(container, cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None

    cat = _docker_exec(container, ["cat", xml_path], timeout=30)
    if cat.returncode != 0 or not cat.stdout.strip():
        return None
    try:
        root = ET.fromstring(cat.stdout)
    except ET.ParseError:
        return None

    outcomes: dict[str, str] = {}
    for tc in root.iter("testcase"):
        fname = tc.get("file") or tc.get("classname") or ""
        name = tc.get("name") or ""
        key = f"{fname}::{name}"
        status = "pass"
        for child in tc:
            tag = child.tag.lower()
            if tag in ("failure", "error"):
                status = "fail"
                break
            if tag == "skipped":
                status = "skip"
                break
        # A test can appear once; last status wins if duplicated.
        outcomes[key] = status
    return outcomes if outcomes else None


def stably_passing(runs: list[dict[str, str]]) -> set[str]:
    """Keys that passed in every run."""
    if not runs:
        return set()
    common = set(runs[0])
    for r in runs[1:]:
        common &= set(r)
    return {k for k in common if all(r.get(k) == "pass" for r in runs)}


# ── Patch application ────────────────────────────────────────────────────────

def apply_patch(container: str, patch: str) -> bool:
    """Write the patch into the container and apply it with git. Returns success."""
    write = subprocess.run(
        ["docker", "exec", "-i", "-w", "/app", container, "sh", "-c", "cat > /app/_patch.diff"],
        input=patch, text=True, capture_output=True, timeout=30,
    )
    if write.returncode != 0:
        return False
    # Apply ONLY if the diff matches the real file (faithful). We deliberately do
    # NOT use --3way or fuzz: the stored "patch" is the model's self-reported diff
    # and is sometimes fabricated, so a forced/merged apply would replay a fiction.
    # A clean apply is our fidelity filter — unfaithful patches are skipped, not guessed.
    variants = [
        ["--recount"],
        ["--recount", "--ignore-whitespace"],
    ]
    for extra in variants:
        r = _docker_exec(
            container,
            ["git", "-C", "/app", "apply", *extra, "_patch.diff"],
            timeout=60,
        )
        if r.returncode == 0:
            return True
    return False


# ── Main check ───────────────────────────────────────────────────────────────

def check_session(args) -> dict:
    url_map = load_url_map(MERGED_CSV)
    fixed = collect_fixed_tests(args.session, url_map)

    if args.faithful_only:
        fixed = [f for f in fixed if f.get("patch_source") == "trace"]
    if args.project:
        fixed = [f for f in fixed if f["project"] == args.project]
    if args.sample and args.sample < len(fixed):
        rng = random.Random(args.seed_sample)
        fixed = rng.sample(fixed, args.sample)
    if args.limit:
        fixed = fixed[: args.limit]

    print(f"Session: {args.session}")
    print(f"Fixed tests to check: {len(fixed)}  "
          f"(baseline_runs={args.baseline_runs}, dir-scoped)")
    print("-" * 70)

    seeds = list(range(1, args.baseline_runs + 1))
    baseline_cache: dict[tuple[str, str], tuple[set[str] | None, str]] = {}
    results: list[dict] = []

    # Group by (project, sha) so the baseline is computed once per checkout.
    by_repo: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in fixed:
        by_repo[(f["project"], f["sha"])].append(f)

    for (project, sha), group in by_repo.items():
        url = group[0]["url"]
        project_dir = os.path.join(WORKSPACE_DIR, project)
        container = None
        try:
            _clone_and_checkout(url, sha, project_dir)
            tag = _build_project_image(project, project_dir)
            container = _ensure_repo_container(tag, project, sha)
        except Exception as e:  # clone / build failed
            for f in group:
                results.append({**_slim(f), "status": "setup_failed", "error": str(e)[:200]})
                print(f"  [setup-fail] {project} :: {f['test_name']}  ({str(e)[:60]})")
            if container:
                cleanup_project_container(project, sha)
            continue

        for f in group:
            directory = _test_dir(f["test_name"])
            cache_key = (project, sha, directory)
            # Baseline for this directory (cached).
            if cache_key not in baseline_cache:
                reset_container_files(project, sha)
                base_runs = []
                for s in seeds:
                    r = run_suite(container, directory, s, args.timeout)
                    if r is not None:
                        base_runs.append(r)
                stable = stably_passing(base_runs) if base_runs else None
                baseline_cache[cache_key] = (stable, f"{len(base_runs)}/{len(seeds)} runs collected")
            stable, base_note = baseline_cache[cache_key]

            if not stable:
                results.append({**_slim(f), "status": "no_baseline", "note": base_note})
                print(f"  [no-baseline] {project} :: {f['test_name']}  ({base_note})")
                continue

            # Apply the fix and re-run.
            reset_container_files(project, sha)
            if not apply_patch(container, f["patch"]):
                results.append({**_slim(f), "status": "patch_failed"})
                print(f"  [patch-fail] {project} :: {f['test_name']}")
                reset_container_files(project, sha)
                continue

            post_runs = []
            for s in seeds:
                r = run_suite(container, directory, s, args.timeout)
                if r is not None:
                    post_runs.append(r)
            reset_container_files(project, sha)

            if not post_runs:
                results.append({**_slim(f), "status": "no_post"})
                print(f"  [no-post] {project} :: {f['test_name']}")
                continue

            tgt_file, tgt_method = _target_key(f["test_name"])

            def _is_target(key: str) -> bool:
                return key.endswith(f"::{tgt_method}") and tgt_file.split("/")[-1].split(".")[0] in key

            broken, removed, flaky = [], [], []
            for key in sorted(stable):
                if _is_target(key):
                    continue
                statuses = [r.get(key) for r in post_runs]           # None = absent
                fails   = sum(1 for s in statuses if s == "fail")
                missing = sum(1 for s in statuses if s is None)
                if missing == len(post_runs):
                    removed.append(key)          # stably passed before, now gone
                elif fails == len(post_runs):
                    broken.append(key)           # stably passed before, now always fails
                elif fails > 0 or missing > 0:
                    flaky.append(key)            # inconsistent -> likely flaky, not counted

            regressions = broken + removed
            status = "regression" if regressions else "clean"
            results.append({
                **_slim(f),
                "status": status,
                "baseline_stable": len(stable),
                "post_runs": len(post_runs),
                "regressions_broken": broken,
                "regressions_removed": removed,
                "regressions_flaky": flaky,
            })
            if regressions:
                tag_txt = f"REGRESSION broke={len(broken)} removed={len(removed)}"
            else:
                tag_txt = "clean" + (f" (+{len(flaky)} flaky)" if flaky else "")
            print(f"  [{tag_txt}] {project} :: {f['test_name']}")

        if container:
            cleanup_project_container(project, sha)

    return _summarise(args, results)


def _slim(f: dict) -> dict:
    return {k: f.get(k) for k in ("project", "sha", "test_name", "flaky_type",
                                  "patch_target", "patch_source")}


def _summarise(args, results: list[dict]) -> dict:
    checked = [r for r in results if r["status"] in ("clean", "regression")]
    regressions = [r for r in results if r["status"] == "regression"]
    summary = {
        "session": args.session,
        "baseline_runs": args.baseline_runs,
        "fixed_considered": len(results),
        "checked": len(checked),
        "regression_fixes": len(regressions),
        "regression_rate_of_checked": round(len(regressions) / len(checked), 4) if checked else None,
        "skipped": {
            s: sum(1 for r in results if r["status"] == s)
            for s in ("setup_failed", "no_baseline", "patch_failed", "no_post")
        },
        "results": results,
    }
    print("-" * 70)
    print(f"Checked (baseline + post both usable): {len(checked)}")
    print(f"Fixes with >=1 consistent regression:  {len(regressions)}")
    if checked:
        print(f"Regression rate among checked:         "
              f"{summary['regression_rate_of_checked']*100:.1f}%")
    print(f"Skipped: {summary['skipped']}")
    if regressions:
        print("\nFixes that caused regressions:")
        for r in regressions:
            b = r.get("regressions_broken", [])
            rm = r.get("regressions_removed", [])
            print(f"  {r['project']} :: {r['test_name']}")
            if b:
                print(f"       broke:   {b}")
            if rm:
                print(f"       removed: {rm}")
    return summary


def main():
    ap = argparse.ArgumentParser(description="Post-hoc repair regression check.")
    ap.add_argument("--session", required=True, help="Path to a results/<session> directory.")
    ap.add_argument("--baseline-runs", type=int, default=3,
                    help="Runs per phase; a test must pass all to count as stably passing.")
    ap.add_argument("--sample", type=int, default=0, help="Check a random N of the fixed tests.")
    ap.add_argument("--seed-sample", type=int, default=42, help="RNG seed for --sample.")
    ap.add_argument("--limit", type=int, default=0, help="Cap number of fixed tests (after sampling).")
    ap.add_argument("--project", default="", help="Only this project name.")
    ap.add_argument("--faithful-only", action="store_true",
                    help="Only check fixes whose diff comes from the trace get_diff output.")
    ap.add_argument("--timeout", type=int, default=600, help="Per suite-run timeout (seconds).")
    ap.add_argument("--out", default="", help="Write JSON report here (default: <session>/regression_check.json).")
    args = ap.parse_args()

    if not os.path.isdir(args.session):
        ap.error(f"Session dir not found: {args.session}")

    summary = check_session(args)
    out = args.out or os.path.join(args.session, "regression_check.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()
