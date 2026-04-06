# Skill: lean-verify

## Purpose
Compiles Lean 4 source code and returns whether it type-checks. This is the only way to verify a proof is correct.

**You MUST call this before presenting any proof as correct.**

## When to Use
- Before presenting ANY proof to the user
- After writing or synthesizing a proof candidate
- When a user asks "does this type-check?"

## How to Use
Read `LEAN_ENV_URL` from `.env`, then:
```bash
curl -s -X POST ${LEAN_ENV_URL}/compile \
  -H "Content-Type: application/json" \
  -d '{"source": "import Mathlib.Tactic\n\ntheorem test : 1 + 1 = 2 := by norm_num"}'
```

## Reading the Response
- `"success": true, "diagnostics": []` → **VERIFIED**. Safe to present.
- `"success": false` → **FAILED**. Read the `diagnostics` array:
  - Each diagnostic has `severity`, `message`, `line`, `column`, `category`
  - `category` tells you the error type: `type_mismatch`, `unknown_identifier`, `elaboration_error`, `timeout`
  - Use these to fix the proof and try again

## Important
- Always include `import Mathlib.Tactic` (or appropriate imports) in the source
- Send the COMPLETE Lean source — theorem declaration + proof body
- Compilation can take 10-30 seconds (mathlib imports are large)
