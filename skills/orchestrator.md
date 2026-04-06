# Skill: orchestrator

## Purpose
YOU are the orchestrator. This skill describes the prove loop you should execute directly using shell/fetch to call services. Do not delegate to a black-box service — drive the loop yourself so you can see intermediate results, read error messages, and adapt your strategy.

## When to Use
When asked to prove a theorem, demonstrate a proof, or verify a mathematical statement in Lean 4.

## The Loop

### 1. Read `.env` for service URLs
Get `LEAN_ENV_URL`, `RETRIEVAL_URL`, `LLM_API_BASE`, `LLM_API_KEY`, `LEANSTRAL_API_MODEL`.

### 2. Retrieve relevant lemmas
```bash
curl -s -X POST ${RETRIEVAL_URL}/search \
  -H "Content-Type: application/json" \
  -d '{"query": "natural language description of the theorem", "top_k": 10}'
```
Read the results. Note which lemma names and signatures might be useful.

### 3. Write or synthesize a proof
Either write a proof yourself using the retrieved lemmas, or call Leanstral:
```bash
curl -s -X POST ${LLM_API_BASE}/chat/completions \
  -H "Authorization: Bearer ${LLM_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model": "${LEANSTRAL_API_MODEL}", "messages": [
    {"role": "system", "content": "You are a Lean 4 proof assistant. Return ONLY tactic-mode proof body. No imports, no theorem declaration, no code fences."},
    {"role": "user", "content": "Prove: <statement>\nRelevant lemmas: <retrieval results>"}
  ], "max_tokens": 1024}'
```

### 4. Verify with Lean
```bash
curl -s -X POST ${LEAN_ENV_URL}/compile \
  -H "Content-Type: application/json" \
  -d '{"source": "import Mathlib.Tactic\n\n<theorem> := by\n  <tactics>"}'
```
- `"success": true` → proof is verified, present it
- `"success": false` → read `diagnostics`, go to step 5

### 5. Repair
Read the error diagnostics. Fix the specific problem:
- `unknown identifier` → search retrieval for the correct lemma name
- `type mismatch` → check the lemma signature, fix argument types
- `unsolved goals` → add more tactics
- `elaboration error` → try a different approach

Go back to step 4. Try up to 5 repair cycles before changing strategy entirely.

## Key Principle
You can see every intermediate result. Use that advantage — don't blindly retry.

## Batch Mode (optional)
For unattended batch proving, the orchestrator service is available at `${ORCHESTRATOR_URL}`:
```bash
curl -X POST ${ORCHESTRATOR_URL}/tasks -d '{"theorem_statement": "..."}'
curl -X POST ${ORCHESTRATOR_URL}/tasks/{id}/run
```
