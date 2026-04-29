# FLAKYCAT Dataset Workspace

This folder is intentionally separate from the IDOFT files in `datasets/idoft/`.

## Contents

- `raw/`: copied from `https://github.com/Amal-AK/FLAKYCAT/tree/main/data`.
- `raw/test_files_v0/`: 451 original categorized Java flaky-test snippets.
- `raw/test_files_v12/`: 878 augmented Java flaky-test snippets.
- `raw/vectors/`: CodeBERT/TestSmell vector artifacts from FLAKYCAT.
- `raw/Dataset_informations.xlsx`: source metadata used to build the CSV below.
- `flakycat-java-tests.csv`: normalized metadata for Java reproduction attempts.

## End-to-End

If you want the full FLAKYCAT workflow from this repository:

```bash
.venv/bin/python -m src.data.flakycat.preprocess.export_flakycat_metadata
PYTHONUNBUFFERED=1 bash ./scripts/flakycat/run_flakycat_full_batch.sh
```

By default, the batch step deletes each repo workspace after its repository
batch finishes.

## Step-by-Step

### 1. Export Normalized Metadata

Generate the CSV with:

```bash
.venv/bin/python -m src.data.flakycat.preprocess.export_flakycat_metadata
```

Current export summary:

- 397 rows with enough metadata for a Java reproduction index.
- 396 rows include a GitHub project URL.
- 147 distinct projects.
- Rows come from the spreadsheet sheets `Costa et al,`, `Habchi et al,`,
  `IfixFlakies`, and `collected`.

The `Luo et al,` spreadsheet sheet is not exported yet because it does not
provide enough repository URL/path metadata for automatic checkout.

### 2. Clone Repositories

FLAKYCAT project repositories belong under:

```text
workspaces/flakycat/<owner>__<repo>
```

This keeps Java FLAKYCAT checkouts distinct from IDOFT Python checkouts in
`workspaces/idoft/<repo>`.

Use the guarded clone command as the default:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces --clone-timeout 180 --prune-failed-from-csv
```

This writes clone failures to `datasets/flakycat/flakycat-clone-failures.csv` and
backs up the original metadata CSV as `datasets/flakycat/flakycat-java-tests.csv.bak`
before removing rows for repos that failed cloning in that run.

### 3. Verify Workspaces

Verify that the active CSV only points at valid git workspaces:

```bash
.venv/bin/python scripts/flakycat/verify_flakycat_workspaces.py
```

To prune missing or invalid repos from the working CSV after creating a backup:

```bash
.venv/bin/python scripts/flakycat/verify_flakycat_workspaces.py --prune-invalid-from-csv
```

### 4. Run Reproduction

Run the full non-order-dependent batch with:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python scripts/flakycat/run_flakycat_batch.py --iterations 100
```

If you want to keep repo workspaces after the batch for debugging:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python scripts/flakycat/run_flakycat_batch.py --iterations 100 --keep-workspace
```

Run one Java row repeatedly with:

```bash
.venv/bin/python scripts/flakycat/run_flakycat_java_test.py --project fastjson --test Issue1492 --iterations 20
```

### 5. Defaults

By default the runner excludes order-dependent rows and focuses on
non-order-dependent categories. To opt into OD rows explicitly, add:

```bash
--include-order-dependent
```
