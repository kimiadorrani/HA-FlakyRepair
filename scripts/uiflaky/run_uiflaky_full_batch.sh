#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

.venv/bin/python -m src.data.uiflaky.preprocess.export_uiflaky_metadata
.venv/bin/python -m src.data.uiflaky.preprocess.fetch_uiflaky_workspaces
.venv/bin/python scripts/uiflaky/run_uiflaky_batch.py --iterations 20
