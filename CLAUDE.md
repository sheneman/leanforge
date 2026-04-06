# forge-lean-prover — Project Instructions

## Core Rule
**No proof is accepted unless verified by Lean 4 compilation.**
Do not output a proof and claim it is correct without compiling it through the lean_env service or `lean` binary. The LLM is NOT the source of truth for proof correctness — only `lean` is.

## Service URLs
Service endpoints are configured in `.env`. **Read `.env` first** to get the current URLs before making any API calls. The key variables are:
- `ORCHESTRATOR_URL` — proof task orchestration
- `LEAN_ENV_URL` — Lean 4 compilation and verification
- `RETRIEVAL_URL` — theorem search (semantic search over 214K+ mathlib declarations)
- `PROOF_SEARCH_URL` — branch search tree
- `TELEMETRY_URL` — logging and metrics

## Workflow
When asked to prove a theorem, you MUST follow this exact sequence:

1. **Read `.env`** to get service URLs.

2. **Retrieve** — POST `${RETRIEVAL_URL}/search` with `{"query": "your search terms", "top_k": 10}` to find relevant lemmas from mathlib. Do this FIRST.

3. **Synthesize** — POST `${ORCHESTRATOR_URL}/tasks` to submit a proof task, then POST `${ORCHESTRATOR_URL}/tasks/{id}/run` to get candidate proofs from Leanstral. If the orchestrator is unavailable, you may write a candidate yourself but you MUST verify it in step 4.

4. **Verify** — Compile EVERY candidate proof with Lean before presenting it as correct:
   ```bash
   curl -X POST ${LEAN_ENV_URL}/compile \
     -H "Content-Type: application/json" \
     -d '{"source": "import Mathlib.Tactic\n\ntheorem test : 1 + 1 = 2 := by norm_num"}'
   ```
   If `"success": true`, the proof is verified. If not, go to step 5.

5. **Repair** — Read the error diagnostics, fix the proof, and go back to step 4. Repeat until verified or budget exhausted.

6. **Web Search (fallback only)** — Only use Brave Search MCP if retrieval returns insufficient results. Never as the first step.

## Anti-patterns (DO NOT do these)
- Do NOT claim a proof is correct without compiling it
- Do NOT skip retrieval and go straight to writing proofs
- Do NOT use web search before trying local retrieval
- Do NOT present Lean code without verification as "the proof"
- Do NOT treat the LLM's reasoning as proof of correctness

## Example: Correct Workflow
User: "Prove that 1 + 1 = 2"

1. Read `.env` to get `RETRIEVAL_URL` and `LEAN_ENV_URL`
2. Search: POST `${RETRIEVAL_URL}/search` with `{"query": "norm_num arithmetic"}`
3. Write candidate: `theorem test : 1 + 1 = 2 := by norm_num`
4. Verify: POST `${LEAN_ENV_URL}/compile` with the full Lean source
5. Response includes `{"success": true}` → proof is verified ✓
6. Present the verified proof to the user
