# ADR-001: Detection Agent Redesign — ReAct Agent with Adaptive Tool Use

**Status:** Proposed  
**Date:** 2026-05-04  
**Author:** Kimia Dorrani

---

## Context

The current detection pipeline uses a fixed three-node LangGraph sequence:

```
DockerRunner (node) → PrepareDetectionInput (node) → DetectionAgent (node)
```

The `DockerRunner` node runs all four execution protocols blindly upfront (solo baseline, repeated in-process, isolated reruns, randomized file context) regardless of what the test actually needs. The `DetectionAgent` then receives pre-collected logs and makes a single LLM call to classify flakiness.

This design has several problems:

1. **No agency over evidence collection.** The detection agent cannot decide which strategy to run or when enough evidence has been gathered. It always receives the same fixed set of logs regardless of what the test actually needs.

2. **Prompt leaks the dataset taxonomy.** The current system prompt names `NIO` and `NOD` explicitly as allowed categories. This anchors the model toward those labels before it has reasoned from evidence, making classification circular.

3. **No OD support.** The four current protocols cannot detect Order-Dependent (OD-Vic, OD-Brit) flakiness because they only run the target test — never the full suite. OD-Vic tests always pass in isolation; the failure only appears when a polluter test runs first in the same process. This is why OD tests are skipped by default in the current pipeline.

4. **Dataset coverage gap.** The pipeline operates only on the non-OD reproduction CSV. The merged dataset (`idoft-merged-reproduction-results.csv`) contains 1535 tests across NIO, NOD, OD-Vic, OD-Brit, and OD categories. The current design cannot handle the full dataset.

---

## Decision

Redesign the detection step as a single **ReAct agent** that owns the entire evidence collection and classification loop. The pre-collection node (`DockerRunner`) and the sanitization node (`PrepareDetectionInput`) are removed as standalone pipeline stages. Instead, the agent is given a set of focused Docker execution tools and decides adaptively which strategies to invoke based on intermediate results.

---

## Architecture

### Pipeline Change

**Before:**
```
START → DockerRunner → PrepareDetectionInput → DetectionAgent → END
```

**After:**
```
START → DetectionAgent (ReAct loop with tools) → END
```

### Agent Tools

The agent is given one tool per execution strategy. Each tool runs a specific Docker execution pattern and returns structured results — never the ground-truth category.

| Tool | What it does | Catches |
|---|---|---|
| `run_solo(test_name)` | Single isolated run | OD-Brit baseline (fails alone) |
| `run_repeated_in_process(test_name, n)` | N runs in same process via `--count=N` | NIO (state left dirty) |
| `run_isolated_reruns(test_name, n)` | N independent subprocess invocations | NOD (random outcome) |
| `run_suite_randomized(test_name, seed)` | Full test directory run with `--randomly-seed` | OD-Vic (order sensitivity) |

Each tool returns:
```json
{
  "strategy": "<tool name>",
  "pass_count": 3,
  "fail_count": 2,
  "outcome": "mixed | always_pass | always_fail | error",
  "passing_log": "<first passing stdout or null>",
  "failing_log": "<first failing stdout or null>"
}
```

### Adaptive Investigation Strategy

The agent follows a reasoning loop rather than a fixed protocol sequence. The recommended exploration order is:

```
1. run_solo()
   ├── always_fail  → suspect OD-Brit or broken env; try run_suite_randomized to check if it passes in some orderings
   ├── mixed        → flaky in isolation; run_isolated_reruns to confirm NOD
   └── always_pass  →
       2. run_repeated_in_process()
          ├── mixed / always_fail → NIO confirmed (state pollution across runs)
          └── always_pass →
              3. run_isolated_reruns()
                 ├── mixed        → NOD confirmed (truly random)
                 └── always_pass  →
                     4. run_suite_randomized() × N seeds
                        ├── mixed → OD-Vic confirmed (order sensitive)
                        └── always_pass → NOT_FLAKY (cannot reproduce)
```

The agent stops early as soon as it has both a passing log and a failing log. It does not call further tools once flakiness is confirmed.

### Prompt Design: No Taxonomy Leakage

The system prompt must not name any label from the dataset taxonomy (`NIO`, `NOD`, `OD`, `OD-Vic`, `OD-Brit`). Instead, it presents purely symptom-based behavioral categories that the agent derives from observation:

| Prompt label | Observable symptom | Maps to (post-processing) |
|---|---|---|
| `STATE_POLLUTION` | Fails after repeated runs in same process; passes on first run | NIO |
| `ISOLATED_UNSTABLE` | Fails and passes across independent isolated runs | NOD |
| `EXECUTION_ORDER_SENSITIVE` | Passes alone; fails when full suite runs with certain orderings | OD-Vic |
| `BRITTLE_SETUP_DEPENDENT` | Fails alone; passes when full suite runs with certain orderings | OD-Brit |
| `NOT_FLAKY` | Consistent outcome across all strategies | — |
| `UNCLEAR` | Non-determinism confirmed but pattern does not match above | Unknown |

The mapping from prompt labels to dataset taxonomy labels happens entirely in post-processing code (`_parse_detection_response`), never in the prompt.

### Termination Conditions

The agent stops the investigation loop when any of the following is true:

- Both a passing log and a failing log have been captured (flakiness confirmed).
- All four strategies have been exhausted with consistent outcomes (cannot reproduce).
- `run_suite_randomized` has been called with at least 10 different seeds with no failure (OD-Vic unlikely; report `NOT_FLAKY` with low confidence).
- A Docker execution error occurs that prevents further runs.

---

## Dataset

The pipeline will operate on the merged dataset:

```
datasets/idoft/idoft-merged-reproduction-results.csv
```

This file contains 1535 rows across all categories:

| Category | Count | Reproduced |
|---|---|---|
| NIO | 150 | 88 (59%) |
| NOD | 213 | 9 (4%) |
| OD-Brit | 321 | 319 (99%) |
| OD-Vic | 784 | 250 (32%) |
| OD | 33 | 13 (39%) |
| ID / UD | 4 | 0 |

The `Reproduced` column reflects the original batch runner results and is used only for evaluation — not as input to the detection agent.

---

## Consequences

### Positive

- The detection agent handles all four flakiness types (NIO, NOD, OD-Vic, OD-Brit) without requiring the category as input.
- Prompt contains zero taxonomy label leakage. Classification is derived from observed behavior.
- The agent stops early once evidence is sufficient, avoiding unnecessary Docker runs for obvious cases (e.g., a test that fails on the first solo run).
- The `PrepareDetectionInput` sanitization node is no longer needed — the agent never receives the ground-truth category.
- Accuracy evaluation uses the ground-truth `Category` column from the merged CSV, compared against the agent's `behavioral_pattern` after post-processing mapping.

### Negative

- OD-Vic detection requires `run_suite_randomized` which is expensive: each call runs the full test directory. Detection of OD-Vic tests may require 10–50 suite runs before a failure is observed (or the agent gives up).
- Total wall-clock time per test increases for OD-Vic cases compared to the current pipeline.
- NOD tests with very low failure probability (e.g., <1%) may not be reproducible within a reasonable budget and will be reported as `NOT_FLAKY`.

### Neutral

- The LangGraph pipeline shrinks from three nodes to one agent node. The orchestrator becomes simpler.
- The `DockerRunner` code is refactored into tool functions rather than a standalone node, but the underlying Docker execution logic is unchanged.

---

## Alternatives Considered

### Repository-based static analysis

Pass the source code repository to the LLM instead of execution logs. The agent reads test code and classifies flakiness from code patterns (e.g., use of `random`, global state, file I/O).

**Rejected because:** Static analysis produces predictions, not observations. A test can use `random.shuffle` and still be deterministic in practice. The log-based approach provides evidence that flakiness was actually observed, which is a stronger signal and aligns with how flakiness is defined in the literature (observed non-determinism).

### Fixed four-protocol pre-collection (current approach)

Keep the `DockerRunner` node running all four protocols upfront, agent only does classification.

**Rejected because:** Cannot detect OD tests (requires suite-level execution), always runs all protocols even when evidence is found early, and the agent has no control over evidence quality.

### Separate OD and non-OD pipelines

Keep two distinct pipelines: one for NIO/NOD (current), one for OD with a separate OD-aware runner.

**Rejected because:** Requires knowing the category before running, which is circular. The unified ReAct agent approach avoids this by using adaptive tool selection based on observed outcomes.
