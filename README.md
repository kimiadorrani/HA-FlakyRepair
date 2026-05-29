# HA-FlakyRepair

This repository contains the code and resources for HA-FlakyRepair.

## Quick Start

### 1. Requirements
- Python 3.10+
- Docker
- (Optional but recommended) Virtual Environment

### 2. Setup
Install the necessary dependencies using `pip`:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Dataset Layout

- `datasets/idoft/`: Python flaky-test dataset inputs and reproduction outputs
- `datasets/idoft/raw/`: raw input CSVs (`py-data.csv` for non-OD, `py-data-od.csv` for OD)
- `datasets/idoft/preprocessed/`: cleaned and filtered CSVs
- `workspaces/idoft/`: cloned Python repositories for IDOFT

## IDOFT

IDOFT contains two independent test categories handled by separate runners:

- **Non-OD** (NIO, NOD): tests that are flaky due to non-determinism or test-order independence — reproduced by repeated execution.
- **OD** (OD-VIC, OD-BRIT): order-dependent tests that require a specific test execution order to expose the flakiness.

There are two ways to run IDOFT tests:

1. **Standalone batch runner** (`scripts/idoft/`) — Docker-container-per-project approach. Good for fresh reproduction passes and retrying failures.
2. **LangGraph pipeline** (`src/`) — adds LLM-based detection on top of reproduction.

---

### Non-OD Tests

#### End-to-End

```bash
# Clone any missing repos, then reproduce all non-OD tests
.venv/bin/python -m src.data.idoft.preprocess.fetch_workspaces
PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_batch.py --iterations 10
```

Results are written incrementally to:

```text
datasets/idoft/idoft-reproduction-results.csv
results/idoft/*.json
```

#### Step-by-Step

##### 1. Clone Repositories

```bash
.venv/bin/python -m src.data.idoft.preprocess.fetch_workspaces
```

Repos are cloned into `workspaces/idoft/<repo_name>`.

##### 2. Run One Test

```bash
.venv/bin/python scripts/idoft/run_idoft_test.py --row-index 0 --iterations 10
```

Use `--input-csv` to point at a different CSV (default: `datasets/idoft/raw/py-data.csv`).

##### 3. Run The Full Batch

```bash
PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_batch.py --iterations 10
```

Options:

```bash
# Only one project
.venv/bin/python scripts/idoft/run_idoft_batch.py --project webssh --iterations 10

# Retry rows that previously errored or could not be reproduced
.venv/bin/python scripts/idoft/run_idoft_batch.py \
    --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv \
    --retry-statuses execution_error could_not_reproduce \
    --iterations 10

# Limit for quick debugging
.venv/bin/python scripts/idoft/run_idoft_batch.py --limit 5

# Parallel workers (default 2)
.venv/bin/python scripts/idoft/run_idoft_batch.py --workers 4 --iterations 10

# Retry rows that have a specific Status in the results CSV
.venv/bin/python scripts/idoft/run_idoft_batch.py --retry-result-status error always_pass
```

The batch runner is **resumable**: already-recorded `(Project URL, Test Name)` pairs are skipped automatically. Results accumulate incrementally.

---

### OD Tests

OD tests are handled by a separate runner (`scripts/idoft/run_idoft_od_batch.py`) that uses the same Docker-per-project pattern but applies a three-step reproduction strategy:

**OD-BRIT (brittle)** — test fails in isolation because it needs state set by a prior "setter" test:
1. Run in isolation → if FAIL → `brit_confirmed`
2. (Fallback) Run whole file in natural order → should PASS

**OD-VIC (victim)** — test passes in isolation but fails after a "polluter" runs:
1. Run in isolation → should PASS (baseline)
2. Run all tests in file with target placed last (`target-last`) → if FAIL → `vic_confirmed`
3. Fallback: N random-seed file runs (`--randomly-seed=i`)

**OD (generic)**: BRIT check first, then VIC strategy.

#### End-to-End

```bash
PYTHONUNBUFFERED=1 .venv/bin/python scripts/idoft/run_idoft_od_batch.py --iterations 20 --workers 2
```

Results are written incrementally to:

```text
datasets/idoft/idoft-od-reproduction-results.csv
results/idoft-od/*.json
```

#### Run One OD Test

```bash
.venv/bin/python scripts/idoft/run_idoft_od_test.py --row-index 0 --iterations 20
```

#### Batch Options

```bash
# Only one project
.venv/bin/python scripts/idoft/run_idoft_od_batch.py --project bottle-neck --iterations 20

# Only OD-VIC rows
.venv/bin/python scripts/idoft/run_idoft_od_batch.py --category OD-VIC --iterations 30

# Only OD-BRIT rows
.venv/bin/python scripts/idoft/run_idoft_od_batch.py --category OD-BRIT --iterations 20

# Retry rows with a specific status
.venv/bin/python scripts/idoft/run_idoft_od_batch.py --retry-result-status error could_not_reproduce

# Ignore existing results and re-run everything
.venv/bin/python scripts/idoft/run_idoft_od_batch.py --no-resume --iterations 20

# Limit for quick debugging
.venv/bin/python scripts/idoft/run_idoft_od_batch.py --limit 10
```

The OD batch runner is **resumable** and **accumulative**: rows already recorded in the results CSV are skipped. Docker containers are created once per project, all OD tests for that project are run, then the container is removed.

#### OD Result Fields

| Field | Description |
|---|---|
| `Isolated Result` | `pass` / `fail` / `error` for the isolated run |
| `Target Last Result` | `pass` / `fail` / `unknown` for the target-last run |
| `Random Pass Count` | Target passes across random-seed file runs |
| `Random Fail Count` | Target fails across random-seed file runs |
| `Reproduced` | `true` if flakiness was confirmed |
| `Status` | `brit_confirmed` / `vic_confirmed` / `could_not_reproduce` / `error` |

#### OD Reproduction Results (full batch)

| Status | Count |
|---|---|
| `brit_confirmed` | 319 |
| `vic_confirmed` | 263 |
| `could_not_reproduce` | 556 |
| **Total reproduced** | **582 / 1138 (51.1%)** |

By category:
- **OD-BRIT** (321 rows): 319/321 reproduced — **99.4%** (isolated=FAIL is a clean signal)
- **OD-VIC** (784 rows): 250/784 reproduced — **31.9%** (polluter often lives in a different file)
- **OD generic** (33 rows): 13/33 reproduced — **39.4%**

---

### LangGraph Detection + Repair Pipeline

The main pipeline runs two LangGraph agents in sequence:

1. **Detection Agent** — reproduces flakiness inside Docker and classifies the root cause (NIO, NOD, OD-Vic, OD-Brit).
2. **Repair Agent** — reads the root cause, navigates the repo, writes a fix, and verifies it — all inside the container. The host workspace is never modified. Repair is capped at `REPAIR_TIMEOUT_SECONDS` (default **800s**); tests that exceed this limit are marked `is_fixed: false` with a timeout summary and skipped without blocking the rest of the run.

```
START → detection_agent → (reproduced?) → repair_agent → END
                        ↘ (not reproduced) → END
```

#### End-to-End

```bash
.venv/bin/python -m src.preprocess_dataset
.venv/bin/python -m src.main
```

#### Common Invocations

```bash
# Single project — full pipeline
.venv/bin/python -m src.main --project plcx

# Multiple projects at once
.venv/bin/python -m src.main --project plcx yamicache multiindex

# Detection only (skip repair)
.venv/bin/python -m src.main --no-repair

# Different models for each agent
.venv/bin/python -m src.main --detection-model minimax --repair-model gpt-4o

# Filter by flaky category
.venv/bin/python -m src.main --category NIO

# Limit to N projects
.venv/bin/python -m src.main --limit 5

# Include tests not marked as reproduced in the dataset
.venv/bin/python -m src.main --include-not-reproduced
```

#### Run Repair on Existing Detection Results

If you already have detection results from a previous session, you can run the repair agent directly without re-running detection:

```bash
.venv/bin/python -m src.main --from-detection results/2026-05-05_09-52-56

# Limit to specific projects from that session
.venv/bin/python -m src.main --from-detection results/2026-05-05_09-52-56 --project plcx

# You can also pass just the session ID
.venv/bin/python -m src.main --from-detection 2026-05-05_09-52-56
```

Repair results are written into the same session directory under `repair/`, alongside the existing `detection/` output.

#### Result Directory Layout

```
results/
  2026-05-05_09-52-56/
    detection/
      plcx.json            detection output per project
      yamicache.json
      ...
    repair/
      plcx.json            repair output per project (only if repair ran)
      ...
    traces/
      detection/
        plcx.jsonl         one JSON line per test — detection agent spans
      repair/
        plcx.jsonl         one JSON line per test — repair agent spans
    summary.json           session-wide aggregates (detection + repair counts)
```

**Detection JSON fields** (per test):

| Field | Description |
|---|---|
| `is_flakiness_reproduced` | `true` if the agent confirmed flakiness |
| `flaky_type` | Detected category: `NIO`, `NOD`, `OD-Vic`, `OD-Brit` |
| `root_cause_analysis` | Agent's explanation of why the test is flaky |
| `failing_log` | Captured output from a failing run |
| `execution_profiles` | Compact record of which strategies were run (used by repair for replay) |

**Repair JSON fields** (per test):

| Field | Description |
|---|---|
| `is_fixed` | `true` if verification confirmed the fix eliminates flakiness |
| `patch_target` | `source`, `test`, or `both` |
| `files_modified` | Repo-relative paths of changed files |
| `fix_summary` | One-line description of what was changed and why |
| `fix_attempts` | Number of write→verify iterations used |
| `patch` | Unified diff of all changes (from `git diff` inside the container) |
| `repair_error` | Non-null if setup failed or the agent was timed out |

#### CLI Reference

| Flag | Default | Description |
|---|---|---|
| `--project NAME [...]` | all | Filter to one or more projects |
| `--category CAT` | all | Filter by flaky type (NIO, NOD, OD-Vic, OD-Brit) |
| `--limit N` | none | Stop after N projects |
| `--detection-model KEY` | `minimax` | Model for the Detection Agent |
| `--repair-model KEY` | `minimax` | Model for the Repair Agent |
| `--no-repair` | off | Run detection only |
| `--from-detection DIR` | — | Skip detection; repair from saved session |
| `--include-not-reproduced` | off | Include dataset rows not marked as reproduced |
| `--shuffle` | off | Randomise test order before applying `--limit` |
| `--input-csv PATH` | merged CSV | Alternative dataset CSV |

Available model keys are defined in `models.json` at the project root.

---

### Notes

- OD rows are skipped by default in the non-OD batch runner (`run_idoft_batch.py`). Use the dedicated OD runner for those.
- Both standalone runners mount each repo as a Docker volume and reuse one container per project — install dependencies once, run all tests, then remove the container.
- Both runners use targeted `git fetch origin <sha>` for shallow clones instead of `git fetch --all`, which avoids the most common checkout failure.
- The LangGraph pipeline builds a per-project Docker image (heavier, but bakes in all deps). The image is reused across detection and repair for the same project.
- The repair agent writes all fixes inside the Docker container. The `workspaces/idoft/` directory on the host is never modified.
- Repair has a hard wall-clock timeout of **800 seconds** per test (configurable via `REPAIR_TIMEOUT_SECONDS` in `src/agents/repair.py`). Tests that time out are logged as not fixed and the run continues normally.

---

### Evaluation

After any session completes, run the evaluation script to compute all defined metrics from the saved JSON files.

```bash
# Full report (table printed + evaluation.json saved next to detection/ and repair/)
.venv/bin/python scripts/shared/evaluate_session.py --session 2026-05-05_09-52-56

# Detection metrics only
.venv/bin/python scripts/shared/evaluate_session.py --session 2026-05-05_09-52-56 --no-repair

# JSON output only (no console table)
.venv/bin/python scripts/shared/evaluate_session.py --session 2026-05-05_09-52-56 --format json

# Compare two sessions side-by-side (e.g. MiniMax vs DeepSeek)
.venv/bin/python scripts/shared/evaluate_session.py --session A --compare B
```

The script writes `results/<session>/evaluation.json` and, when `--compare` is used, prints a side-by-side comparison table.

#### Detection metrics

| Metric | Description |
|---|---|
| Reproduction rate | % of tests where the agent confirmed flaky behaviour |
| Accuracy | % correct category predictions, among reproduced tests |
| Precision / Recall / F1 | Per class (NIO, NOD, OD-Vic, OD-Brit) |
| Confusion matrix | Actual vs predicted category counts |
| Avg / total tokens | Input + output tokens per test and for the whole session |
| Avg LLM calls | LLM invocations per test |
| Avg / total duration | Wall-clock time per test and for the whole session |

#### Repair metrics

| Metric | Description |
|---|---|
| Fix rate | % of attempted repairs that verified as fixed |
| Regression rate | % of fixed tests that broke other tests (`is_regression` field) |
| Avg fix attempts | Iterations used per repair (fixed-only and across all attempts) |
| Patch target distribution | Share of patches targeting `test` / `source` / `both` |
| Lines added / removed | Avg and total diff lines across all repairs |
| Avg / total tokens | Input + output tokens per repair and for the whole session |
| Avg LLM calls | LLM invocations per repair |
| Avg / total duration | Wall-clock time per repair and for the whole session |

#### CLI reference

| Flag | Default | Description |
|---|---|---|
| `--session SESSION_ID` | — | Session to evaluate (required) |
| `--compare SESSION_ID [...]` | — | Additional sessions for side-by-side comparison |
| `--no-repair` | off | Skip repair metrics |
| `--format table\|json\|both` | `both` | Output format |
| `--output PATH` | `results/<session>/evaluation.json` | Custom output path |

---

## Documentation
- Main Architecture overview: `docs/Architecture.md`
- Dataset notes: `docs/README_IDoFT.md`
- Mermaid diagrams: `docs/diagrams/`
