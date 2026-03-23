import sys
from src.orchestrator import build_graph

def run_development_session():
    """
    Entrypoint simulating the Orchestrator initializing a repair session
    from an IDoFT dataset row (using the Alibaba fastjson example from README).
    """
    # 1. Initialize the IDoFT target row as initial state (Trajectory Memory initialization)
    initial_state = {
        "dataset": "IDoFT",
        "language": "java",
        "build_system": "maven",
        "project_url": "https://github.com/alibaba/fastjson",
        "sha_detected": "e05e9c5e4be580691cc55a59f3256595393203a1",
        "module_path": ".",
        "test_name": "com.alibaba.json.bvt.date.DateTest_tz.test_codec",
        "category": ["OD"],
        "flaky_type": None,
        "code_context": None,
        "current_patch": None,
        "validation_result": None,
        "rotation_count": 0,
        "trajectory": [] # Memory array starts empty
    }
    
    print("\n" + "*"*60)
    print("🚀 INITIALIZING HA-FLAKYREPAIR ORCHESTRATOR")
    print("*"*60)
    print(f"Target Repository: {initial_state['project_url']}")
    print(f"Target Revision  : {initial_state['sha_detected']}")
    print(f"Target Test      : {initial_state['test_name']}")
    print(f"Known Categories : {initial_state['category']}")
    print("*"*60 + "\n")
    
    # 2. Build LangGraph workflow
    app = build_graph()
    
    # 3. Execute graph (This runs START -> Detection Agent -> END)
    print("Starting Repair Workflow Iteration Loop...\n")
    final_state = app.invoke(initial_state)
    
    print("*"*60)
    print("🏁 WORKFLOW REACHED END OF DETECTION PHASE")
    print("*"*60)
    print("\nResulting State Updates:")
    print(f"-> Final determined flaky_type: '{final_state.get('flaky_type')}'")
    
    print("\nCollected Trajectory History (Memory Vault):")
    for event in final_state.get('trajectory', []):
        print(f" >> {event}")
    print("\n" + "*"*60 + "\n")

if __name__ == "__main__":
    run_development_session()
