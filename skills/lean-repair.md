# Skill: lean-repair

## Purpose
Takes a failed proof attempt along with its compiler diagnostics and original goal, then generates repair candidates. Analyzes the error category to apply targeted repair strategies (e.g., fixing identifier names, adjusting types, restructuring tactic sequences) and verifies each candidate.

## When to Use
- After lean-verify returns a non-verified status.
- Inside the orchestrator loop when a synthesized proof fails to compile.
- When a user has a broken proof and asks for help fixing it.
- NOT as a first step; always attempt synthesis before repair.

## Inputs
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `failed_proof` | string | yes | The Lean proof text that failed verification. |
| `diagnostics` | list[object] | yes | Compiler diagnostics from lean-verify (with severity, message, line, column, category). |
| `goal` | string | yes | The original theorem statement being proved. |
| `context` | string | no | Surrounding Lean context (imports, definitions). |
| `max_candidates` | int | no | Maximum number of repair candidates to generate. Default: 3. |
| `prior_attempts` | list[string] | no | Previous failed repair attempts to avoid repeating. |

## Outputs
| Field | Type | Description |
|-------|------|-------------|
| `candidates` | list[object] | Repair candidates, each containing: |
| `candidates[].proof` | string | The repaired proof text. |
| `candidates[].status` | enum | Verification status of this candidate: `verified`, `failed`, `timeout`. |
| `candidates[].changes` | list[string] | Human-readable description of what was changed. |
| `candidates[].diagnostics` | list[object] | Remaining diagnostics if not fully verified. |
| `best` | object | The best candidate (verified if any are, otherwise fewest remaining errors). |

## Service Endpoint
- **URL:** `${ORCHESTRATOR_URL}` (orchestrator) and `${LEAN_ENV_URL}` (lean_env)
- **Service:** orchestrator + lean_env loop
- **Key endpoints:**
  - `POST /repair` on orchestrator -- submit a repair request.
  - Internally calls lean_env `/verify` to check each candidate.

## Example Usage
```
Use lean-repair to fix this failed proof:

Failed proof:
  theorem add_assoc (a b c : Nat) : a + b + c = a + (b + c) := by
    simp [Nat.add_assoc_wrong]

Diagnostics:
  [{"severity": "error", "message": "unknown identifier 'Nat.add_assoc_wrong'", "line": 2, "column": 9, "category": "unknown_identifier"}]

Goal: theorem add_assoc (a b c : Nat) : a + b + c = a + (b + c)
```

Expected: repair candidates that replace `Nat.add_assoc_wrong` with `Nat.add_assoc` or use alternative tactics like `omega`.

## Notes
- Repair strategies are selected based on the diagnostic category:
  - `unknown_identifier`: retrieve correct name via lean-retrieve, try fuzzy matches.
  - `type_mismatch`: adjust term types, add coercions, rewrite intermediate steps.
  - `elaboration_error`: simplify or restructure the tactic block.
  - `timeout`: break the proof into smaller lemmas or use more direct tactics.
- The `prior_attempts` field prevents the repair loop from cycling through the same failing approaches.
- Each candidate is verified before being returned, so `candidates[].status` is always populated.
- If no candidate achieves `verified`, the best candidate (fewest errors) is still returned so the next repair iteration has something to work with.
