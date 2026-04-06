# forge-lean-prover

A multi-agent Lean 4 theorem-proving system designed to run inside ForgeCode.

## Architecture

```
                        +------------------+
                        |    ForgeCode     |
                        |  (Claude Code)   |
                        +--------+---------+
                                 |
                    MCP tools / skills / prompts
                                 |
            +--------------------+--------------------+
            |                    |                    |
    +-------v-------+   +-------v-------+   +-------v-------+
    |  orchestrator  |   | lean-verify   |   | lean-retrieve |
    |  (skill)       |   | (skill)       |   | (skill)       |
    +-------+--------+   +-------+-------+   +-------+-------+
            |                    |                    |
    +-------v--------+  +-------v--------+  +--------v-------+
    |  orchestrator   |  |   lean_env     |  |   retrieval    |
    |  service :8100  |  |  service :8101 |  |  service :8103 |
    +-------+---------+  +-------+--------+  +--------+-------+
            |                    |                    |
            |            +-------v--------+           |
            +----------->| proof_search   |<----------+
            |            | service :8102  |
            |            +----------------+
            |
    +-------v--------+
    |   telemetry     |       +-----------------+
    |  service :8104  |       |  Brave MCP      |
    +----------------+        |  (web fallback) |
                              +-----------------+
            |
    +-------v--------+
    |    Lean 4       |
    |  + Mathlib      |
    +----------------+
```

**Key principles:**

- Lean 4 is the only source of proof truth -- every candidate is compiled.
- Retrieve first, synthesize second, verify every step.
- Web search (Brave MCP) is a fallback only, used when local retrieval is insufficient.
- Branch-and-bound search with configurable budget limits prevents runaway exploration.

## Components

| Component | Role |
|-----------|------|
| **orchestrator** | Coordinates the full prove loop: retrieve, synthesize, verify, repair. |
| **lean_env** | Compiles Lean 4 snippets, classifies diagnostics, manages Pantograph sessions. |
| **proof_search** | Maintains a best-first search tree over proof branches with scoring and expansion. |
| **retrieval** | Searches a theorem corpus (hardcoded fallback now, vector index planned). |
| **telemetry** | Collects structured events for observability, debugging, and benchmarking. |

## Prerequisites

- macOS or Linux
- [elan](https://github.com/leanprover/elan) (Lean version manager)
- Python 3.11+
- Node.js 18+ (for MCP servers, optional)
- Git

## Quick Start

```bash
git clone <repo-url> forge-lean-prover
cd forge-lean-prover
cp .env.example .env
# Edit .env with your API keys
make bootstrap
make smoke
make dev
```

`make bootstrap` installs Python dependencies, builds the Lean project with `lake build`, and prepares the data directories. `make smoke` runs unit tests and health-checks each service. `make dev` starts all services in the foreground.

## Environment Variables

All configuration is managed through environment variables. Copy `.env.example` to `.env` and fill in the values.

| Variable | Default | Description |
|----------|---------|-------------|
| `LEAN_BIN` | `lean` | Path to the Lean 4 binary. |
| `LAKE_BIN` | `lake` | Path to the Lake build tool. |
| `LEAN_PROJECT_DIR` | `""` | Root of the Lean project (the `lean/` subdirectory). |
| `COMPILE_TIMEOUT` | `60` | Max seconds for a single Lean compilation. |
| `RETRIEVAL_URL` | `http://localhost:8103` | Base URL of the retrieval service. |
| `LEAN_ENV_URL` | `http://localhost:8101` | Base URL of the lean_env service. |
| `PROOF_SEARCH_URL` | `http://localhost:8102` | Base URL of the proof_search service. |
| `TELEMETRY_URL` | `http://localhost:8104` | Base URL of the telemetry service. |
| `INDEX_PATH` | `data/corpus/index.faiss` | Path to the FAISS vector index file. |
| `TELEMETRY_LOG_DIR` | `data/logs` | Directory for telemetry event logs. |
| `TELEMETRY_FLUSH_THRESHOLD` | `1000` | Number of events buffered before auto-flush. |
| `LLM_API_KEY` | `""` | API key for the LLM provider (OpenAI-compatible). |
| `LLM_API_BASE` | `""` | Base URL for the LLM provider (e.g. `https://api.openai.com/v1`). |
| `LLM_API_MODEL` | `""` | Model ID for the orchestrator planner. |
| `LEANSTRAL_API_MODEL` | `""` | Model ID for proof synthesis. |
| `BRAVE_API_KEY` | `""` | API key for Brave Search MCP fallback. |

## Services

| Service | Port | Description | Key Endpoints |
|---------|------|-------------|---------------|
| orchestrator | 8100 | Prove-loop coordinator | `POST /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/step` |
| lean_env | 8101 | Lean compilation and diagnostics | `POST /compile`, `POST /compile-file`, `POST /session/create`, `POST /session/{id}/tactic` |
| proof_search | 8102 | Search tree management | `POST /branches`, `GET /branches/{task_id}`, `POST /branches/{id}/children`, `GET /best/{task_id}` |
| retrieval | 8103 | Theorem corpus search | `POST /search`, `POST /index`, `GET /stats` |
| telemetry | 8104 | Event collection and metrics | `POST /events`, `GET /events/{task_id}`, `GET /metrics`, `POST /flush` |

All services expose `GET /health` returning `{"status": "ok", "service": "<name>"}`.

## Using with ForgeCode

1. Open the `forge-lean-prover` project directory in ForgeCode.
2. MCP tools load automatically from `.mcp.json` in the project root.
3. Use the provided skills: `orchestrator`, `lean-verify`, `lean-state`, and others in the `skills/` directory.
4. ForgeCode reads skill definitions from `skills/*.md` and maps them to the appropriate service endpoints.

**Example workflow:** ask ForgeCode to prove a theorem and it will invoke the orchestrator skill, which retrieves relevant lemmas, synthesizes candidates, compiles each against Lean 4, and repairs failures automatically.

### Reloading MCP

If you modify `.mcp.json` or add new skills:

1. Open the ForgeCode command palette.
2. Run "Reload MCP Configuration" (or restart the ForgeCode window).
3. Verify the tools appear in the MCP tool list.

## Example Theorem Prompts

Here are example prompts you might give ForgeCode:

1. **Simple arithmetic:**
   > Prove that addition is commutative: `theorem add_comm (n m : Nat) : n + m = m + n`

2. **Using Mathlib tactics:**
   > Prove this ring identity: `theorem ring_example (a b : Int) : (a + b) * (a + b) = a * a + 2 * a * b + b * b`

3. **List property:**
   > Prove that appending an empty list is the identity: `theorem append_nil (l : List a) : l ++ [] = l`

4. **With context:**
   > Given `variable (G : Type) [Group G]`, prove `theorem mul_left_cancel (a b c : G) (h : a * b = a * c) : b = c`

## Running Benchmarks

```bash
make bench
```

This runs `tests/e2e/run_benchmarks.py`, which submits each theorem in its `BENCHMARKS` list to the orchestrator, polls for completion, and prints a summary table.

Benchmark theorem files live under `lean/ForgeLean/Bench/`:

| File | Domain |
|------|--------|
| `Algebra.lean` | Basic algebraic identities |
| `NumberTheory.lean` | Number-theoretic lemmas |
| `Topology.lean` | Topological space properties |
| `Countability.lean` | Countability and cardinality |

Results are saved to `data/logs/benchmark_results.json`.

## Inspecting Traces and Logs

- **Telemetry events** are written to `data/logs/` as JSONL files (`events_*.jsonl`) when the buffer is flushed.
- **Service logs** use `structlog` for structured JSON logging. Pipe service output through `jq` for readable formatting.
- **Benchmark results** are stored in `data/logs/benchmark_results.json` after each benchmark run.
- **Proof traces** (future) will be written to `data/traces/` for detailed step-by-step inspection.
- Query live telemetry via `GET /events/{task_id}` or `GET /metrics` on the telemetry service (port 8104).

## Development

### Running Tests

```bash
# All tests
make test

# Unit tests only
pytest tests/unit/ -v

# Integration tests only
pytest tests/integration/ -v

# With coverage
pytest --cov=services tests/
```

### Formatting

```bash
make fmt
```

### Adding New Theorems

1. Add the Lean statement to the appropriate file under `lean/ForgeLean/Bench/`.
2. Add a corresponding entry to `BENCHMARKS` in `tests/e2e/run_benchmarks.py`.
3. Run `make bench` to verify.

### Adding New Services

1. Create a new directory under `services/<name>/` with `__init__.py` and `main.py`.
2. Define a FastAPI app with a `/health` endpoint.
3. Add the service URL to `.env.example` and document it above.
4. Add a health-check test in `tests/integration/test_services.py`.
5. Wire it into the orchestrator if needed.

## Repo Structure

```
forge-lean-prover/
+-- config/
|   +-- models/              # Model configuration files
|   +-- prompts/             # Prompt templates
+-- data/
|   +-- cache/               # Cached compilation results
|   +-- corpus/              # Theorem corpus and FAISS index
|   +-- logs/                # Telemetry event logs and benchmark results
|   +-- traces/              # Proof trace files (future)
|   +-- vectors/             # Embedding vectors (future)
+-- infra/                   # Infrastructure and deployment configs
+-- lean/
|   +-- ForgeLean.lean       # Root Lean module
|   +-- ForgeLean/
|   |   +-- Basic.lean       # Core definitions
|   |   +-- Scratch.lean     # Scratch workspace
|   |   +-- Bench/
|   |   |   +-- Algebra.lean
|   |   |   +-- Countability.lean
|   |   |   +-- NumberTheory.lean
|   |   |   +-- Topology.lean
|   |   +-- Examples/
|   |   |   +-- HumanProofs.lean
|   |   +-- Generated/
|   |       +-- CandidateProofs.lean
|   +-- lakefile.toml
|   +-- lean-toolchain
+-- services/
|   +-- __init__.py
|   +-- schemas.py            # Shared Pydantic schemas
|   +-- orchestrator/
|   |   +-- __init__.py
|   |   +-- main.py           # Orchestrator FastAPI app
|   +-- lean_env/
|   |   +-- __init__.py
|   |   +-- main.py           # Lean environment FastAPI app
|   +-- proof_search/
|   |   +-- __init__.py
|   |   +-- main.py           # Proof search FastAPI app
|   +-- retrieval/
|   |   +-- __init__.py
|   |   +-- indexer.py         # Index builder (future)
|   |   +-- main.py           # Retrieval FastAPI app
|   +-- telemetry/
|       +-- __init__.py
|       +-- main.py           # Telemetry FastAPI app
+-- skills/
|   +-- orchestrator.md       # Orchestrator skill definition
|   +-- lean-verify.md        # Verification skill definition
|   +-- lean-state.md         # State management skill definition
+-- tests/
|   +-- __init__.py
|   +-- conftest.py           # Shared pytest fixtures
|   +-- unit/
|   |   +-- __init__.py
|   |   +-- test_schemas.py
|   |   +-- test_lean_env.py
|   |   +-- test_proof_search.py
|   |   +-- test_retrieval.py
|   +-- integration/
|   |   +-- __init__.py
|   |   +-- test_services.py
|   +-- e2e/
|       +-- __init__.py
|       +-- run_benchmarks.py
+-- README.md
```

## TODOs and Next Steps

- Connect real Nemotron and Leanstral model endpoints for proof synthesis and repair.
- Implement the Pantograph adapter for structured tactic-level interaction with Lean.
- Build a vector index with sentence-transformers and FAISS to replace the hardcoded fallback corpus.
- Add LeanCopilot integration as an alternative synthesis backend.
- Expand the benchmark suite with more theorems across algebra, analysis, and topology.
- Add a CI/CD pipeline with automated testing, Lean compilation checks, and benchmark regression tracking.
- Implement proof minimization in the search tree (remove redundant tactics from verified proofs).
- Add WebSocket support for streaming proof progress to ForgeCode.
