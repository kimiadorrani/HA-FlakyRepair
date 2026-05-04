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

### LangGraph Detection Pipeline

#### End-to-End

```bash
.venv/bin/python -m src.preprocess_dataset
.venv/bin/python -m src.main --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv
```

This gives you:
- a cleaned preprocessing CSV at `datasets/idoft/preprocessed/py-data-reproducible.csv`
- a preprocessing report at `datasets/idoft/preprocessed/preprocess-report.json`
- detection results under `results/`

#### Step-by-Step

##### 1. Preprocess The Dataset

```bash
.venv/bin/python -m src.preprocess_dataset
```

Outputs:

```text
datasets/idoft/preprocessed/py-data-reproducible.csv
datasets/idoft/preprocessed/preprocess-report.json
```

Each row is annotated with: `Preprocess Status`, `Reproduced`, `Preprocess Error`,
`Selected Profile`, `CPU Limit`, `Memory Limit`, `Pass Count`, `Fail Count`, `Outcome Profile`.

##### 2. Run Detection

```bash
.venv/bin/python -m src.main --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv
```

To include rows that could not be reproduced:

```bash
.venv/bin/python -m src.main --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv --include-not-reproduced
```

##### 3. Filters

```bash
.venv/bin/python -m src.main --project bottle-neck
.venv/bin/python -m src.main --category NOD
.venv/bin/python -m src.main --limit 3
```

Export reproduced tests from an existing results session:

```bash
.venv/bin/python -m src.data.idoft.preprocess.export_reproducible_csv --session 2026-03-24_14-35-48
```

---

### Notes

- OD rows are skipped by default in the non-OD batch runner (`run_idoft_batch.py`). Use the dedicated OD runner for those.
- Both runners mount each repo as a Docker volume and reuse one container per project — install dependencies once, run all tests, then remove the container.
- Both runners use targeted `git fetch origin <sha>` for shallow clones instead of `git fetch --all`, which avoids the most common checkout failure.
- The LangGraph pipeline builds a per-project Docker image (heavier, but bakes in all deps).

## Documentation
- Main Architecture overview: `docs/Architecture.md`
- Dataset notes: `docs/README_IDoFT.md`
- Mermaid diagrams: `docs/diagrams/`
