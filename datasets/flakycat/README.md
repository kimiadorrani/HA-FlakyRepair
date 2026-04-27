# FLAKYCAT Dataset Workspace

This folder is intentionally separate from the IDOFT files in `datasets/idoft/`.

## Contents

- `raw/`: copied from `https://github.com/Amal-AK/FLAKYCAT/tree/main/data`.
- `raw/test_files_v0/`: 451 original categorized Java flaky-test snippets.
- `raw/test_files_v12/`: 878 augmented Java flaky-test snippets.
- `raw/vectors/`: CodeBERT/TestSmell vector artifacts from FLAKYCAT.
- `raw/Dataset_informations.xlsx`: source metadata used to build the CSV below.
- `flakycat-java-tests.csv`: normalized metadata for Java reproduction attempts.

## Normalized Metadata

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

## Workspaces

FLAKYCAT project repositories belong under:

```text
workspaces/flakycat/<owner>__<repo>
```

This keeps Java FLAKYCAT checkouts distinct from IDOFT Python checkouts in
`workspaces/idoft/<repo>`.

Clone missing repos with:

```bash
.venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces --limit 3
```

For a full guarded clone pass with per-repo timeout and CSV pruning of failed repos:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces --clone-timeout 180 --prune-failed-from-csv
```

This writes clone failures to `datasets/flakycat/flakycat-clone-failures.csv` and
backs up the original metadata CSV as `datasets/flakycat/flakycat-java-tests.csv.bak`
before removing rows for repos that failed cloning in that run.

## Reproduction

Verify that the active CSV only points at valid git workspaces:

```bash
.venv/bin/python scripts/flakycat/verify_flakycat_workspaces.py
```

To prune missing or invalid repos from the working CSV after creating a backup:

```bash
.venv/bin/python scripts/flakycat/verify_flakycat_workspaces.py --prune-invalid-from-csv
```

Run one Java row repeatedly with:

```bash
.venv/bin/python scripts/flakycat/run_flakycat_java_test.py --project fastjson --test Issue1492 --iterations 20
```

By default the runner excludes order-dependent rows and focuses on
non-order-dependent categories. To opt into OD rows explicitly, add:

```bash
--include-order-dependent
```

The current machine cannot execute Java reproduction yet because `java`, `mvn`,
and `gradle` are not installed. The first attempted row was:

```text
fastjson :: com.alibaba.json.bvt.issue_1400.Issue1492#test_for_issue
checkout: 3ea25de368b185e3c9f3d56e46a4cfcdb9265318
result: environment blocked, required command not found: mvn
```

The attempt result is stored in `results/flakycat/fastjson-test_for_issue.json`.
