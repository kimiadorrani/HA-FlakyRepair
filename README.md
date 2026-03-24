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

### 3. Preprocess The Dataset
The recommended workflow is:
1. start from the raw Python flaky-test dataset in `src/data/py-data.csv`
2. fetch any missing non-OD repositories into `workspaces/`
3. run the reproducibility pass
4. export only the reproduced tests into a clean CSV

Run the full preprocessing step with:
```bash
.venv/bin/python -m src.preprocess_dataset
```

This writes a clean benchmark CSV to:
```text
src/data/preprocessed/py-data-reproducible.csv
```

It also writes a preprocessing report to:
```text
src/data/preprocessed/preprocess-report.json
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
.venv/bin/python -m src.main --input-csv src/data/preprocessed/py-data-reproducible.csv --include-not-reproduced
```

If a repository already exists in `workspaces/`, it is skipped and not downloaded again.

### 4. Run Detection On The Clean CSV
After preprocessing, run the detection phase on the cleaned dataset:
```bash
.venv/bin/python -m src.main --input-csv src/data/preprocessed/py-data-reproducible.csv
```

You can still run the raw dataset directly if needed:
```bash
.venv/bin/python -m src.main
```

## Useful Commands

Fetch missing repositories only:
```bash
.venv/bin/python -m src.data.preprocess.fetch_workspaces
```

Export reproduced tests from an existing results session:
```bash
.venv/bin/python -m src.data.preprocess.export_reproducible_csv --session 2026-03-24_14-35-48
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

## Notes
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

## Documentation
- Main Architecture overview: `docs/Architecture.md`
- Dataset notes: `docs/README_IDoFT.md`
- Mermaid diagrams: `docs/diagrams/`
