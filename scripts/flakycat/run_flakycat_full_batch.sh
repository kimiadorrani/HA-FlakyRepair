#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

.venv/bin/python -m src.data.flakycat.preprocess.fetch_flakycat_workspaces --clone-timeout 180 --prune-failed-from-csv
.venv/bin/python scripts/flakycat/run_flakycat_batch.py --iterations 100
