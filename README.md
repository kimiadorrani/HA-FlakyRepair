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

### 3. Preprocess The Dataset
The recommended IDOFT workflow is:
1. start from the raw Python flaky-test dataset in `datasets/idoft/raw/py-data.csv`
2. fetch any missing non-OD repositories into `workspaces/idoft/`
3. run the reproducibility pass
4. export only the reproduced tests into a clean CSV

Run the full preprocessing step with:
```bash
.venv/bin/python -m src.preprocess_dataset
```

This writes a clean benchmark CSV to:
```text
datasets/idoft/preprocessed/py-data-reproducible.csv
```

It also writes a preprocessing report to:
```text
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

### 4. Run Detection On The Clean CSV
After preprocessing, run the detection phase on the cleaned dataset:
```bash
.venv/bin/python -m src.main --input-csv datasets/idoft/preprocessed/py-data-reproducible.csv
```

You can still run the raw dataset directly if needed:
```bash
.venv/bin/python -m src.main
```

### Useful Commands

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

### 1. Prepare Metadata
The FLAKYCAT workflow is separate from IDOFT. It is currently focused on Java
test reproduction for non-order-dependent rows.

Generate the normalized metadata CSV from the raw replication package:

```bash
.venv/bin/python -m src.data.flakycat.preprocess.export_flakycat_metadata
```

This writes:

```text
datasets/flakycat/flakycat-java-tests.csv
```

### 2. Clone FLAKYCAT Repositories
Clone the Java repositories used by the normalized CSV into:

```text
workspaces/flakycat/<owner>__<repo>
```

Use:

```bash
.venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces
```

If a repo hangs or is gone, use the guarded mode below. It times out each clone,
records failures, and removes failed repos from the working CSV after backing it up:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces --clone-timeout 180 --prune-failed-from-csv
```

### 3. Run One FLAKYCAT Test
Run one non-order-dependent Java test repeatedly:

```bash
.venv/bin/python scripts/flakycat/run_flakycat_java_test.py --project fastjson --test Issue1492 --iterations 100
```

By default this runner excludes order-dependent rows. To include them:

```bash
.venv/bin/python scripts/flakycat/run_flakycat_java_test.py --project dubbo --include-order-dependent --iterations 100
```

Per-test results are written to:

```text
results/flakycat/*.json
```

### 4. Verify FLAKYCAT Workspaces
Audit the active FLAKYCAT CSV against `workspaces/flakycat/` and confirm that
each expected repo is a real git checkout:

```bash
.venv/bin/python scripts/flakycat/verify_flakycat_workspaces.py
```

If you want to remove missing or invalid repos from the working CSV after
backing it up:

```bash
.venv/bin/python scripts/flakycat/verify_flakycat_workspaces.py --prune-invalid-from-csv
```

### 5. Run The FLAKYCAT Batch
Run the full non-order-dependent batch in the foreground:

```bash
PYTHONUNBUFFERED=1 bash ./scripts/flakycat/run_flakycat_full_batch.sh
```

That command:
1. clones any missing FLAKYCAT repositories
2. runs the non-order-dependent rows
3. executes each selected row `100` times
4. records incremental results in:

```text
datasets/flakycat/flakycat-reproduction-results.csv
```

If you already cloned the repos and only want the batch step:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python scripts/flakycat/run_flakycat_batch.py --iterations 100
```

### Notes
- FLAKYCAT is a Java dataset, not a pytest/Python dataset.
- The current reproduction path uses Docker to run Maven or Gradle where possible.
- Non-order-dependent rows are the default focus.
- Many older Java projects may still fail for environmental reasons such as dead repositories, blocked old HTTP artifact sources, or missing legacy dependencies.

## Documentation
- Main Architecture overview: `docs/Architecture.md`
- Dataset notes: `docs/README_IDoFT.md`
- Mermaid diagrams: `docs/diagrams/`
