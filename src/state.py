import operator
from typing import Annotated, TypedDict, Any

class RepairState(TypedDict):
    """
    The Memory Layer (Vault) / Trajectory Memory.
    This state is passed around the LangGraph network between agents.
    """
    # Ecosystem / IDoFT context
    dataset: str
    language: str
    build_system: str
    project_url: str
    sha_detected: str
    module_path: str
    test_name: str
    category: list[str]
    
    # Execution State
    flaky_type: str | None
    code_context: str | None
    current_patch: str | None
    validation_result: str | None
    rotation_count: int
    
    # History package / Trajectory (appending logs of what agents did)
    # The Annotated[list[dict], operator.add] means we append to the list across node edges
    trajectory: Annotated[list[dict[str, Any]], operator.add]
