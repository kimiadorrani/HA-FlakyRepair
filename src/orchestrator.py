from langgraph.graph import StateGraph, START, END
from src.state import RepairState
from src.agents.detection import detection_agent_node

def build_graph() -> StateGraph:
    """
    Builds the state machine (LangGraph) for HA-FlakyRepair.
    This acts as the Orchestrator, managing flow across specialized agents.
    """
    # 1. Initialize the Controller (StateGraph) with our Memory Layer (RepairState)
    workflow = StateGraph(RepairState)
    
    # 2. Add nodes (The specialized agents)
    workflow.add_node("detection_agent", detection_agent_node)
    
    # Future agents to be added:
    # workflow.add_node("context_explorer", context_explorer_node)
    # workflow.add_node("repair_agent", repair_agent_node)
    # workflow.add_node("review_agent", review_agent_node)
    # workflow.add_node("validator", validator_node)

    # 3. Define edges (The workflow logic)
    # For Phase 1 (up to Detection), we just start -> detection -> end
    workflow.add_edge(START, "detection_agent")
    workflow.add_edge("detection_agent", END)
    
    # 4. Compile the graph
    return workflow.compile()
