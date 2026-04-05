# Skill: orchestrator

## Purpose
Main entry point for the LeanForge proving pipeline. Receives a theorem statement and orchestrates the full prove loop: retrieve relevant lemmas, synthesize proof candidates, verify each candidate against the Lean compiler, and repair failures until a valid proof is found or retries are exhausted.

**CRITICAL: The orchestrator MUST call lean_env (localhost:8101) to verify every candidate proof. No proof may be presented to the user unless lean_env returns `is_verified: true`. The LLM's belief that a proof is correct is irrelevant — only Lean compilation counts.**

## When to Use
- A user asks to prove a Lean theorem or lemma.
- A user provides a theorem statement and wants an end-to-end proof.
- Any time a complete prove cycle is needed (as opposed to a single sub-step like verification alone).

## Required Steps (mandatory, in order)
Every invocation of the orchestrator MUST execute these steps in sequence. Skipping any step is a protocol violation.

1. **Retrieve** — Call lean-retrieve (POST localhost:8103/search) with the theorem statement to find relevant lemmas from mathlib and the local corpus. This grounds synthesis in known results and prevents hallucinated lemma names.

2. **Synthesize** — Submit a task to the orchestrator service:
   - `POST localhost:8100/tasks` to create the task
   - `POST localhost:8100/tasks/{id}/run` to execute the full pipeline
   The synthesis service (Leanstral) generates candidate proofs using the retrieved context. Do NOT skip this and write proofs from LLM reasoning alone.

3. **Verify** — Send EVERY candidate proof to lean_env for compilation:
   - `POST localhost:8101/verify` with the full Lean source (including imports)
   - Check the response: `is_verified: true` means the proof compiles. Any other result means it does NOT.
   - **A proof that has not been through this step is NOT verified, regardless of how confident the LLM is.**

4. **Repair** — If verification fails, read the `diagnostics` array from the lean_env response. Feed the error messages back into synthesis or fix manually. Then go back to step 3. Repeat up to `max_retries` times.

5. **Report** — Only after step 3 returns `is_verified: true`, present the proof to the user as verified. If all retries are exhausted, report failure with the final diagnostics.

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
  - `POST /tasks` -- create a new prove task.
  - `POST /tasks/{id}/run` -- execute the full prove pipeline for a task.
  - `GET /tasks/{id}/status` -- poll for task completion.
  - `GET /tasks/{id}/result` -- retrieve the final result.

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
3. Call **lean-verify** to compile-check the candidate. **This step is NOT optional.**
4. If verification fails, call **lean-repair** with the diagnostics.
5. Repeat until verified or retries exhausted.

## Anti-patterns (DO NOT do these)
- **DO NOT present a proof without calling lean_env first.** A proof that "looks correct" is not verified.
- **DO NOT skip the retrieval step.** Synthesis without retrieval leads to hallucinated lemma names and wasted verification cycles.
- **DO NOT treat LLM confidence as verification.** The LLM may be certain a proof is correct and still be wrong. Only `is_verified: true` from lean_env counts.
- **DO NOT silently swallow verification failures.** If lean_env returns errors, they must be fed into the repair loop or reported to the user.
- **DO NOT bypass the orchestrator to write proofs directly** unless the orchestrator service is confirmed unreachable — and even then, you MUST still verify through lean_env.

## Notes
- The orchestrator coordinates the other skills; it should not be bypassed for full proofs.
- The retrieve step runs first to ground synthesis in known lemmas. Do not skip retrieval.
- Each repair iteration feeds the previous error diagnostics back into synthesis, so the trace grows monotonically.
- Timeout for the entire pipeline defaults to 120 seconds; individual verification calls have their own 30-second timeout.
- If the orchestrator service at localhost:8100 is unreachable, report the connection error immediately rather than retrying silently.
