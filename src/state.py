import operator
from typing import Annotated, TypedDict, Any


class RepairState(TypedDict):
    """
    Shared memory passed between LangGraph nodes across the pipeline.
    """
    # Session metadata
    session_id:       str | None   # set by main.py from ResultLogger.session_id
    detection_model:  str          # model key for Detection Agent (default: "minimax")

    # Test coordinates
    dataset:      str
    language:     str
    build_system: str
    project_url:  str
    sha_detected: str
    module_path:  str
    test_name:    str
    category:     list[str]   # ground truth — never passed to detection agent

    # Detection Agent output
    passing_log:              str | None
    failing_log:              str | None
    is_flakiness_reproduced:  bool
    error_message:            str | None
    pass_count:               int
    fail_count:               int
    outcome_profile:          str | None
    execution_profiles:       list[dict[str, Any]]
    flaky_type:               str | None
    root_cause_analysis:      str | None
    agent_trace:              list[dict[str, Any]]
    token_usage:              dict[str, Any]

    # Repair Agent inputs / config
    repair_model: str          # model key for Repair Agent (default: "minimax")

    # Repair Agent output
    patch:          str | None   # unified diff of all changes made
    patch_target:   str | None   # "source" | "test" | "both"
    files_modified: list[str]    # repo-relative paths of changed files
    fix_summary:    str | None   # one-line human description of the fix
    is_fixed:       bool         # True if verification confirmed flakiness is gone
    fix_attempts:   int          # number of write→verify iterations used
    repair_error:   str | None   # set if repair could not run at all

    # Structured per-agent spans — each agent appends its span dict via operator.add
    # Use src.tracing.agent_trace.AgentSpan.to_dict() format
    pipeline_trace: Annotated[list[dict[str, Any]], operator.add]

    # Append-only trajectory log
    trajectory: Annotated[list[dict[str, Any]], operator.add]
