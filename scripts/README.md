# Scripts

This directory is split by ownership:

- `shared/`: utilities used across datasets.
- `idoft/`: Python/pytest dataset runners and IDOFT-specific helpers.
- `flakycat/`: Java/FLAKYCAT reproduction runners and batch entrypoints.

## Shared

- `shared/compute_detection_accuracy.py`: compare a detection session against the IDOFT ground-truth CSV.

## IDOFT

- `idoft/run_repo_dataset_tests.py`: run all dataset-listed pytest tests for one IDOFT repository in a single invocation.

## FLAKYCAT

- `flakycat/run_flakycat_java_test.py`: repeat one FLAKYCAT Java test and record pass/fail behavior.
- `flakycat/run_flakycat_batch.py`: batch-run non-order-dependent FLAKYCAT rows and update the incremental results CSV.
- `flakycat/run_flakycat_full_batch.sh`: clone missing FLAKYCAT repos, then launch the 100-iteration batch run.
