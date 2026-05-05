"""
Orchestrator — LangGraph state machine for HA-FlakyRepair.

Detection phase:
  START → detection_agent → END

The detection agent is a self-contained ReAct agent that owns Docker
execution. There is no separate pre-collection or sanitization node.
"""

from langgraph.graph import StateGraph, START, END

from src.state import RepairState
from src.agents.detection import detection_agent_node


def build_graph() -> StateGraph:
    workflow = StateGraph(RepairState)

    workflow.add_node("detection_agent", detection_agent_node)

    # Future agents:
    # workflow.add_node("context_explorer", context_explorer_node)
    # workflow.add_node("repair_agent", repair_agent_node)
    # workflow.add_node("review_agent", review_agent_node)

    workflow.add_edge(START, "detection_agent")
    workflow.add_edge("detection_agent", END)

    return workflow.compile()
