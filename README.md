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

- `datasets/idoft/`: Python flaky-test dataset inputs and preprocessing outputs
- `datasets/flakycat/`: Java FLAKYCAT dataset inputs and reproduction outputs
- `workspaces/idoft/`: cloned Python repositories for IDOFT
- `workspaces/flakycat/`: cloned Java repositories for FLAKYCAT

## IDOFT

There are two independent ways to run IDOFT tests:

1. **Standalone batch runner** (`scripts/idoft/`) — simple, Docker-container-per-project approach, mirrors the UIFlaky runner. Good for fresh reproduction passes and retrying failures.
2. **LangGraph pipeline** (`src/`) — adds LLM-based detection on top of reproduction. Use this for the full detection experiment.

### Standalone Batch Runner

#### End-to-End (one command)

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

# Parallel workers (default 2, increase if Docker has headroom)
.venv/bin/python scripts/idoft/run_idoft_batch.py --workers 4 --iterations 10
```

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

### Notes
- OD / OD-Vic / OD-Brit rows are skipped by default in both runners.
- The standalone runner mounts each repo as a Docker volume and reuses one container per project, mirroring the UIFlaky runner design.
- The LangGraph pipeline builds a per-project Docker image (heavier, but bakes in all deps).
- Both runners use targeted `git fetch origin <sha>` for shallow clones instead of `git fetch --all`, which avoids the most common checkout failure.

## FLAKYCAT

FLAKYCAT is handled separately from IDOFT. It is a Java dataset, and the
current workflow focuses on reproducing non-order-dependent rows.

### End-to-End

If you want the full FLAKYCAT path in one go, use:

```bash
.venv/bin/python -m src.data.flakycat.preprocess.export_flakycat_metadata
PYTHONUNBUFFERED=1 bash ./scripts/flakycat/run_flakycat_full_batch.sh
```

That flow:
- exports the working CSV
- clones missing repos with timeout and pruning enabled
- runs the non-order-dependent batch
- deletes each repo workspace after its repository batch finishes
- writes incremental reproduction results to `datasets/flakycat/flakycat-reproduction-results.csv`

### Step-by-Step

#### 1. Export The Working CSV

Generate the normalized metadata CSV from the raw replication package:

```bash
.venv/bin/python -m src.data.flakycat.preprocess.export_flakycat_metadata
```

This writes:

```text
datasets/flakycat/flakycat-java-tests.csv
```

#### 2. Clone Repositories

FLAKYCAT repositories are cloned into:

```text
workspaces/flakycat/<owner>__<repo>
```

Use the guarded clone command as the default. It prevents long hangs, records
clone failures, and removes failed repos from the active working CSV after
creating a backup:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces --clone-timeout 180 --prune-failed-from-csv
```

This also writes:

```text
datasets/flakycat/flakycat-clone-failures.csv
datasets/flakycat/flakycat-java-tests.csv.bak
```

#### 3. Verify Workspaces

After cloning, verify that every repo referenced by the active CSV is a real
git checkout:

```bash
.venv/bin/python scripts/flakycat/verify_flakycat_workspaces.py
```

If you ever need to prune missing or invalid repos from the active CSV later:

```bash
.venv/bin/python scripts/flakycat/verify_flakycat_workspaces.py --prune-invalid-from-csv
```

#### 4. Run Reproduction

Run one non-order-dependent Java test repeatedly:

```bash
.venv/bin/python scripts/flakycat/run_flakycat_java_test.py --project fastjson --test Issue1492 --iterations 100
```

Per-test results are written to:

```text
results/flakycat/*.json
```

Run the full non-order-dependent batch:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python scripts/flakycat/run_flakycat_batch.py --iterations 100
```

That writes incremental results to:

```text
datasets/flakycat/flakycat-reproduction-results.csv
```

By default, the batch runner deletes each repo workspace after its repository
batch finishes.

If you want one command that performs guarded cloning first and then runs the
batch:

```bash
PYTHONUNBUFFERED=1 bash ./scripts/flakycat/run_flakycat_full_batch.sh
```

#### 5. Long-Run Mode

The FLAKYCAT batch runner processes rows repository-by-repository. That lets us
reuse the same repo workspace for all selected rows of that project before
moving on.

If you want to keep repo workspaces after the batch for debugging:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python scripts/flakycat/run_flakycat_batch.py --iterations 100 --keep-workspace
```

### Filters And Defaults

- Non-order-dependent rows are the default focus.
- Repo workspaces are deleted after each repository batch by default.
- Order-dependent rows are skipped unless you explicitly add `--include-order-dependent`.
- The current Java reproduction path uses Docker to run Maven or Gradle where possible.
- Some older Java projects may still fail for environmental reasons such as dead repositories, blocked old HTTP artifact sources, or missing legacy dependencies.

## UI-FLAKY

UI-FLAKY is a JavaScript/TypeScript dataset from an ICSE 2021 empirical study on UI-based flaky tests.
The raw dataset is sourced from https://github.com/ui-flaky-test/ui-flaky-test.github.io.

### End-to-End

If you want the full UI-FLAKY path in one go, use:

```bash
PYTHONUNBUFFERED=1 bash ./scripts/uiflaky/run_uiflaky_full_batch.sh
```

That flow:
- exports the working CSV from the raw dataset
- clones missing repos into `workspaces/uiflaky/`
- runs the reproduction batch
- writes incremental reproduction results to `datasets/uiflaky/uiflaky-reproduction-results.csv`

### Step-by-Step

#### 1. Export The Working CSV

Generate the normalized metadata CSV from the raw dataset:

```bash
.venv/bin/python -m src.data.uiflaky.preprocess.export_uiflaky_metadata
```

This reads `datasets/uiflaky/raw/repo/dataset.csv` and writes:

```text
datasets/uiflaky/preprocessed/uiflaky-metadata.csv
```

#### 2. Clone Repositories

UI-FLAKY repositories are cloned into:

```text
workspaces/uiflaky/<owner>__<repo>
```

```bash
.venv/bin/python -m src.data.uiflaky.preprocess.fetch_uiflaky_workspaces
```

#### 3. Verify Workspaces

After cloning, verify that every repo referenced by the metadata CSV is a real git checkout:

```bash
.venv/bin/python scripts/uiflaky/verify_uiflaky_workspaces.py
```

To prune missing or invalid repos from the metadata CSV:

```bash
.venv/bin/python scripts/uiflaky/verify_uiflaky_workspaces.py --prune-invalid-from-csv
```

#### 4. Run Reproduction

Run one test by row index:

```bash
.venv/bin/python scripts/uiflaky/run_uiflaky_test.py --row-index 0 --iterations 10
```

Per-test results are written to:

```text
results/uiflaky/*.json
```

Run the full batch:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python scripts/uiflaky/run_uiflaky_batch.py --iterations 20
```

That writes incremental results to:

```text
datasets/uiflaky/uiflaky-reproduction-results.csv
```

### Filters And Defaults

- Tests run inside Docker using a Node.js image auto-detected from `package.json` (defaults to `node:12-buster`).
- Yarn is installed in the container and used for dependency installation.
- The runner checks out the commit _before_ the fix commit (`<sha>^`) to reproduce the flaky state.
- Early exit: a test stops after the first iteration that produces both a PASS and a FAIL.
- The batch runner groups rows by project and runs each project sequentially to avoid Docker and Git checkout conflicts.

## Documentation
- Main Architecture overview: `docs/Architecture.md`
- Dataset notes: `docs/README_IDoFT.md`
- Mermaid diagrams: `docs/diagrams/`
