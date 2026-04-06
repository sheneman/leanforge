# Skill: lean-retrieve

## Purpose
Searches the local theorem corpus, mathlib index, project-specific theorems, and prior successful proof traces to find relevant lemmas, theorems, and definitions. This provides the grounding context that synthesis needs to produce correct proofs.

## When to Use
- **FIRST step** before any proof synthesis attempt. Always retrieve before you synthesize.
- When the user asks "what lemmas are related to X?"
- When a proof attempt fails with `unknown_identifier` and you need to find the correct name.
- To look up a mathlib theorem by informal description.
- To find prior proof traces for similar goals.

## Inputs
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | Natural language description or theorem pattern to search for (e.g., `"commutativity of addition on natural numbers"` or `"Nat.add_comm"`). |
| `top_k` | int | no | Maximum number of results to return. Default: 10. |
| `sources` | list[enum] | no | Filter to specific sources: `mathlib`, `project`, `traces`. Default: all. |
| `type_filter` | string | no | Filter by type signature pattern (e.g., `"Nat -> Nat -> Prop"`). |

## Outputs
| Field | Type | Description |
|-------|------|-------------|
| `results` | list[object] | Ranked list of matches, each containing: |
| `results[].name` | string | Fully qualified theorem/lemma name. |
| `results[].statement` | string | Full Lean type signature / statement. |
| `results[].source` | enum | Where this result came from: `mathlib`, `project`, `traces`. |
| `results[].score` | float | Relevance score (0.0 to 1.0). |
| `results[].module` | string | The Lean module path (e.g., `Mathlib.Algebra.Group.Basic`). |
| `results[].doc` | string | Docstring if available. |

## Service Endpoint
- **URL:** `${RETRIEVAL_URL}`
- **Service:** retrieval
- **Key endpoints:**
  - `POST /search` -- semantic search over the corpus.
  - `POST /search/by_type` -- search filtered by type signature.
  - `GET /index/status` -- check whether the index is loaded and ready.

## Example Usage
```
Use lean-retrieve to find lemmas related to:

"If a list is non-empty, then its head exists"

Expected: results like List.head?, List.head_cons, List.ne_nil_iff, etc.
```

## Notes
- **Always call lean-retrieve before synthesizing a proof.** Retrieval-augmented synthesis dramatically outperforms ungrounded generation.
- If retrieval returns fewer than 3 results with score > 0.5, consider rephrasing the query or broadening the search.
- If retrieval is insufficient even after rephrasing, then (and only then) consider using the **web-search** skill as a fallback.
- The retrieval index must be loaded before queries work. Check `/index/status` if you get empty results unexpectedly.
- Results from `traces` source are prior successful proofs from this project and are especially valuable for recurring patterns.
- The `type_filter` is matched structurally, not syntactically, so minor formatting differences are tolerated.
