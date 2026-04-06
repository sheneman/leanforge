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
Do NOT delegate to a black-box orchestrator service. YOU drive the proof loop using the helper scripts. You can see intermediate results, reason about errors, and adapt your strategy.

## Helper Scripts
Use these instead of raw curl — they handle JSON escaping, .env loading, and Unicode automatically:

**Search mathlib for lemmas:**
```bash
python3 scripts/search.py "sum of two even numbers"
python3 scripts/search.py "continuous compact bounded" --top_k 5
```

**Call Leanstral for tactic suggestions:**
```bash
python3 scripts/synthesize.py "theorem even_add (a b : Nat) (ha : Even a) (hb : Even b) : Even (a + b)"
python3 scripts/synthesize.py "theorem test : ∀ n : Nat, n + 0 = n" --hints "Nat.add_zero exists in mathlib"
```

**Verify a proof (from file or inline):**
```bash
cat > /tmp/proof.lean << 'EOF'
import Mathlib.Tactic

theorem test : 1 + 1 = 2 := by norm_num
EOF
python3 scripts/verify.py /tmp/proof.lean
```

## Workflow
When asked to prove a theorem, follow these steps IN ORDER. Do not skip steps.

### Step 1: Read `.env`
Get the service URLs and API keys.

### Step 2: Search mathlib
```bash
python3 scripts/search.py "natural language description of the theorem"
```
Read the results. Note which lemma names and signatures might be useful.

### Step 3: Call Leanstral for tactic suggestions
**This step is MANDATORY.** Always call Leanstral before writing your own proof. Pass the theorem statement and the relevant lemmas from Step 2 as hints:
```bash
python3 scripts/synthesize.py "<theorem statement>" --hints "<lemma names and signatures from step 2>"
```
Read Leanstral's suggested tactics. You may use them directly, modify them, or combine them with your own ideas — but you must call Leanstral first.

### Step 4: Verify with Lean
Write the complete proof (imports + theorem + tactics) to a file and verify:
```bash
cat > /tmp/proof.lean << 'EOF'
import Mathlib.Tactic

<theorem statement> := by
  <tactics from Leanstral or your own>
EOF
python3 scripts/verify.py /tmp/proof.lean
```
- Prints `✓ VERIFIED` → present the proof to the user
- Prints `✗ FAILED` with diagnostics → go to Step 5

### Step 5: Repair
Read the error diagnostics printed by verify.py. Common errors and fixes:
- `unknown identifier` → wrong lemma name, search again with `scripts/search.py`
- `type mismatch` → argument types wrong, check the lemma signature from search results
- `unsolved goals` → proof incomplete, call Leanstral again with the error context
- `elaboration error` → structural issue, try a different approach

Fix the proof and re-run `python3 scripts/verify.py /tmp/proof.lean`. If your fix doesn't work, call Leanstral again with the error diagnostics:
```bash
python3 scripts/synthesize.py "<theorem statement>" --hints "Previous attempt failed with: <error message>. Relevant lemmas: <from search>"
```
Try up to 5 repair cycles before changing strategy entirely.

### Step 6: Web search (fallback only)
Only if retrieval returns nothing useful AND Leanstral + your own attempts fail.

## Anti-patterns
- Do NOT claim a proof is correct without running `scripts/verify.py`
- Do NOT skip retrieval — always run `scripts/search.py` first
- Do NOT skip Leanstral — always run `scripts/synthesize.py` before writing your own proof
- Do NOT ignore error diagnostics — read them and fix specifically
- Do NOT retry the same broken proof — change your approach or call Leanstral with error context
- Do NOT use web search before trying retrieval + Leanstral

## Example: Full Workflow

User: "Prove that the sum of two even numbers is even"

```bash
# Step 1: already read .env

# Step 2: search mathlib
python3 scripts/search.py "Even add sum two even numbers"
# → finds Even.add (score 0.83), Even.add_odd, even_iff_two_dvd

# Step 3: call Leanstral (MANDATORY)
python3 scripts/synthesize.py "theorem even_add (a b : Nat) (ha : Even a) (hb : Even b) : Even (a + b)" --hints "Even.add exists in Mathlib.Algebra.Group.Even"
# → Leanstral suggests: exact Even.add ha hb

# Step 4: verify
cat > /tmp/proof.lean << 'EOF'
import Mathlib.Tactic

theorem even_add (a b : Nat) (ha : Even a) (hb : Even b) : Even (a + b) := by
  exact Even.add ha hb
EOF
python3 scripts/verify.py /tmp/proof.lean
# → ✓ VERIFIED

# Step 5: present the verified proof
```
