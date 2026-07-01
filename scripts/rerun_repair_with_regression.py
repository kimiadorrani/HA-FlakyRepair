#!/usr/bin/env python3
"""
Re-run repair on each model's own previously-fixed tests, storing FAITHFUL
patches and measuring regression — the two things the original pipeline did not.

For each model we take the first N tests THAT MODEL previously fixed
(is_fixed=True in its repair records), write a small temporary dataset for the
record, and re-run the real repair agent on them via the same building blocks as
src.agents.repair.repair_agent_node. Two differences from the original run:

  1. FAITHFUL PATCH — after the agent finishes we capture the real `git diff`
     straight from the container (untruncated), instead of the model's retyped
     `diff` field. That copy actually re-applies.
  2. REGRESSION — before the agent runs we record the test directory's stably
     passing tests (BASELINE), and after the fix we run it again (POST). A
     regression is a stably-passing test (other than the target) that now fails
     in all runs or disappears in all runs.

Everything happens in the SAME live container the fix is applied to, so there is
no patch-replay / base-mismatch problem. This calls the model (repair only;
detection is reused), so it costs tokens — but only on N tests per model.

Usage:
  python3 -m scripts.rerun_repair_with_regression --sample 50 --threads 4
  python3 -m scripts.rerun_repair_with_regression --model minimax --sample 50

Output per model (share these back):
  results/rerun_fixed/<model>/dataset.csv           the temp dataset used
  results/rerun_fixed/<model>/repair_regression.json faithful patches + regression
"""
from __future__ import annotations

import argparse
import csv
import glob
import itertools
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from dotenv import load_dotenv
load_dotenv(os.path.join(_REPO, ".env"))

from langgraph.prebuilt import create_react_agent
from langchain_core.messages import SystemMessage, HumanMessage

from src.config.models import get_model
from src.agents.repair import (
    _SYSTEM_PROMPT, _parse_repair_response, _apply_xml_fallback,
    REPAIR_TIMEOUT_SECONDS,
)
from src.tools.repair_tools import make_repair_tools
from src.tools.docker_infra import (
    WORKSPACE_DIR, _clone_and_checkout, _build_project_image,
    _repo_container_name, _docker_exec, cleanup_project_container,
)
from scripts.regression_check import (
    load_url_map, MERGED_CSV, run_suite, stably_passing, _test_dir, _target_key,
)

# session directory : model key (as used by src.config.models.get_model)
SESSIONS = [
    ("results/2026-05-29_18-21-51", "minimax"),
    ("results/2026-06-08_09-07-21", "fireworks"),   # DeepSeek
    ("results/2026-06-09_08-41-31", "gpt-oss"),
]

TEST_NAME_COL = ("Pytest Test Name "
                 "(PathToFile::TestClass::TestMethod or PathToFile::TestMethod)")


def _detection_index(session_dir: str) -> dict:
    """(project, test_name) -> detection record (root_cause, profiles, logs, ...)."""
    idx = {}
    for path in sorted(glob.glob(os.path.join(session_dir, "detection", "*.json"))):
        rec = json.load(open(path, encoding="utf-8"))
        proj = rec.get("project")
        file_url = rec.get("project_url", "")
        for t in rec.get("tests", []):
            t.setdefault("project_url", file_url)
            idx[(proj, t.get("test_name"))] = t
    return idx


def load_fixed(session_dir: str, url_map: dict) -> list[dict]:
    """First-N-ready: every test THIS model fixed, joined to its detection input.

    Sorted deterministically by (project, test) so `--sample N` is reproducible.
    """
    det = _detection_index(session_dir)
    out = []
    for path in sorted(glob.glob(os.path.join(session_dir, "repair", "*.json"))):
        rec = json.load(open(path, encoding="utf-8"))
        proj = rec.get("project")
        for t in rec.get("tests", []):
            if not t.get("is_fixed"):
                continue
            d = det.get((proj, t.get("test_name")))
            if not d:
                continue
            url = d.get("project_url") or url_map.get(proj, "")
            sha = d.get("sha_detected") or t.get("sha_detected") or ""
            ftype = d.get("flaky_type") or t.get("flaky_type") or ""
            if not (url and sha and ftype):
                continue
            out.append({
                "project": proj,
                "project_url": url,
                "sha_detected": sha,
                "test_name": t.get("test_name"),
                "flaky_type": ftype,
                "category": d.get("category") or ftype,
                "root_cause_analysis": d.get("root_cause_analysis") or "",
                "failing_log": d.get("failing_log") or "",
                "execution_profiles": d.get("execution_profiles") or [],
            })
    out.sort(key=lambda x: (x["project"], x["test_name"]))
    return out


def build_prompt(t: dict) -> str:
    """Same prompt text as src.agents.repair.repair_agent_node."""
    fl = t.get("failing_log") or ""
    return (
        f"Fix the following flaky test.\n\n"
        f"Repository: {t['project_url']}\n"
        f"Test: {t['test_name']}\n\n"
        f"Root cause analysis:\n{t.get('root_cause_analysis') or ''}\n\n"
        f"Failing log (excerpt):\n{fl[:2000] if fl else 'N/A'}\n\n"
        f"Apply a minimal fix, verify it eliminates the flakiness, then output "
        f"your result as JSON."
    )


def capture_diff(container: str) -> str:
    """The real, untruncated git diff from inside the container = faithful patch."""
    try:
        r = _docker_exec(container, ["git", "-C", "/app", "diff"], timeout=30)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def regressions_of(stable: set, post_runs: list, test_name: str):
    tgt_file, tgt_method = _target_key(test_name)

    def is_target(k: str) -> bool:
        return k.endswith(f"::{tgt_method}") and tgt_file.split("/")[-1].split(".")[0] in k

    broken, removed = [], []
    for k in sorted(stable):
        if is_target(k):
            continue
        st = [r.get(k) for r in post_runs]
        if all(s is None for s in st):
            removed.append(k)
        elif all(s == "fail" for s in st):
            broken.append(k)
    return broken, removed


def _summarize(model: str, results: list) -> dict:
    checked = [r for r in results if r["status"] in ("clean", "regression")]
    regs = [r for r in results if r["status"] == "regression"]
    refixed = [r for r in checked if r.get("is_fixed")]
    return {
        "model": model,
        "processed": len(results),
        "checked": len(checked),           # baseline + post both usable
        "re_fixed": len(refixed),          # agent fixed it again this run
        "regressions": len(regs),
        "regression_rate": round(len(regs) / len(checked), 4) if checked else None,
        "skipped": {s: sum(1 for r in results if r["status"] == s)
                    for s in ("setup_failed", "no_baseline", "repair_timeout", "no_post")},
        "results": results,
    }


def write_dataset(model: str, tests: list, out_dir: str) -> str:
    path = os.path.join(out_dir, "dataset.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Project URL", "SHA Detected", TEST_NAME_COL, "Category"])
        for t in tests:
            w.writerow([t["project_url"], t["sha_detected"], t["test_name"], t["category"]])
    return path


def run_model(model: str, session_dir: str, url_map: dict, args) -> dict:
    out_dir = os.path.join("results", "rerun_fixed", model)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "repair_regression.json")

    all_fixed = load_fixed(session_dir, url_map)
    target = args.sample if args.sample and args.sample > 0 else len(all_fixed)
    chosen = all_fixed[:target]
    write_dataset(model, chosen, out_dir)

    # Resume: keep prior rows, skip their (project, test).
    existing, done = [], set()
    if os.path.exists(out_path):
        try:
            existing = json.load(open(out_path)).get("results", [])
            done = {(r.get("project"), r.get("test_name")) for r in existing}
        except Exception:
            existing, done = [], set()
    todo = [t for t in chosen if (t["project"], t["test_name"]) not in done]

    print(f"[{model}] fixed_available={len(all_fixed)} | selected={len(chosen)} "
          f"| already_done={len(existing)} | to_run={len(todo)} | "
          f"baseline_runs={args.baseline_runs} | threads={args.threads}")

    model_cfg = get_model(model)
    seeds = list(range(1, args.baseline_runs + 1))
    results = list(existing)
    results_lock = threading.Lock()
    counter = itertools.count(1)

    # Prime each (project, SHA) once: clone + build the image before any of its
    # sibling tests start, so concurrent tests never race on the shared clone dir.
    # After priming, each test runs in its OWN container off that image, so tests
    # of the same project run in parallel too.
    primed: set = set()
    prime_locks: dict = {}
    prime_guard = threading.Lock()

    def prime(proj, sha, url):
        key = (proj, sha)
        with prime_guard:
            lock = prime_locks.setdefault(key, threading.Lock())
        with lock:
            if key in primed:
                return
            project_dir = os.path.join(WORKSPACE_DIR, proj)
            _clone_and_checkout(url, sha, project_dir)
            _build_project_image(proj, project_dir)
            primed.add(key)

    def add(rec):
        with results_lock:
            results.append(rec)
            json.dump(_summarize(model, results), open(out_path, "w"), indent=2)

    def work(t):
        proj, sha, test = t["project"], t["sha_detected"], t["test_name"]
        directory = _test_dir(test)
        inst = f"r{next(counter)}"                        # unique container per test
        rec = {"project": proj, "test_name": test, "flaky_type": t["flaky_type"]}
        try:
            prime(proj, sha, t["project_url"])
            tools, repair_log, last_verified = make_repair_tools(
                t["project_url"], sha, test, t["execution_profiles"], t["flaky_type"],
                instance=inst)
            container = _repo_container_name(proj, sha, inst)
        except Exception as e:
            rec["status"] = "setup_failed"; rec["error"] = str(e)[:200]
            add(rec); print(f"  [setup-fail] {model} {proj}::{test}"); return
        try:
            base = [r for r in (run_suite(container, directory, s, args.timeout) for s in seeds) if r]
            stable = stably_passing(base) if base else None
            if not stable:
                rec["status"] = "no_baseline"; add(rec)
                print(f"  [no-baseline] {model} {proj}::{test}"); return

            llm = model_cfg.make_llm()
            agent = create_react_agent(llm, tools)
            kwargs = {"messages": [SystemMessage(content=_SYSTEM_PROMPT),
                                   HumanMessage(content=build_prompt(t))]}
            try:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(agent.invoke, kwargs, {"recursion_limit": 60})
                    res = fut.result(timeout=REPAIR_TIMEOUT_SECONDS)
                final_content = res["messages"][-1].content
            except FutureTimeoutError:
                rec["status"] = "repair_timeout"; add(rec)
                print(f"  [repair-timeout] {model} {proj}::{test}"); return

            parsed = _parse_repair_response(final_content)
            # MiniMax XML fallback — same as the pipeline.
            if (not parsed.get("files_modified") and "<invoke" in final_content
                    and "write_file" in final_content):
                fb = _apply_xml_fallback(final_content, test, container,
                                         t["flaky_type"], t["execution_profiles"], repair_log)
                if fb:
                    parsed = fb

            is_fixed = parsed.get("is_fixed", False)
            if last_verified:
                is_fixed = last_verified[-1]

            rec["is_fixed"] = bool(is_fixed)
            rec["files_modified"] = parsed.get("files_modified", [])
            rec["patch_target"] = parsed.get("patch_target")
            rec["patch"] = capture_diff(container)          # FAITHFUL, from container
            rec["patch_reported"] = parsed.get("diff") or ""  # model's transcription, for comparison

            post = [r for r in (run_suite(container, directory, s, args.timeout) for s in seeds) if r]
            if not post:
                rec["status"] = "no_post"; add(rec)
                print(f"  [no-post] {model} {proj}::{test}"); return

            broken, removed = regressions_of(stable, post, test)
            rec["status"] = "regression" if (broken or removed) else "clean"
            rec["baseline_stable"] = len(stable)
            rec["regressions_broken"] = broken
            rec["regressions_removed"] = removed
            tag = (f"REGRESSION broke={len(broken)} removed={len(removed)}"
                   if (broken or removed) else "clean")
            add(rec)
            print(f"  [{tag}] fixed={rec['is_fixed']} patch={len(rec['patch'])}b "
                  f"{model} {proj}::{test}")
        finally:
            cleanup_project_container(proj, sha, inst)

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, args.threads)) as pool:
            list(pool.map(work, todo))

    summary = _summarize(model, results)
    json.dump(summary, open(out_path, "w"), indent=2)
    print(f"[{model}] checked={summary['checked']} re_fixed={summary['re_fixed']} "
          f"regressions={summary['regressions']} rate={summary['regression_rate']} "
          f"skipped={summary['skipped']}")
    print(f"[{model}] report: {out_path}\n")
    return summary


def main():
    ap = argparse.ArgumentParser(description="Re-run repair with faithful patches + regression.")
    ap.add_argument("--model", default="", help="Only this model (minimax|fireworks|gpt-oss).")
    ap.add_argument("--sample", type=int, default=50, help="Tests per model (0 = all fixed).")
    ap.add_argument("--baseline-runs", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    url_map = load_url_map(MERGED_CSV)
    pairs = [(s, m) for s, m in SESSIONS if not args.model or m == args.model]
    if not pairs:
        ap.error(f"unknown model {args.model!r}; choose from {[m for _, m in SESSIONS]}")

    for session_dir, model in pairs:
        if not os.path.isdir(session_dir):
            print(f"[{model}] session missing: {session_dir}"); continue
        run_model(model, session_dir, url_map, args)


if __name__ == "__main__":
    main()
