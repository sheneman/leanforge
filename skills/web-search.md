# Skill: web-search

## Purpose
FALLBACK ONLY. Uses Brave Search MCP to find theorem names, current mathlib documentation, package compatibility information, repository issues, and external proof examples from the web. Provides supplementary information when the local retrieval index is insufficient.

## When to Use
- **ONLY after lean-retrieve returns insufficient results** (fewer than 3 results with score > 0.5, or no results at all).
- When looking up current mathlib4 API documentation or recent changes.
- When diagnosing a package compatibility issue (e.g., toolchain version mismatches).
- When searching for known issues or workarounds in GitHub repos.
- **Do NOT use as the default first step.** Always try lean-retrieve first.

## Inputs
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | The search query. Be specific: include "lean4", "mathlib4", or theorem names for best results. |
| `max_results` | int | no | Maximum number of results. Default: 5. |
| `domain_filter` | string | no | Restrict to a specific domain (e.g., `"leanprover-community.github.io"`, `"github.com/leanprover"`). |

## Outputs
| Field | Type | Description |
|-------|------|-------------|
| `results` | list[object] | Search results, each containing: |
| `results[].title` | string | Page title. |
| `results[].url` | string | Page URL. |
| `results[].snippet` | string | Text snippet from the page. |
| `results[].domain` | string | Source domain. |

## Service Endpoint
- **URL:** Brave Search MCP (configured in the MCP server settings)
- **Service:** Brave Search via MCP protocol
- **Key tool:** `brave_web_search` MCP tool

## Example Usage
```
lean-retrieve returned no results for "Finset.sum_comm".

Use web-search as fallback:
  query: "mathlib4 Finset.sum_comm lean4 theorem statement"
  domain_filter: "leanprover-community.github.io"
```

## Notes
- **Ordering rule:** lean-retrieve FIRST, web-search SECOND. Never reverse this order.
- Web results may be outdated or refer to Lean 3 / mathlib (Lean 3). Always verify that results apply to Lean 4 / mathlib4.
- Prefer domain-filtered queries to reduce noise (e.g., filter to `leanprover-community.github.io` for mathlib docs).
- Do not use web-search for information that is already in the local corpus. The local index is faster and more reliable.
- Rate limits may apply to the Brave Search API. Use sparingly and batch queries when possible.
- If web-search also returns insufficient results, report the gap to the user rather than hallucinating theorem names.
