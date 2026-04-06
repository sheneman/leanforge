# Skill: lean-verify

## Purpose
Compiles a Lean 4 snippet or file against the project environment and returns a structured verification result. Classifies compiler diagnostics into actionable categories so downstream skills (repair, orchestrator) can react appropriately.

**MANDATORY: This skill MUST be called before any proof is presented as correct to the user. A proof that has not passed lean-verify is NOT verified, no matter how confident the LLM is. The LLM is not a proof checker — Lean is.**

## When to Use
- **Before presenting ANY proof as correct** — this is not optional.
- After synthesizing a proof candidate, to check whether it compiles.
- When a user pastes Lean code and asks "does this type-check?"
- As the verification step inside the orchestrator loop.
- To validate edits before writing them back to a file.

## Inputs
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | yes (either source or file_path) | Lean 4 source code to compile. |
| `file_path` | string | yes (either source or file_path) | Absolute path to a `.lean` file to compile. |
| `timeout_seconds` | int | no | Compilation timeout. Default: 30. |
| `imports` | list[string] | no | Additional imports to prepend. |

## Outputs
| Field | Type | Description |
|-------|------|-------------|
| `status` | enum | One of: `verified`, `elaboration_error`, `type_mismatch`, `unknown_identifier`, `timeout`, `other_error`. |
| `diagnostics` | list[object] | Each diagnostic contains `severity` (error/warning/info), `message`, `line`, `column`, and `category`. |
| `elapsed_ms` | int | Wall-clock compilation time in milliseconds. |
| `is_verified` | bool | Convenience flag: true when status is `verified` and no errors exist. |

## How to Determine Success or Failure
- **Success:** `is_verified` is `true` AND `status` is `"verified"` AND `diagnostics` contains no entries with `severity: "error"`. Only then may you present the proof as correct.
- **Failure:** Any other combination. Read the `diagnostics` array for error details, then feed them into lean-repair or fix manually, and call lean-verify again.

## Service Endpoint
- **URL:** `${LEAN_ENV_URL}`
- **Service:** lean_env
- **Key endpoints:**
  - `POST /verify` -- compile source and return diagnostics.
  - `POST /verify/file` -- compile a file on disk.

## Example: Verify via curl
```bash
# Verify a proof snippet
curl -X POST ${LEAN_ENV_URL}/verify \
  -H "Content-Type: application/json" \
  -d '{
    "source": "import Mathlib.Tactic\n\ntheorem test : 1 + 1 = 2 := by norm_num"
  }'
```

Successful response:
```json
{
  "status": "verified",
  "diagnostics": [],
  "elapsed_ms": 450,
  "is_verified": true
}
```

Failed response (proof has errors):
```json
{
  "status": "elaboration_error",
  "diagnostics": [
    {
      "severity": "error",
      "message": "unsolved goals\n⊢ 1 + 1 = 3",
      "line": 3,
      "column": 40,
      "category": "elaboration_error"
    }
  ],
  "elapsed_ms": 320,
  "is_verified": false
}
```

## Example: Verify via shell
```bash
# Write the proof to a temp file and compile directly
cat > /tmp/test_proof.lean << 'EOF'
import Mathlib.Tactic

theorem test : 1 + 1 = 2 := by norm_num
EOF
cd lean && lake env lean /tmp/test_proof.lean
```
If the command exits with code 0 and no error output, the proof is verified.

## Example Usage (skill invocation)
```
Use lean-verify to check whether this proof compiles:

theorem double_neg (p : Prop) [Decidable p] : ~~p -> p := by
  intro h
  exact Classical.byContradiction (fun hn => h hn)
```

## Notes
- **Always call this skill before claiming a proof is correct.** No exceptions.
- Always prefer sending `source` with full context (imports + declarations) rather than relying on `file_path` for ad-hoc checks.
- The diagnostic `category` field maps to the `status` enum: use it to decide whether to invoke lean-repair or lean-search.
- `timeout` status means the Lean server did not finish within the allotted time. Consider simplifying the proof or increasing the timeout.
- This skill does NOT modify any files. It is read-only / compile-only.
- If the lean_env service at ${LEAN_ENV_URL} is unreachable, surface the connection error.
