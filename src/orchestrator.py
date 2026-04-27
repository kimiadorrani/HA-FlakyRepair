"""
Orchestrator — LangGraph state machine for HA-FlakyRepair.

Flow: START → run_test_in_docker → prepare_detection_input → detection_agent → END
"""

from langgraph.graph import StateGraph, START, END

from src.state import RepairState
from src.agents.prepare_detection_input import prepare_detection_input_node
from src.tools.docker_runner import run_test_in_docker
from src.agents.detection import detection_agent_node


def build_graph() -> StateGraph:
    """
    Builds the LangGraph workflow for HA-FlakyRepair detection phase.
    """
    workflow = StateGraph(RepairState)

    # Add nodes
    workflow.add_node("run_test_in_docker", run_test_in_docker)
    workflow.add_node("prepare_detection_input", prepare_detection_input_node)
    workflow.add_node("detection_agent", detection_agent_node)

    # Future agents:
    # workflow.add_node("context_explorer", context_explorer_node)
    # workflow.add_node("repair_agent", repair_agent_node)
    # workflow.add_node("review_agent", review_agent_node)

    # Define flow: START → Docker Runner → Sanitizer → Detection Agent → END
    workflow.add_edge(START, "run_test_in_docker")
    workflow.add_edge("run_test_in_docker", "prepare_detection_input")
    workflow.add_edge("prepare_detection_input", "detection_agent")
    workflow.add_edge("detection_agent", END)

    return workflow.compile()
