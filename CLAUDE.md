# forge-lean-prover — Project Instructions

## Core Rule
**No proof is accepted unless verified by Lean 4 compilation.**
Do not output a proof and claim it is correct without compiling it through the lean_env service or `lean` binary. The LLM is NOT the source of truth for proof correctness — only `lean` is.

## Workflow
When asked to prove a theorem, you MUST follow this exact sequence:

1. **Retrieve** — Call the retrieval service (POST localhost:8103/search) or use the lean-retrieve skill to find relevant lemmas from mathlib and the local corpus. Do this FIRST.

2. **Synthesize** — Call the orchestrator service (POST localhost:8100/tasks then POST localhost:8100/tasks/{id}/run) to get candidate proofs from Leanstral. Do NOT write proofs yourself — use the synthesis service. If the orchestrator is not running, you may write a candidate yourself but you MUST verify it in step 3.

3. **Verify** — Compile EVERY candidate proof with Lean before presenting it as correct. Use one of:
   - POST localhost:8101/compile with the full Lean source
   - Run `cd lean && lake env lean <file>` in the shell
   - Use the lean-verify skill
   
   If compilation fails, go to step 4. If it succeeds with no errors, the proof is verified.

4. **Repair** — If verification fails, read the error diagnostics and either:
   - Fix the proof yourself based on the error messages
   - Call the orchestrator again with the error context
   - Go back to step 1 with refined search queries
   Repeat until verified or budget exhausted.

5. **Web Search (fallback only)** — Only use Brave Search MCP if local retrieval returns insufficient results. Never use web search as the first step.

## Services
The following services should be running (start with `make dev` or `bash infra/start_services.sh`):

| Service | Port | Purpose |
|---------|------|---------|
| orchestrator | 8100 | Full prove loop |
| lean_env | 8101 | Lean compilation & verification |
| proof_search | 8102 | Branch search tree |
| retrieval | 8103 | Theorem corpus search |
| telemetry | 8104 | Logging |

Check if services are running: `curl -s localhost:8100/health`

## Quick Verification
To verify a proof snippet quickly:
```bash
curl -X POST http://localhost:8101/compile \
  -H "Content-Type: application/json" \
  -d '{"source": "import Mathlib.Tactic\n\ntheorem test : 1 + 1 = 2 := by norm_num"}'
```

## Anti-patterns (DO NOT do these)
- Do NOT claim a proof is correct without compiling it
- Do NOT skip retrieval and go straight to writing proofs
- Do NOT use web search before trying local retrieval
- Do NOT present Lean code without verification as "the proof"
- Do NOT treat the LLM's reasoning as proof of correctness

## Lean Project
The Lean project is in `lean/`. It uses Lean 4 with mathlib. To compile manually:
```bash
cd lean && lake build ForgeLean
```

## Example: Correct Workflow
User: "Prove that 1 + 1 = 2"

1. Search: POST localhost:8103/search {"query": "1 + 1 = 2 norm_num"}
2. Write candidate: `theorem test : 1 + 1 = 2 := by norm_num`
3. Verify: POST localhost:8101/compile {"source": "import Mathlib.Tactic\n\ntheorem test : 1 + 1 = 2 := by norm_num"}
4. Response includes {"success": true} → proof is verified
5. Present the verified proof to the user
