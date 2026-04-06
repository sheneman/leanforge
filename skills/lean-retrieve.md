# Skill: lean-retrieve

## Purpose
Semantic search over 214K+ mathlib declarations. Finds relevant lemmas, theorems, and definitions by meaning, not just keyword matching.

## When to Use
- **FIRST step** before any proof attempt — always retrieve before you synthesize
- When you need to find the correct mathlib lemma name
- When a proof fails with `unknown_identifier`
- When you want to know what's available in mathlib for a topic

## How to Use
Read `RETRIEVAL_URL` from `.env`, then:
```bash
curl -s -X POST ${RETRIEVAL_URL}/search \
  -H "Content-Type: application/json" \
  -d '{"query": "sum of two even numbers is even", "top_k": 10}'
```

Use natural language queries — the search is semantic (vector similarity), not keyword-based. Examples:
- `"continuous function on compact set is bounded"` → finds `IsCompact.exists_bound_of_continuousOn'`
- `"prime number greater than one"` → finds `Nat.Prime.one_lt`
- `"list reverse is involution"` → finds `List.reverse_reverse`

## Reading the Response
Results are ranked by relevance score. Each result has:
- `name` — the Lean declaration name (e.g., `Even.add`)
- `statement` — the full type signature
- `module` — which mathlib module it's in
- `score` — relevance (higher is better)

## Tips
- If results are poor, try rephrasing: use mathlib terminology, mention type names
- If you get fewer than 3 good results, try a broader query
- If retrieval is insufficient after rephrasing, use web-search as fallback
