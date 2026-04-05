#!/usr/bin/env python3
"""Mathlib declaration extractor, embedder, and FAISS indexer.

Usage:
    python -m services.retrieval.indexer              # run all steps
    python -m services.retrieval.indexer --extract-only
    python -m services.retrieval.indexer --embed-only
    python -m services.retrieval.indexer --build-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import structlog

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent.parent
MATHLIB_DIR = ROOT / "lean" / ".lake" / "packages" / "mathlib" / "Mathlib"
DECLARATIONS_PATH = ROOT / "data" / "corpus" / "declarations.jsonl"
EMBEDDINGS_PATH = ROOT / "data" / "vectors" / "embeddings.npy"
METADATA_PATH = ROOT / "data" / "vectors" / "metadata.jsonl"
INDEX_PATH = ROOT / "data" / "vectors" / "index.faiss"

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
_DECL_RE = re.compile(
    r"^(theorem|lemma|def|instance|abbrev|noncomputable def)\s+(\S+)",
)

# Tokens that mark the end of a declaration statement (before the body)
_BODY_START = re.compile(r":=|:= by|\bwhere\b|\bby\b$|\bby\s")


def _module_from_path(filepath: Path) -> str:
    """Derive Lean module path from file path, e.g. Mathlib.Algebra.Group.Even."""
    try:
        rel = filepath.relative_to(MATHLIB_DIR.parent)
    except ValueError:
        return str(filepath)
    parts = rel.with_suffix("").parts  # ('Mathlib', 'Algebra', ...)
    return ".".join(parts)


def _extract_statement(lines: list[str], start_idx: int) -> str:
    """Extract the declaration statement starting at start_idx.

    Reads until we hit := | where | by, or a line that starts at column 0
    (not indented) after the first line. Caps at 500 chars.
    """
    stmt_parts = [lines[start_idx].rstrip()]

    for i in range(start_idx + 1, min(start_idx + 30, len(lines))):
        line = lines[i]
        # Stop if we hit an empty line or a line starting at column 0
        # (meaning a new top-level declaration)
        if not line.strip():
            break
        if line[0:1] not in (" ", "\t") and i > start_idx:
            break
        stripped = line.rstrip()
        # Check if this line contains a body-start token
        body_match = _BODY_START.search(stripped)
        if body_match:
            # Include text up to the body start token
            stmt_parts.append(stripped[: body_match.start()].rstrip())
            break
        stmt_parts.append(stripped)

    full = " ".join(part.strip() for part in stmt_parts if part.strip())
    # Trim body markers that might have slipped into the first line
    for marker in (":= by", ":=", " where", " by"):
        idx = full.find(marker)
        if idx > 0:
            full = full[:idx].rstrip()
            break

    return full[:500]


def extract_declarations() -> list[dict]:
    """Scan all .lean files under mathlib and extract declarations."""
    if not MATHLIB_DIR.exists():
        log.error("mathlib_not_found", path=str(MATHLIB_DIR))
        print(f"ERROR: Mathlib directory not found at {MATHLIB_DIR}")
        sys.exit(1)

    lean_files = sorted(MATHLIB_DIR.rglob("*.lean"))
    print(f"Found {len(lean_files)} .lean files under {MATHLIB_DIR}")

    declarations: list[dict] = []
    skipped = 0

    for file_idx, filepath in enumerate(lean_files):
        if file_idx % 500 == 0 and file_idx > 0:
            print(f"  Scanned {file_idx}/{len(lean_files)} files, {len(declarations)} declarations so far")

        try:
            text = filepath.read_text(errors="replace")
        except Exception:
            continue

        lines = text.splitlines()
        module = _module_from_path(filepath)

        for line_idx, line in enumerate(lines):
            m = _DECL_RE.match(line)
            if not m:
                continue
            name = m.group(2)

            # Skip private declarations
            if name.startswith("_root_") or "._" in name:
                skipped += 1
                continue

            statement = _extract_statement(lines, line_idx)

            declarations.append({
                "name": name,
                "statement": statement,
                "module": module,
            })

    print(f"Extracted {len(declarations)} declarations (skipped {skipped} private)")
    return declarations


def save_declarations(declarations: list[dict]) -> None:
    """Save declarations to JSONL file."""
    DECLARATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DECLARATIONS_PATH, "w") as f:
        for decl in declarations:
            f.write(json.dumps(decl, ensure_ascii=False) + "\n")
    print(f"Saved declarations to {DECLARATIONS_PATH}")


def load_declarations() -> list[dict]:
    """Load declarations from JSONL file."""
    if not DECLARATIONS_PATH.exists():
        print(f"ERROR: {DECLARATIONS_PATH} not found. Run --extract-only first.")
        sys.exit(1)
    declarations = []
    with open(DECLARATIONS_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                declarations.append(json.loads(line))
    print(f"Loaded {len(declarations)} declarations from {DECLARATIONS_PATH}")
    return declarations


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
def embed_declarations(declarations: list[dict]) -> np.ndarray:
    """Embed declarations using mindrouter API in batches."""
    api_key = os.environ.get("NEMOTRON_API_KEY", "")
    if not api_key:
        print("ERROR: NEMOTRON_API_KEY environment variable not set")
        sys.exit(1)

    api_base = os.environ.get("NEMOTRON_API_BASE", "https://mindrouter.uidaho.edu/v1")
    url = f"{api_base}/embeddings"

    batch_size = 64
    max_retries = 3
    all_embeddings: list[np.ndarray] = []

    # Prepare input strings
    inputs = [f"{d['name']}: {d['statement']}" for d in declarations]

    print(f"Embedding {len(inputs)} declarations in batches of {batch_size}")

    with httpx.Client(timeout=120.0) as client:
        for batch_start in range(0, len(inputs), batch_size):
            batch_end = min(batch_start + batch_size, len(inputs))
            batch = inputs[batch_start:batch_end]

            if batch_start % 1000 < batch_size:
                print(f"  Progress: {batch_start}/{len(inputs)} ({100 * batch_start / len(inputs):.1f}%)")

            # Retry loop
            for attempt in range(max_retries):
                try:
                    resp = client.post(
                        url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"model": "Qwen/Qwen3-Embedding-8B", "input": batch},
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    # Extract embeddings from response
                    batch_vecs = [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]
                    all_embeddings.extend([np.array(v, dtype=np.float32) for v in batch_vecs])
                    break  # success
                except (httpx.HTTPError, KeyError, Exception) as e:
                    if attempt < max_retries - 1:
                        wait = 2 ** (attempt + 1)
                        print(f"  Retry {attempt + 1}/{max_retries} after error: {e}. Waiting {wait}s...")
                        time.sleep(wait)
                    else:
                        print(f"  FAILED after {max_retries} retries at batch {batch_start}: {e}")
                        raise

            # Rate limit pause
            time.sleep(0.5)

    embeddings = np.stack(all_embeddings)
    print(f"Embeddings shape: {embeddings.shape}")
    return embeddings


def save_embeddings(embeddings: np.ndarray, declarations: list[dict]) -> None:
    """Save embeddings array and metadata."""
    EMBEDDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(EMBEDDINGS_PATH), embeddings)
    print(f"Saved embeddings to {EMBEDDINGS_PATH}")

    with open(METADATA_PATH, "w") as f:
        for decl in declarations:
            f.write(json.dumps({
                "name": decl["name"],
                "statement": decl["statement"],
                "module": decl["module"],
            }, ensure_ascii=False) + "\n")
    print(f"Saved metadata to {METADATA_PATH}")


# ---------------------------------------------------------------------------
# FAISS index build
# ---------------------------------------------------------------------------
def build_faiss_index() -> None:
    """Build FAISS IndexFlatIP from saved embeddings."""
    import faiss

    if not EMBEDDINGS_PATH.exists():
        print(f"ERROR: {EMBEDDINGS_PATH} not found. Run --embed-only first.")
        sys.exit(1)

    embeddings = np.load(str(EMBEDDINGS_PATH))
    print(f"Loaded embeddings: {embeddings.shape}")

    # Normalize for cosine similarity via inner product
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    embeddings = embeddings / norms

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings.astype(np.float32))

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(INDEX_PATH))
    print(f"FAISS index saved to {INDEX_PATH}")
    print(f"Total declarations indexed: {index.ntotal}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract mathlib declarations, embed them, and build FAISS index"
    )
    parser.add_argument("--extract-only", action="store_true", help="Only extract declarations from .lean files")
    parser.add_argument("--embed-only", action="store_true", help="Only embed declarations (requires declarations.jsonl)")
    parser.add_argument("--build-only", action="store_true", help="Only build FAISS index (requires embeddings.npy)")
    args = parser.parse_args()

    run_all = not (args.extract_only or args.embed_only or args.build_only)

    t0 = time.time()

    # Step 1: Extract
    if run_all or args.extract_only:
        print("=" * 60)
        print("STEP 1: Extracting declarations from mathlib")
        print("=" * 60)
        declarations = extract_declarations()
        save_declarations(declarations)
        if args.extract_only:
            print(f"\nDone in {time.time() - t0:.1f}s")
            return

    # Step 2: Embed
    if run_all or args.embed_only:
        print("=" * 60)
        print("STEP 2: Embedding declarations via mindrouter")
        print("=" * 60)
        declarations = load_declarations()
        embeddings = embed_declarations(declarations)
        save_embeddings(embeddings, declarations)
        if args.embed_only:
            print(f"\nDone in {time.time() - t0:.1f}s")
            return

    # Step 3: Build FAISS index
    if run_all or args.build_only:
        print("=" * 60)
        print("STEP 3: Building FAISS index")
        print("=" * 60)
        build_faiss_index()

    print(f"\nAll steps completed in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
