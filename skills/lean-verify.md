# Skill: lean-verify

## Purpose
Compiles a Lean 4 snippet or file against the project environment and returns a structured verification result. Classifies compiler diagnostics into actionable categories so downstream skills (repair, orchestrator) can react appropriately.

## When to Use
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

## Service Endpoint
- **URL:** `http://localhost:8101`
- **Service:** lean_env
- **Key endpoints:**
  - `POST /verify` -- compile source and return diagnostics.
  - `POST /verify/file` -- compile a file on disk.

## Example Usage
```
Use lean-verify to check whether this proof compiles:

theorem double_neg (p : Prop) [Decidable p] : ~~p -> p := by
  intro h
  exact Classical.byContradiction (fun hn => h hn)
```

Expected output if correct:
```json
{
  "status": "verified",
  "diagnostics": [],
  "elapsed_ms": 450,
  "is_verified": true
}
```

## Notes
- Always prefer sending `source` with full context (imports + declarations) rather than relying on `file_path` for ad-hoc checks.
- The diagnostic `category` field maps to the `status` enum: use it to decide whether to invoke lean-repair or lean-search.
- `timeout` status means the Lean server did not finish within the allotted time. Consider simplifying the proof or increasing the timeout.
- This skill does NOT modify any files. It is read-only / compile-only.
- If the lean_env service at localhost:8101 is unreachable, surface the connection error.
