# Skill: lean-state

## Purpose
Manages interactive Lean 4 proof sessions backed by Pantograph. Allows creating a proof session, applying individual tactics, inspecting the current goal state, and snapshotting/restoring intermediate states. This enables fine-grained, step-by-step proof construction.

## When to Use
- When building a proof tactic-by-tactic rather than submitting a complete proof term.
- During proof search (lean-search) to explore tactic branches interactively.
- When the user asks to step through a proof or inspect intermediate goals.
- To snapshot a known-good state before trying a speculative tactic.

## Inputs
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | enum | yes | One of: `create_session`, `apply_tactic`, `get_goals`, `snapshot`, `restore`, `close_session`. |
| `session_id` | string | yes (except create_session) | Identifier for an existing session. |
| `theorem` | string | yes (create_session only) | The theorem statement to open a proof session for. |
| `imports` | list[string] | no | Additional imports for the session environment. |
| `tactic` | string | yes (apply_tactic only) | The tactic string to apply (e.g., `"intro h"`). |
| `snapshot_id` | string | yes (restore only) | Identifier of a previously saved snapshot to restore. |

## Outputs
| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | The session identifier (returned on create). |
| `goals` | list[string] | Current open goals after the action, rendered as Lean goal strings. |
| `tactic_result` | enum | One of: `success`, `failure`, `no_goals` (proof complete). |
| `error` | string | Error message if the tactic failed. |
| `snapshot_id` | string | Identifier for a saved snapshot (returned on snapshot). |

## Service Endpoint
- **URL:** `${LEAN_ENV_URL}`
- **Service:** lean_env (Pantograph interface)
- **Key endpoints:**
  - `POST /session/create` -- open a new proof session.
  - `POST /session/{session_id}/tactic` -- apply a tactic.
  - `GET /session/{session_id}/goals` -- retrieve current goals.
  - `POST /session/{session_id}/snapshot` -- save current state.
  - `POST /session/{session_id}/restore` -- restore a saved state.
  - `DELETE /session/{session_id}` -- close and free the session.

## Example Usage
```
Use lean-state to interactively prove:

theorem and_comm (p q : Prop) : p /\ q -> q /\ p

Steps:
1. create_session with the theorem.
2. apply_tactic "intro h"
3. apply_tactic "exact And.mk h.2 h.1"
4. Confirm tactic_result is "no_goals".
5. close_session.
```

## Notes
- Sessions consume server memory. Always close sessions when finished.
- Snapshots are lightweight but also accumulate; clean up when a branch is abandoned.
- The lean-search skill relies heavily on lean-state to explore tactic trees.
- Tactic strings must be valid Lean 4 tactic syntax. Lean 3 syntax will fail.
- If a tactic fails, the goal state is unchanged; you do not need to restore.
- Maximum concurrent sessions is limited by the lean_env server configuration.
