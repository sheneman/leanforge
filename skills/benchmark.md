# Skill: benchmark

## Purpose
Runs benchmark theorems from the `lean/ForgeLean/Bench/` directory through the full orchestrator pipeline. Measures success rates, timing, and identifies common failure modes across a suite of test theorems. Used for evaluating and improving the proving pipeline.

## When to Use
- When evaluating the overall performance of the LeanForge pipeline.
- After making changes to synthesis, retrieval, or repair to measure impact.
- When the user asks "how well does the prover work?" or requests a benchmark run.
- For regression testing before deploying updates.

## Inputs
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `suite` | string | yes | Benchmark suite name (corresponds to a subdirectory or file in `lean/ForgeLean/Bench/`) or `"all"` to run every available benchmark. |
| `timeout_per_theorem` | int | no | Timeout in seconds for each theorem. Default: 120. |
| `parallel` | int | no | Number of theorems to prove in parallel. Default: 4. |
| `filter` | string | no | Regex pattern to select a subset of theorems by name. |

## Outputs
| Field | Type | Description |
|-------|------|-------------|
| `summary` | object | Aggregate statistics: `total`, `verified`, `failed`, `timeout`, `success_rate`, `mean_time_ms`, `median_time_ms`. |
| `results` | list[object] | Per-theorem results, each containing: |
| `results[].name` | string | Theorem name. |
| `results[].status` | enum | One of: `verified`, `failed`, `timeout`. |
| `results[].time_ms` | int | Time taken in milliseconds. |
| `results[].attempts` | int | Number of synthesis/repair attempts used. |
| `results[].failure_mode` | string | If failed, the primary failure category (e.g., `unknown_identifier`, `type_mismatch`, `search_exhausted`). |
| `failure_analysis` | object | Breakdown of failures by category with counts and example theorems. |

## Service Endpoint
- **URL:** `${ORCHESTRATOR_URL}`
- **Service:** orchestrator
- **Key endpoints:**
  - `POST /benchmark/run` -- start a benchmark run.
  - `GET /benchmark/{run_id}/status` -- poll progress.
  - `GET /benchmark/{run_id}/report` -- retrieve the final report.

## Example Usage
```
Use benchmark to evaluate the prover on all available benchmarks:

  suite: "all"
  timeout_per_theorem: 120
  parallel: 4

Or run a specific suite:
  suite: "basic_nat"

Or filter to specific theorems:
  suite: "all"
  filter: ".*comm.*"
```

## Notes
- Benchmark runs can take a long time depending on the suite size and timeout settings. Monitor via the status endpoint.
- The `failure_analysis` output is especially useful for identifying systematic weaknesses (e.g., if most failures are `unknown_identifier`, the retrieval index may need updating).
- Benchmark theorems are stored in `lean/ForgeLean/Bench/` as standard Lean files with `sorry` placeholders.
- Results are deterministic for a given pipeline configuration but may vary if retrieval indices are updated.
- Running benchmarks consumes significant compute on the lean_env and proof_search services. Avoid running benchmarks during active proving sessions.
- Compare benchmark results across runs to track improvement over time.
