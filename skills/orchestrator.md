# Skill: orchestrator

## Purpose
Main entry point for the LeanForge proving pipeline. Receives a theorem statement and orchestrates the full prove loop: retrieve relevant lemmas, synthesize proof candidates, verify each candidate against the Lean compiler, and repair failures until a valid proof is found or retries are exhausted.

## When to Use
- A user asks to prove a Lean theorem or lemma.
- A user provides a theorem statement and wants an end-to-end proof.
- Any time a complete prove cycle is needed (as opposed to a single sub-step like verification alone).

## Inputs
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `theorem` | string | yes | The full Lean theorem statement to prove (e.g., `theorem foo : 1 + 1 = 2 := by ...`). |
| `context` | string | no | Additional Lean context such as variable declarations, definitions, or namespace opens that precede the theorem. |
| `imports` | list[string] | no | Extra Lean imports beyond the project defaults (e.g., `["Mathlib.Tactic.Ring"]`). |
| `max_retries` | int | no | Maximum number of repair iterations before giving up. Default: 5. |

## Outputs
| Field | Type | Description |
|-------|------|-------------|
| `proof` | string | The synthesized Lean proof text. |
| `status` | enum | One of `verified`, `failed`, `timeout`. |
| `trace` | list[object] | Ordered list of steps taken: retrieval results, synthesis attempts, verification outcomes, repair actions. |
| `diagnostics` | list[object] | Final compiler diagnostics if the proof did not verify. |

## Service Endpoint
- **URL:** `http://localhost:8100`
- **Service:** orchestrator
- **Key endpoints:**
  - `POST /prove` -- submit a theorem and run the full pipeline.
  - `GET /prove/{task_id}/status` -- poll for task completion.
  - `GET /prove/{task_id}/result` -- retrieve the final result.

## Example Usage
```
Use the orchestrator skill to prove the following theorem:

theorem add_comm_zero (n : Nat) : n + 0 = n := by
  sorry

Context: None needed beyond Lean prelude.
```

The orchestrator will:
1. Call **lean-retrieve** to find related lemmas (e.g., `Nat.add_zero`).
2. Synthesize a proof candidate using retrieved context.
3. Call **lean-verify** to compile-check the candidate.
4. If verification fails, call **lean-repair** with the diagnostics.
5. Repeat until verified or retries exhausted.

## Notes
- The orchestrator coordinates the other skills; it should not be bypassed for full proofs.
- The retrieve step runs first to ground synthesis in known lemmas. Do not skip retrieval.
- Each repair iteration feeds the previous error diagnostics back into synthesis, so the trace grows monotonically.
- Timeout for the entire pipeline defaults to 120 seconds; individual verification calls have their own 30-second timeout.
- If the orchestrator service at localhost:8100 is unreachable, report the connection error immediately rather than retrying silently.
