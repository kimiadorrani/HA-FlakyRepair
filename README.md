# HA-FlakyRepair

This repository contains the code and resources for HA-FlakyRepair.

## Quick Start

### 1. Requirements
- Python 3.10+
- (Optional but recommended) Virtual Environment

### 2. Setup
Install the necessary dependencies using `pip`:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Run the Detection Phase
The core Orchestrator logic has been developed up to the **Detection Phase**. You can run a simulated repair session (for an IDoFT test case) using:
```bash
PYTHONPATH=. python -m src.main
```

## Documentation
- Main Architecture overview: `docs/Architecture.md`
- Dataset notes: `docs/README_IDoFT.md`
- Mermaid diagrams: `docs/diagrams/`
