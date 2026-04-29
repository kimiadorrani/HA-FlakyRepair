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

### End-to-End

If you want the standard IDOFT flow from raw dataset to detection, run:

```bash
.venv/bin/python -m src.preprocess_dataset
.venv/bin/python -m src.main --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv
```

This gives you:
- a cleaned preprocessing CSV at `datasets/idoft/preprocessed/py-data-reproducible.csv`
- a preprocessing report at `datasets/idoft/preprocessed/preprocess-report.json`
- detection results under `results/`

### Step-by-Step

#### 1. Preprocess The Dataset

This step:
- fetches any missing non-OD repositories into `workspaces/idoft/`
- runs the reproducibility pass
- records preprocessing outcome fields back into the cleaned CSV

Run:
```bash
.venv/bin/python -m src.preprocess_dataset
```

Outputs:
```text
datasets/idoft/preprocessed/py-data-reproducible.csv
datasets/idoft/preprocessed/preprocess-report.json
```

The cleaned CSV now keeps every processed row, not only reproduced ones. Each row
is annotated with preprocessing outcome fields such as:
- `Preprocess Status`
- `Reproduced`
- `Preprocess Error`
- `Selected Profile`
- `CPU Limit`
- `Memory Limit`
- `Pass Count`
- `Fail Count`
- `Outcome Profile`

When you later run `src.main` on that cleaned CSV, it uses only rows marked as
reproduced by default and reuses the stored CPU and memory settings for them.
If you want to include rows that preprocessing marked as not reproduced, add:
```bash
.venv/bin/python -m src.main --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv --include-not-reproduced
```

If a repository already exists in `workspaces/idoft/`, it is skipped and not downloaded again.

#### 2. Run Detection On The Clean CSV

After preprocessing, run the detection phase on the cleaned dataset:
```bash
.venv/bin/python -m src.main --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv
```

#### 3. Optional Variations

You can still run the raw dataset directly if needed:
```bash
.venv/bin/python -m src.main
```

Fetch missing repositories only:
```bash
.venv/bin/python -m src.data.idoft.preprocess.fetch_workspaces
```

Export reproduced tests from an existing results session:
```bash
.venv/bin/python -m src.data.idoft.preprocess.export_reproducible_csv --session 2026-03-24_14-35-48
```

Run only one project:
```bash
.venv/bin/python -m src.main --project bottle-neck
```

Run only one category:
```bash
.venv/bin/python -m src.main --category NOD
```

Run only the first `N` tests:
```bash
.venv/bin/python -m src.main --limit 3
```

### Notes
- By default, OD / OD-Vic / OD-Brit rows are skipped.
- Detection currently calls the LLM only when flaky behavior is actually reproduced.
- Result files now record:
  - `pass_count`
  - `fail_count`
  - `outcome_profile`
  - `execution_profiles`
- The cleaned reproducible CSV also stores the selected execution profile so
  `src.main` can rerun tests with the same settings found during preprocessing.
- Docker state is aggressively cleaned before each run to reduce disk usage.
- The runner can fall back from a baseline run to a stressed run when a test is `always_pass`.

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

## Documentation
- Main Architecture overview: `docs/Architecture.md`
- Dataset notes: `docs/README_IDoFT.md`
- Mermaid diagrams: `docs/diagrams/`
