#!/usr/bin/env python3
"""
Cross-model regression check — one build per project, three models' patches.

For every project/commit that appears in ANY model's fixed set, this clones and
builds the Docker image ONCE, computes the clean baseline ONCE per test directory,
and then applies each model's stored patch in turn (resetting between them). This
is the efficient version of scripts.regression_check: the expensive clone+build+
baseline work is shared across all three models instead of repeated per session.

No LLM is called — it replays the stored patches (git apply). A patch that does
not apply cleanly is skipped (status=patch_failed), never force-merged, so an
unfaithful diff is never counted as a clean or regressing fix.

Method (per project/commit):
  1. Clone at sha, build image, start container.
  2. For each test directory: BASELINE = run K times, keep tests passing in all K
     (stably passing). Cached per (project, sha, directory), shared by all models.
  3. For each (model, fixed-test) whose target lives in that directory:
       reset files -> git apply the model's patch -> run K times (POST) -> reset.
     Regression = a stably-passing test (other than the target) that now fails in
     ALL K post runs, or disappears in ALL K post runs.

Resumable: results are saved as each patch is checked; a re-run skips (model,
project, sha, test) rows already present in the output file. Projects are checked
in parallel (--threads), one container each; work within a project is serial.

Usage:
  python3 -m scripts.regression_check_all --sample 30
  python3 -m scripts.regression_check_all --sample 0 --threads 4     # all fixed
  python3 -m scripts.regression_check_all --project centreon-sdk-python

Writes results/regression_check_all.json and prints a per-model summary.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.tools.docker_infra import (  # noqa: E402
    WORKSPACE_DIR,
    _clone_and_checkout,
    _build_project_image,
    _ensure_repo_container,
    reset_container_files,
    cleanup_project_container,
)
from scripts.regression_check import (  # noqa: E402
    MERGED_CSV,
    load_url_map,
    collect_fixed_tests,
    run_suite,
    stably_passing,
    apply_patch,
    _test_dir,
    _target_key,
    _slim,
)

# session directory : model label
SESSIONS = [
    ("results/2026-05-29_18-21-51", "minimax"),
    ("results/2026-06-08_09-07-21", "deepseek"),
    ("results/2026-06-09_08-41-31", "gpt-oss"),
]


def load_all_fixed(url_map: dict) -> list[dict]:
    """Fixed tests from every session, each tagged with its model."""
    out = []
    for session_dir, model in SESSIONS:
        if not os.path.isdir(session_dir):
            print(f"  [warn] session missing: {session_dir}")
            continue
        for f in collect_fixed_tests(session_dir, url_map):
            f["model"] = model
            out.append(f)
    return out


def _row_key(f: dict) -> tuple:
    return (f["model"], f["project"], f["sha"], f["test_name"])


def _regressions(stable: set, post_runs: list, test_name: str):
    tgt_file, tgt_method = _target_key(test_name)

    def is_target(key: str) -> bool:
        return key.endswith(f"::{tgt_method}") and tgt_file.split("/")[-1].split(".")[0] in key

    broken, removed, flaky = [], [], []
    for key in sorted(stable):
        if is_target(key):
            continue
        statuses = [r.get(key) for r in post_runs]
        fails = sum(1 for s in statuses if s == "fail")
        missing = sum(1 for s in statuses if s is None)
        if missing == len(post_runs):
            removed.append(key)
        elif fails == len(post_runs):
            broken.append(key)
        elif fails > 0 or missing > 0:
            flaky.append(key)
    return broken, removed, flaky


def _summarize(results: list) -> dict:
    by_model = defaultdict(lambda: {"checked": 0, "regressions": 0,
                                    "patch_failed": 0, "no_baseline": 0,
                                    "setup_failed": 0, "no_post": 0})
    for r in results:
        m = by_model[r["model"]]
        st = r["status"]
        if st in ("clean", "regression"):
            m["checked"] += 1
            if st == "regression":
                m["regressions"] += 1
        elif st in m:
            m[st] += 1
    for m, d in by_model.items():
        d["regression_rate"] = round(d["regressions"] / d["checked"], 4) if d["checked"] else None
    return {"models": dict(by_model), "results": results}


def check_all(args) -> dict:
    url_map = load_url_map(MERGED_CSV)
    fixed = load_all_fixed(url_map)
    if args.project:
        fixed = [f for f in fixed if f["project"] == args.project]

    out_path = args.out or os.path.join("results", "regression_check_all.json")

    # Resume: keep prior rows, skip their (model, project, sha, test).
    existing, done = [], set()
    if os.path.exists(out_path):
        try:
            existing = json.load(open(out_path)).get("results", [])
            done = {(r["model"], r["project"], r["sha"], r["test_name"]) for r in existing}
        except Exception:
            existing, done = [], set()

    # Deterministic order so a later, larger --sample continues past an earlier one.
    order = list(fixed)
    random.Random(args.seed).shuffle(order)
    remaining = [f for f in order if _row_key(f) not in done]
    target = args.sample if args.sample and args.sample > 0 else len(order)
    need = max(0, target - len(existing))
    todo = remaining[:need]

    # Group the work to do by (project, sha): one build serves all its patches.
    groups: dict[tuple, list] = defaultdict(list)
    for f in todo:
        groups[(f["project"], f["sha"])].append(f)

    print(f"Fixed rows total: {len(fixed)} | already_done: {len(existing)} | "
          f"to_check: {len(todo)} | groups(project,sha): {len(groups)} | "
          f"baseline_runs={args.baseline_runs} | threads={args.threads}")
    print("-" * 70)

    seeds = list(range(1, args.baseline_runs + 1))
    results = list(existing)
    results_lock = threading.Lock()
    proj_locks: dict = {}
    proj_guard = threading.Lock()

    def proj_lock(p):
        with proj_guard:
            return proj_locks.setdefault(p, threading.Lock())

    def add(rec):
        with results_lock:
            results.append(rec)
            json.dump(_summarize(results), open(out_path, "w"), indent=2)

    def do_group(key_group):
        (project, sha), group = key_group
        url = group[0]["url"]
        project_dir = os.path.join(WORKSPACE_DIR, project)
        # Serialise same-project work: shared clone dir + container name.
        with proj_lock(project):
            container = None
            try:
                _clone_and_checkout(url, sha, project_dir)
                tag = _build_project_image(project, project_dir)
                container = _ensure_repo_container(tag, project, sha)
            except Exception as e:
                for f in group:
                    add({**_slim(f), "model": f["model"], "status": "setup_failed",
                         "error": str(e)[:200]})
                    print(f"  [setup-fail] {f['model']} {project} :: {f['test_name']} ({str(e)[:50]})")
                if container:
                    cleanup_project_container(project, sha)
                return
            try:
                baseline: dict[str, tuple] = {}   # directory -> (stable set|None)
                for f in group:
                    directory = _test_dir(f["test_name"])
                    if directory not in baseline:
                        reset_container_files(project, sha)
                        base = [r for r in (run_suite(container, directory, s, args.timeout)
                                            for s in seeds) if r is not None]
                        baseline[directory] = stably_passing(base) if base else None
                    stable = baseline[directory]
                    if not stable:
                        add({**_slim(f), "model": f["model"], "status": "no_baseline"})
                        print(f"  [no-baseline] {f['model']} {project} :: {f['test_name']}")
                        continue

                    reset_container_files(project, sha)
                    if not apply_patch(container, f["patch"]):
                        add({**_slim(f), "model": f["model"], "status": "patch_failed"})
                        print(f"  [patch-fail] {f['model']} {project} :: {f['test_name']}")
                        reset_container_files(project, sha)
                        continue

                    post = [r for r in (run_suite(container, directory, s, args.timeout)
                                        for s in seeds) if r is not None]
                    reset_container_files(project, sha)
                    if not post:
                        add({**_slim(f), "model": f["model"], "status": "no_post"})
                        print(f"  [no-post] {f['model']} {project} :: {f['test_name']}")
                        continue

                    broken, removed, flaky = _regressions(stable, post, f["test_name"])
                    status = "regression" if (broken or removed) else "clean"
                    add({**_slim(f), "model": f["model"], "status": status,
                         "baseline_stable": len(stable), "post_runs": len(post),
                         "regressions_broken": broken, "regressions_removed": removed,
                         "regressions_flaky": flaky})
                    tag_txt = (f"REGRESSION broke={len(broken)} removed={len(removed)}"
                               if (broken or removed)
                               else "clean" + (f" (+{len(flaky)} flaky)" if flaky else ""))
                    print(f"  [{tag_txt}] {f['model']} {project} :: {f['test_name']}")
            finally:
                cleanup_project_container(project, sha)

    if groups:
        with ThreadPoolExecutor(max_workers=max(1, args.threads)) as pool:
            list(pool.map(do_group, list(groups.items())))

    summary = _summarize(results)
    json.dump(summary, open(out_path, "w"), indent=2)
    print("-" * 70)
    for model, d in sorted(summary["models"].items()):
        print(f"{model:10} checked={d['checked']:4} regressions={d['regressions']:3} "
              f"rate={d['regression_rate']} "
              f"(patch_failed={d['patch_failed']} no_baseline={d['no_baseline']} "
              f"setup_failed={d['setup_failed']} no_post={d['no_post']})")
    print(f"\nReport: {out_path}")
    return summary


def main():
    ap = argparse.ArgumentParser(description="Cross-model repair regression check (patch replay).")
    ap.add_argument("--sample", type=int, default=30,
                    help="Total fixed rows to check across all models; 0 = all.")
    ap.add_argument("--baseline-runs", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--threads", type=int, default=4,
                    help="Projects checked in parallel (one container each).")
    ap.add_argument("--project", default="", help="Restrict to one project name.")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    check_all(args)


if __name__ == "__main__":
    main()
