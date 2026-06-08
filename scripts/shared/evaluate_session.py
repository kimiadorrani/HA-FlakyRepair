#!/usr/bin/env python3
"""
Evaluate a HA-FlakyRepair pipeline session.

Reads saved detection/ and repair/ JSON files from a results session directory
and computes all defined metrics. Can compare multiple sessions side by side.

Detection metrics
  - Reproduction rate
  - Overall accuracy (among reproduced tests, correct category)
  - Per-class precision / recall / F1 (NIO, NOD, OD-Vic, OD-Brit)
  - Confusion matrix
  - Token rate  (avg input / output / total tokens per test)
  - LLM calls per test
  - Duration per test

Repair metrics
  - Fix rate
  - Regression rate  (requires is_regression field in results; 0 if absent)
  - Avg fix attempts (fixed only, and overall)
  - Lines of code changed  (added / removed from patch diff)
  - Patch target distribution  (test / source / both)
  - Token rate  (avg tokens per repair)
  - LLM calls per repair
  - Duration per repair

Usage
  python scripts/shared/evaluate_session.py --session 2026-05-05_09-52-56
  python scripts/shared/evaluate_session.py --session A --compare B C
  python scripts/shared/evaluate_session.py --session A --format json
  python scripts/shared/evaluate_session.py --session A --no-repair
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR    = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT_DIR / "results"

# ── Category normalisation ────────────────────────────────────────────────────

_CANONICAL: dict[str, str] = {
    "NIO":      "NIO",
    "NOD":      "NOD",
    "OD-VIC":   "OD-Vic",
    "OD-BRIT":  "OD-Brit",
    "OD":       "OD-Vic",   # generic OD treated as Victim
}
ALL_CLASSES = ["NIO", "NOD", "OD-Vic", "OD-Brit"]


def _norm_category(raw: str | list | None) -> str:
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    text = (raw or "").strip().upper()
    return _CANONICAL.get(text, text or "UNKNOWN")


def _norm_predicted(raw: str | None) -> str:
    """Normalise an agent flaky_type string to canonical form."""
    text = (raw or "").strip().upper()
    if "NIO" in text:
        return "NIO"
    if "NOD" in text:
        return "NOD"
    if "OD-VIC" in text or text == "OD":
        return "OD-Vic"
    if "OD-BRIT" in text:
        return "OD-Brit"
    if "NOT FLAKY" in text or "NOT_FLAKY" in text:
        return "NOT_FLAKY"
    return text or "UNKNOWN"


# ── Patch diff helpers ────────────────────────────────────────────────────────

def _count_diff_lines(patch: str | None) -> tuple[int, int]:
    """Return (lines_added, lines_removed) from a unified diff string."""
    if not patch:
        return 0, 0
    added = removed = 0
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


# ── Token / timing extraction ─────────────────────────────────────────────────

def _get_token_usage(test_record: dict) -> dict:
    """
    Return token_usage dict for a test record.
    Repair JSON stores token_usage inside trajectory[0] rather than at top level.
    """
    top = test_record.get("token_usage") or {}
    if top.get("total_tokens"):
        return top
    traj = test_record.get("trajectory") or []
    if traj:
        return traj[0].get("token_usage") or {}
    return {}


def _get_duration(test_record: dict) -> float:
    traj = test_record.get("trajectory") or []
    if traj:
        return float(traj[0].get("duration_seconds") or 0)
    return 0.0


def _get_model(test_record: dict) -> str:
    tok = _get_token_usage(test_record)
    if tok.get("model"):
        return tok["model"]
    traj = test_record.get("trajectory") or []
    if traj:
        return traj[0].get("model") or ""
    return ""


# ── Load session data ─────────────────────────────────────────────────────────

def _load_phase(session_dir: Path, phase: str) -> list[dict]:
    """Load all test records from detection/ or repair/ sub-directory."""
    phase_dir = session_dir / phase
    if not phase_dir.is_dir():
        return []
    records: list[dict] = []
    for jf in sorted(phase_dir.glob("*.json")):
        if jf.name == "summary.json":
            continue
        with jf.open(encoding="utf-8") as fh:
            data = json.load(fh)
        project = data.get("project", jf.stem)
        for t in data.get("tests", []):
            t.setdefault("_project", project)
            records.append(t)
    return records


# ── Detection metrics ─────────────────────────────────────────────────────────

def compute_detection_metrics(records: list[dict]) -> dict[str, Any]:
    total        = len(records)
    reproduced   = [r for r in records if r.get("is_flakiness_reproduced")]
    not_reproduced = [r for r in records if not r.get("is_flakiness_reproduced")]

    # Build (actual, predicted) pairs — only for reproduced tests
    pairs: list[tuple[str, str]] = []
    for r in reproduced:
        actual    = _norm_category(r.get("category"))
        predicted = _norm_predicted(r.get("flaky_type"))
        pairs.append((actual, predicted))

    # Confusion matrix over reproduced tests
    classes = sorted(
        set(a for a, _ in pairs) | set(p for _, p in pairs) | set(ALL_CLASSES)
    )
    cm: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for actual, predicted in pairs:
        cm[actual][predicted] += 1

    # Per-class P / R / F1
    per_class: dict[str, Any] = {}
    correct = 0
    for cls in classes:
        tp = cm[cls][cls]
        fp = sum(cm[other][cls] for other in classes if other != cls)
        fn = sum(cm[cls][other] for other in classes if other != cls)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec  = tp / (tp + fn) if (tp + fn) else 0.0
        f1   = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        per_class[cls] = {
            "support":   tp + fn,
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(prec, 4),
            "recall":    round(rec,  4),
            "f1":        round(f1,   4),
        }
        correct += tp

    accuracy = correct / len(pairs) if pairs else 0.0

    # Per-class reproduction rates (over all records, not just reproduced)
    repro_by_class: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "reproduced": 0})
    for r in records:
        cls = _norm_category(r.get("category"))
        repro_by_class[cls]["total"] += 1
        if r.get("is_flakiness_reproduced"):
            repro_by_class[cls]["reproduced"] += 1
    reproduction_by_class = {
        cls: {
            "total":      v["total"],
            "reproduced": v["reproduced"],
            "rate":       round(v["reproduced"] / v["total"], 4) if v["total"] else 0.0,
        }
        for cls, v in repro_by_class.items()
    }

    # Token / timing aggregates
    in_toks  = [_get_token_usage(r).get("input_tokens",  0) for r in records]
    out_toks = [_get_token_usage(r).get("output_tokens", 0) for r in records]
    tot_toks = [_get_token_usage(r).get("total_tokens",  0) for r in records]
    calls    = [_get_token_usage(r).get("llm_calls", 0)     for r in records]
    durs     = [_get_duration(r)                             for r in records]

    def _avg(lst: list) -> float:
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    models = list({_get_model(r) for r in records if _get_model(r)})

    return {
        "model":              models[0] if len(models) == 1 else models,
        "total":              total,
        "reproduced":         len(reproduced),
        "not_reproduced":     len(not_reproduced),
        "reproduction_rate":  round(len(reproduced) / total, 4) if total else 0.0,
        "accuracy":               round(accuracy, 4),
        "correct":                correct,
        "reproduction_by_class":  reproduction_by_class,
        "per_class":              per_class,
        "confusion_matrix":   {k: dict(v) for k, v in cm.items()},
        "token_usage": {
            "avg_input_tokens":  _avg(in_toks),
            "avg_output_tokens": _avg(out_toks),
            "avg_total_tokens":  _avg(tot_toks),
            "total_input_tokens":  sum(in_toks),
            "total_output_tokens": sum(out_toks),
            "total_tokens":        sum(tot_toks),
            "avg_llm_calls":     _avg(calls),
        },
        "timing": {
            "avg_duration_seconds":   _avg(durs),
            "total_duration_seconds": round(sum(durs), 2),
        },
    }


# ── Repair metrics ────────────────────────────────────────────────────────────

def compute_repair_metrics(records: list[dict]) -> dict[str, Any]:
    attempted = len(records)
    fixed     = [r for r in records if r.get("is_fixed")]
    not_fixed = [r for r in records if not r.get("is_fixed")]

    # Regression: use is_regression field if present (not yet persisted by pipeline)
    regressions = sum(1 for r in fixed if r.get("is_regression"))

    # Attempts
    attempts_fixed = [r.get("fix_attempts", 0) for r in fixed]
    attempts_all   = [r.get("fix_attempts", 0) for r in records]

    # Patch target distribution
    target_counts: dict[str, int] = defaultdict(int)
    for r in records:
        t = (r.get("patch_target") or "unknown").lower()
        target_counts[t] += 1

    # Lines changed
    lines_added_all = []
    lines_removed_all = []
    for r in records:
        a, rem = _count_diff_lines(r.get("patch"))
        lines_added_all.append(a)
        lines_removed_all.append(rem)

    # Token / timing
    in_toks  = [_get_token_usage(r).get("input_tokens",  0) for r in records]
    out_toks = [_get_token_usage(r).get("output_tokens", 0) for r in records]
    tot_toks = [_get_token_usage(r).get("total_tokens",  0) for r in records]
    calls    = [_get_token_usage(r).get("llm_calls", 0)     for r in records]
    durs     = [_get_duration(r)                             for r in records]

    def _avg(lst: list) -> float:
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    models = list({_get_model(r) for r in records if _get_model(r)})

    target_dist: dict[str, Any] = {}
    for tgt, cnt in sorted(target_counts.items()):
        target_dist[tgt] = {
            "count": cnt,
            "pct":   round(cnt / attempted, 4) if attempted else 0.0,
        }

    return {
        "model":            models[0] if len(models) == 1 else models,
        "attempted":        attempted,
        "fixed":            len(fixed),
        "not_fixed":        len(not_fixed),
        "fix_rate":         round(len(fixed) / attempted, 4) if attempted else 0.0,
        "regression_count": regressions,
        "regression_rate":  round(regressions / len(fixed), 4) if fixed else 0.0,
        "avg_attempts_fixed": _avg(attempts_fixed),
        "avg_attempts_all":   _avg(attempts_all),
        "patch_target": target_dist,
        "lines_changed": {
            "avg_added":     _avg(lines_added_all),
            "avg_removed":   _avg(lines_removed_all),
            "total_added":   sum(lines_added_all),
            "total_removed": sum(lines_removed_all),
        },
        "token_usage": {
            "avg_input_tokens":  _avg(in_toks),
            "avg_output_tokens": _avg(out_toks),
            "avg_total_tokens":  _avg(tot_toks),
            "total_input_tokens":  sum(in_toks),
            "total_output_tokens": sum(out_toks),
            "total_tokens":        sum(tot_toks),
            "avg_llm_calls":     _avg(calls),
        },
        "timing": {
            "avg_duration_seconds":   _avg(durs),
            "total_duration_seconds": round(sum(durs), 2),
        },
    }


# ── Full session evaluation ───────────────────────────────────────────────────

def evaluate_session(session_id: str, include_repair: bool = True) -> dict[str, Any]:
    session_dir = RESULTS_DIR / session_id
    if not session_dir.is_dir():
        raise FileNotFoundError(f"Session directory not found: {session_dir}")

    det_records  = _load_phase(session_dir, "detection")
    rep_records  = _load_phase(session_dir, "repair") if include_repair else []

    result: dict[str, Any] = {
        "session_id":   session_id,
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "detection":    compute_detection_metrics(det_records) if det_records else {},
    }
    if include_repair and rep_records:
        result["repair"] = compute_repair_metrics(rep_records)
    elif include_repair:
        result["repair"] = {}

    return result


# ── Pretty-print helpers ──────────────────────────────────────────────────────

def _bar(value: float, width: int = 20, char: str = "█") -> str:
    filled = int(round(value * width))
    return char * filled + "░" * (width - filled)


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def _print_detection(det: dict, session_id: str) -> None:
    W = 72
    print("=" * W)
    print(f"  DETECTION  —  {session_id}")
    print("=" * W)
    model = det.get("model", "?")
    print(f"  Model         : {model}")
    print(f"  Tests run     : {det['total']}")
    print(f"  Reproduced    : {det['reproduced']}  /  {det['total']}  "
          f"({_pct(det['reproduction_rate'])})")
    print(f"  Accuracy      : {_pct(det['accuracy'])}  "
          f"({det['correct']} / {det['reproduced']} reproduced correct)")

    rbc = det.get("reproduction_by_class", {})
    if rbc:
        print()
        print(f"  {'Class':<10}  {'Reproduced':>10}  {'Total':>7}  {'Rate':>7}")
        print(f"  {'-'*10}  {'-'*10}  {'-'*7}  {'-'*7}")
        for cls in ["NIO", "NOD", "OD-Vic", "OD-Brit", "OD"]:
            if cls not in rbc:
                continue
            info = rbc[cls]
            print(f"  {cls:<10}  {info['reproduced']:>10}  {info['total']:>7}  "
                  f"{_pct(info['rate']):>7}")

    print()
    print(f"  {'Class':<10}  {'Support':>7}  {'Prec':>6}  {'Recall':>6}  {'F1':>6}")
    print(f"  {'-'*10}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*6}")
    for cls in ALL_CLASSES:
        pc = det.get("per_class", {}).get(cls, {})
        if not pc:
            continue
        print(f"  {cls:<10}  {pc['support']:>7}  {pc['precision']:>6.3f}  "
              f"{pc['recall']:>6.3f}  {pc['f1']:>6.3f}")

    # Confusion matrix
    cm = det.get("confusion_matrix", {})
    if cm:
        active_classes = [c for c in ALL_CLASSES if c in cm]
        if active_classes:
            print()
            print("  Confusion matrix  (rows=actual, cols=predicted)")
            header_classes = active_classes + [c for c in cm if c not in active_classes]
            col_w = max(len(c) for c in header_classes) + 1
            print("  " + " " * 10 + "".join(f"{c:>{col_w}}" for c in header_classes))
            for actual in header_classes:
                row = cm.get(actual, {})
                cells = "".join(f"{row.get(pred, 0):>{col_w}}" for pred in header_classes)
                print(f"  {actual:<10}{cells}")

    tok = det.get("token_usage", {})
    tim = det.get("timing", {})
    print()
    print(f"  Avg tokens/test : {tok.get('avg_total_tokens', 0):>8,.0f}  "
          f"(in {tok.get('avg_input_tokens', 0):,.0f}  "
          f"/ out {tok.get('avg_output_tokens', 0):,.0f})")
    print(f"  Total tokens    : {tok.get('total_tokens', 0):>8,}")
    print(f"  Avg LLM calls   : {tok.get('avg_llm_calls', 0):>8.1f}")
    print(f"  Avg duration    : {tim.get('avg_duration_seconds', 0):>8.1f}s")
    print(f"  Total duration  : {tim.get('total_duration_seconds', 0):>8.1f}s  "
          f"({tim.get('total_duration_seconds', 0) / 60:.1f} min)")


def _print_repair(rep: dict, session_id: str) -> None:
    W = 72
    print()
    print("=" * W)
    print(f"  REPAIR  —  {session_id}")
    print("=" * W)
    model = rep.get("model", "?")
    print(f"  Model           : {model}")
    print(f"  Attempted       : {rep['attempted']}")
    print(f"  Fixed           : {rep['fixed']}  /  {rep['attempted']}  "
          f"({_pct(rep['fix_rate'])})")
    print(f"  Not fixed       : {rep['not_fixed']}")
    print(f"  Regressions     : {rep['regression_count']}  "
          f"({_pct(rep['regression_rate'])}  of fixed)")
    print(f"  Avg attempts    : {rep['avg_attempts_all']:.1f} all  /  "
          f"{rep['avg_attempts_fixed']:.1f} fixed-only")

    # Patch target distribution
    tgt = rep.get("patch_target", {})
    if tgt:
        print()
        print("  Patch target distribution:")
        for name, info in sorted(tgt.items()):
            bar = _bar(info["pct"], width=15)
            print(f"    {name:<8}  {info['count']:>3}  {_pct(info['pct'])}  {bar}")

    lc = rep.get("lines_changed", {})
    print()
    print(f"  Avg lines added   : {lc.get('avg_added', 0):>6.1f}  "
          f"(total {lc.get('total_added', 0)})")
    print(f"  Avg lines removed : {lc.get('avg_removed', 0):>6.1f}  "
          f"(total {lc.get('total_removed', 0)})")

    tok = rep.get("token_usage", {})
    tim = rep.get("timing", {})
    print()
    print(f"  Avg tokens/repair : {tok.get('avg_total_tokens', 0):>8,.0f}  "
          f"(in {tok.get('avg_input_tokens', 0):,.0f}  "
          f"/ out {tok.get('avg_output_tokens', 0):,.0f})")
    print(f"  Total tokens      : {tok.get('total_tokens', 0):>8,}")
    print(f"  Avg LLM calls     : {tok.get('avg_llm_calls', 0):>8.1f}")
    print(f"  Avg duration      : {tim.get('avg_duration_seconds', 0):>8.1f}s")
    print(f"  Total duration    : {tim.get('total_duration_seconds', 0):>8.1f}s  "
          f"({tim.get('total_duration_seconds', 0) / 60:.1f} min)")


def _print_comparison(sessions: list[dict]) -> None:
    """Side-by-side comparison table for multiple sessions."""
    W = 72
    ids = [s["session_id"] for s in sessions]
    print()
    print("=" * W)
    print("  SESSION COMPARISON")
    print("=" * W)

    col = 16
    header = f"  {'Metric':<28}" + "".join(f"{sid[:col]:>{col}}" for sid in ids)
    print(header)
    print("  " + "-" * (28 + col * len(ids)))

    def _row(label: str, values: list) -> None:
        cells = "".join(f"{str(v):>{col}}" for v in values)
        print(f"  {label:<28}{cells}")

    # Detection rows
    print(f"\n  [Detection]")
    _row("Tests run",         [s["detection"].get("total", "-")          for s in sessions])
    _row("Reproduced",        [s["detection"].get("reproduced", "-")     for s in sessions])
    _row("Repro rate",        [_pct(s["detection"].get("reproduction_rate", 0)) for s in sessions])
    _row("Accuracy",          [_pct(s["detection"].get("accuracy", 0))   for s in sessions])
    for cls in ALL_CLASSES:
        pc_vals = [s["detection"].get("per_class", {}).get(cls, {}).get("f1", "-") for s in sessions]
        _row(f"  F1({cls})",  [f"{v:.3f}" if isinstance(v, float) else v for v in pc_vals])
    _row("Avg tokens",        [f"{s['detection'].get('token_usage', {}).get('avg_total_tokens', 0):,.0f}" for s in sessions])
    _row("Avg duration (s)",  [f"{s['detection'].get('timing', {}).get('avg_duration_seconds', 0):.1f}" for s in sessions])

    # Repair rows (if present)
    if any(s.get("repair") for s in sessions):
        print(f"\n  [Repair]")
        _row("Attempted",         [s.get("repair", {}).get("attempted", "-")  for s in sessions])
        _row("Fixed",             [s.get("repair", {}).get("fixed", "-")      for s in sessions])
        _row("Fix rate",          [_pct(s.get("repair", {}).get("fix_rate", 0)) for s in sessions])
        _row("Regression rate",   [_pct(s.get("repair", {}).get("regression_rate", 0)) for s in sessions])
        _row("Avg attempts",      [f"{s.get('repair', {}).get('avg_attempts_all', 0):.1f}" for s in sessions])
        _row("Avg lines added",   [f"{s.get('repair', {}).get('lines_changed', {}).get('avg_added', 0):.1f}" for s in sessions])
        _row("Avg tokens",        [f"{s.get('repair', {}).get('token_usage', {}).get('avg_total_tokens', 0):,.0f}" for s in sessions])
        _row("Avg duration (s)",  [f"{s.get('repair', {}).get('timing', {}).get('avg_duration_seconds', 0):.1f}" for s in sessions])


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate a HA-FlakyRepair pipeline session.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--session", "-s", required=True,
                        help="Primary session ID (e.g. 2026-05-05_09-52-56)")
    parser.add_argument("--compare", "-c", nargs="+", metavar="SESSION_ID",
                        help="Additional session IDs to compare side-by-side")
    parser.add_argument("--no-repair", action="store_true", default=False,
                        help="Skip repair metrics (detection only)")
    parser.add_argument("--format", choices=["table", "json", "both"], default="both",
                        help="Output format (default: both)")
    parser.add_argument("--output", "-o", metavar="PATH",
                        help="Path to save evaluation JSON "
                             "(default: results/<session>/evaluation.json)")
    return parser.parse_args()


def main() -> int:
    args   = parse_args()
    include_repair = not args.no_repair
    all_session_ids = [args.session] + (args.compare or [])

    evaluations: list[dict] = []
    for sid in all_session_ids:
        try:
            ev = evaluate_session(sid, include_repair=include_repair)
            evaluations.append(ev)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    primary = evaluations[0]

    # ── Print ──
    if args.format in ("table", "both"):
        det = primary.get("detection", {})
        rep = primary.get("repair",    {})

        if det:
            _print_detection(det, primary["session_id"])
        if rep:
            _print_repair(rep, primary["session_id"])

        if len(evaluations) > 1:
            _print_comparison(evaluations)

        print()

    # ── JSON output ──
    if args.format in ("json", "both"):
        payload = primary if len(evaluations) == 1 else {
            "sessions": evaluations,
            "evaluated_at": primary["evaluated_at"],
        }

        output_path = (
            Path(args.output)
            if args.output
            else RESULTS_DIR / primary["session_id"] / "evaluation.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"Evaluation saved to: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
