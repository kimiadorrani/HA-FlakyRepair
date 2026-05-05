"""
TraceWriter — appends serialised PipelineTrace objects to per-project JSONL files.

Layout:
    results/<session_id>/traces/<project_name>.jsonl
        one JSON line per test invocation

Each line is a complete PipelineTrace dict so traces can be streamed and
analysed without loading the whole file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .agent_trace import PipelineTrace

logger = logging.getLogger(__name__)


class TraceWriter:
    """Writes one JSONL line per test trace under results/<session>/traces/."""

    def __init__(self, session_dir: str | Path) -> None:
        self.traces_dir = Path(session_dir) / "traces"
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Traces directory: %s", self.traces_dir)

    def write(self, trace: PipelineTrace) -> None:
        path = self.traces_dir / f"{trace.project}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trace.to_dict(), default=str) + "\n")
        logger.debug("Trace saved: project=%s test=%s", trace.project, trace.test_name)
