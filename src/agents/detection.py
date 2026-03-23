from src.state import RepairState

def detection_agent_node(state: RepairState) -> dict:
    """
    The Detection Agent analyzes the test failure and identifies the flaky type.
    This node receives the global RepairState and returns state updates.
    """
    print("\n" + "="*40)
    print("🕵️‍♀️  [DETECTION AGENT] Executing...")
    print("="*40)
    print(f"Analyzing test: {state.get('test_name')} \nfrom repo: {state.get('project_url')}")
    
    # Heuristic analysis based on IDoFT category priors
    category_priors = state.get('category', [])
    
    flaky_type = None
    if "OD" in category_priors:
        flaky_type = "Order-Dependent (OD)"
        print("-> Prior category is OD. Identified Flaky Type: Order-Dependent (OD).")
        print("-> Hypothesis: Test depends on suite execution order, shared state, or environment residue.")
    elif "NIO" in category_priors:
        flaky_type = "Non-Idempotent-Outcome (NIO)"
        print("-> Prior category is NIO. Identified Flaky Type: Non-Idempotent-Outcome (NIO).")
        print("-> Hypothesis: Test lacks idempotency or proper state reset mechanism.")
    elif "TD" in category_priors or "TZD" in category_priors:
        flaky_type = "Time-Dependent (TD/TZD)"
        print("-> Prior category is TD/TZD. Identified Flaky Type: Time-Dependent.")
        print("-> Hypothesis: Test behaves differently depending on time zones or sleep durations.")
    else:
        flaky_type = "Unknown Flaky Behavior"
        print("-> Identified Flaky Type: Unknown.")
        
    print("="*40 + "\n")
    
    # The node returns only the parts of the state that require updating.
    # The `trajectory` uses `operator.add`, so passing a list here APPENDS to the shared state.
    return {
        "flaky_type": flaky_type,
        "trajectory": [{
            "agent": "DetectionAgent",
            "action": "classified_flaky_type",
            "observation": flaky_type,
            "hypothesis": category_priors
        }]
    }
