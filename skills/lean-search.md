# Skill: lean-search

## Purpose
Performs branch-and-bound search over the proof state space. Manages a search tree of tactic candidates, expanding promising branches and pruning dead ends. Uses lean-state to apply tactics and evaluate resulting goal states, guided by heuristic scoring.

## When to Use
- When a direct synthesis attempt fails and a systematic search over tactic combinations is needed.
- For complex proofs where the tactic sequence is not obvious.
- When the orchestrator decides that repair alone is insufficient and exploration is needed.
- NOT for simple proofs that can be solved by `simp`, `omega`, `decide`, or a single tactic.

## Inputs
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes (except create) | Identifier for an existing search task. |
| `action` | enum | yes | One of: `create`, `expand`, `get_best`, `prune`, `status`, `cancel`. |
| `theorem` | string | yes (create only) | The theorem statement to search a proof for. |
| `imports` | list[string] | no | Additional imports for the search environment. |
| `branch_id` | string | yes (expand/prune only) | Which branch to expand or prune. |
| `max_depth` | int | no | Maximum tactic depth to explore. Default: 15. |
| `max_branches` | int | no | Maximum concurrent branches. Default: 32. |
| `timeout_seconds` | int | no | Total search time budget. Default: 60. |

## Outputs
| Field | Type | Description |
|-------|------|-------------|
| `task_id` | string | The search task identifier. |
| `status` | enum | One of: `running`, `solved`, `exhausted`, `timeout`, `cancelled`. |
| `branches` | list[object] | Active branches with their goal states and scores. |
| `best_candidate` | object | The current best proof (complete if solved, partial otherwise). Contains `proof`, `goals_remaining`, `score`. |
| `stats` | object | Search statistics: `branches_explored`, `branches_pruned`, `elapsed_ms`. |

## Service Endpoint
- **URL:** `http://localhost:8102`
- **Service:** proof_search
- **Key endpoints:**
  - `POST /search/create` -- start a new search task.
  - `POST /search/{task_id}/expand` -- expand a branch.
  - `GET /search/{task_id}/best` -- get the current best candidate.
  - `POST /search/{task_id}/prune` -- prune a branch.
  - `GET /search/{task_id}/status` -- poll search progress.
  - `POST /search/{task_id}/cancel` -- cancel a running search.

## Example Usage
```
Use lean-search to find a proof for:

theorem list_reverse_involution (l : List a) : l.reverse.reverse = l

1. Create a search task with the theorem.
2. Poll status until solved, exhausted, or timeout.
3. Retrieve best_candidate.
```

## Notes
- Search is computationally expensive. Prefer direct synthesis and repair before resorting to search.
- The search service internally uses lean-state sessions for tactic application, so lean_env must be running.
- `max_branches` controls memory usage; reduce it if the lean_env server is under load.
- Branches are scored by a heuristic that considers: number of remaining goals, goal complexity, and similarity to known proof patterns.
- When status is `exhausted`, all reachable branches within `max_depth` have been explored without finding a complete proof.
- Cancel long-running searches promptly if a simpler approach becomes apparent.
