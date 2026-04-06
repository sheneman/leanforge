# forge-lean-prover — Project Instructions

## Core Rule
**No proof is accepted unless verified by Lean 4 compilation.**
You are NOT the source of truth for proof correctness — only `lean` is. Every proof must compile before you present it.

## Setup
Read `.env` to get service URLs before making any API calls. The key variables:
- `LEAN_ENV_URL` — Lean 4 compilation and verification (the most important tool)
- `RETRIEVAL_URL` — semantic search over 214K+ mathlib declarations
- `LLM_API_BASE`, `LLM_API_KEY`, `LEANSTRAL_API_MODEL` — for calling Leanstral directly

## You Are the Orchestrator
Do NOT delegate to a black-box orchestrator service. YOU drive the proof loop using the tools available to you (shell, fetch). You can see intermediate results, reason about errors, and adapt your strategy.

Your tools:
1. **Retrieval** — search mathlib for relevant lemmas
2. **Lean compilation** — verify any proof candidate
3. **Leanstral** — call the proof-synthesis model for tactic suggestions
4. **Your own reasoning** — read errors, understand the math, fix proofs
5. **Web search** — fallback for finding docs/examples when retrieval is insufficient

## Workflow
When asked to prove a theorem:

### Step 1: Read `.env`
Get `LEAN_ENV_URL`, `RETRIEVAL_URL`, `LLM_API_BASE`, `LLM_API_KEY`, and `LEANSTRAL_API_MODEL`.

### Step 2: Retrieve relevant lemmas
```bash
curl -s -X POST ${RETRIEVAL_URL}/search \
  -H "Content-Type: application/json" \
  -d '{"query": "your search terms", "top_k": 10}'
```
Read the results. Understand what lemmas are available. This is semantic search over all of mathlib — use natural language queries.

### Step 3: Write or synthesize a proof candidate
You have two options:
- **Write it yourself** using the retrieved lemmas and your knowledge of Lean 4
- **Call Leanstral** for tactic suggestions:
```bash
curl -s -X POST ${LLM_API_BASE}/chat/completions \
  -H "Authorization: Bearer ${LLM_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "${LEANSTRAL_API_MODEL}",
    "messages": [
      {"role": "system", "content": "You are a Lean 4 proof assistant. Return ONLY tactic-mode proof body (no imports, no theorem declaration, no code fences). Just the tactics after := by"},
      {"role": "user", "content": "Prove: <theorem statement>\n\nRelevant lemmas: <paste retrieval results>"}
    ],
    "max_tokens": 2048,
    "temperature": 0.6
  }'
```
Extract the tactic body from Leanstral's response.

### Step 4: Verify with Lean
Build the full Lean source and compile it:
```bash
curl -s -X POST ${LEAN_ENV_URL}/compile \
  -H "Content-Type: application/json" \
  -d '{"source": "import Mathlib.Tactic\n\n<theorem statement> := by\n  <tactics>"}'
```
Check the response:
- `"success": true` → **proof is verified**, present it to the user
- `"success": false` → read the `diagnostics` array, go to Step 5

### Step 5: Repair
Read the error diagnostics carefully. Common errors and fixes:
- `unknown identifier` → wrong lemma name, search retrieval for the correct one
- `type mismatch` → argument types don't match, check the lemma signature
- `unsolved goals` → proof is incomplete, add more tactics
- `elaboration error` → structural issue, try a different approach

Fix the proof based on the specific error, then go back to Step 4. Try up to 5 repair attempts before trying a completely different approach.

### Step 6: Web search (fallback only)
Only if retrieval returns nothing useful AND your own attempts fail. Use Brave Search to find:
- Correct mathlib lemma names
- Proof strategies for this type of theorem
- Similar proofs in mathlib or other Lean projects

Then go back to Step 3 with new information.

## Quick Reference

**Verify a proof:**
```bash
curl -s -X POST ${LEAN_ENV_URL}/compile \
  -H "Content-Type: application/json" \
  -d '{"source": "import Mathlib.Tactic\n\ntheorem test : 1 + 1 = 2 := by norm_num"}'
```

**Search mathlib:**
```bash
curl -s -X POST ${RETRIEVAL_URL}/search \
  -H "Content-Type: application/json" \
  -d '{"query": "continuous function compact bounded", "top_k": 10}'
```

**Call Leanstral:**
```bash
curl -s -X POST ${LLM_API_BASE}/chat/completions \
  -H "Authorization: Bearer ${LLM_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model": "${LEANSTRAL_API_MODEL}", "messages": [{"role": "user", "content": "prove: ..."}], "max_tokens": 1024}'
```

## Anti-patterns
- Do NOT claim a proof is correct without compiling it
- Do NOT skip retrieval — always search mathlib first
- Do NOT ignore error diagnostics — read them and fix specifically
- Do NOT retry the same broken proof — change your approach
- Do NOT use web search before trying retrieval + Leanstral

## Example: Full Workflow

User: "Prove that the sum of two even numbers is even"

1. Read `.env` → get URLs
2. Search: `{"query": "Even add sum two even numbers"}` → finds `Even.add`, `Even.add_odd`, `even_iff_two_dvd`
3. See that `Even.add` exists — try: `exact Even.add ha hb`
4. Build source:
   ```lean
   import Mathlib.Tactic
   theorem even_add (a b : Nat) (ha : Even a) (hb : Even b) : Even (a + b) := by
     exact Even.add ha hb
   ```
5. Compile → `{"success": true}` ✓
6. Present: "The proof is `exact Even.add ha hb`, verified by Lean 4."
