"""
Prepare Detection Input — sanitize state before the detection agent.

This node removes dataset-label hints that the rerun stage needed for execution
strategy selection, so the detection stage only sees rerun evidence.
"""

from __future__ import annotations

from typing import Any

from src.state import RepairState


def prepare_detection_input_node(state: RepairState) -> dict[str, Any]:
    """
    Remove label-carrying fields before handing control to the detection agent.

    We keep only the rerun evidence and redact category hints from the shared
    state/trajectory so later stages do not accidentally read the dataset label.
    """
    sanitized_trajectory: list[dict[str, Any]] = []
    for item in state.get("trajectory", []):
        if not isinstance(item, dict):
            sanitized_trajectory.append(item)
            continue
        clean_item = dict(item)
        clean_item.pop("category", None)
        sanitized_trajectory.append(clean_item)

    return {
        "category": [],
        "trajectory": sanitized_trajectory,
    }
