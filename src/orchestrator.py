"""
Orchestrator — LangGraph state machine for HA-FlakyRepair.

Detection → (if reproduced) → Repair → END
           → (if not reproduced) → END

Both agents are self-contained ReAct agents that own their Docker execution.
The repair agent only runs when detection confirmed flakiness reproduction.
Pass {"configurable": {"skip_repair": True}} to run detection only.
"""

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END

from src.state import RepairState
from src.agents.detection import detection_agent_node
from src.agents.repair import repair_agent_node


def _should_repair(state: RepairState, config: RunnableConfig | None = None) -> str:
    """Route to repair only when flakiness was reproduced and repair is not skipped."""
    if not state.get("is_flakiness_reproduced"):
        return "skip"
    if (config or {}).get("configurable", {}).get("skip_repair", False):
        return "skip"
    return "repair"


def build_graph() -> StateGraph:
    workflow = StateGraph(RepairState)

    workflow.add_node("detection_agent", detection_agent_node)
    workflow.add_node("repair_agent",    repair_agent_node)

    workflow.add_edge(START, "detection_agent")
    workflow.add_conditional_edges(
        "detection_agent",
        _should_repair,
        {"repair": "repair_agent", "skip": END},
    )
    workflow.add_edge("repair_agent", END)

    return workflow.compile()
