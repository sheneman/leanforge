#!/usr/bin/env python3
"""CLI script to build the retrieval index from lean/ files and data/corpus/.

Usage:
    python -m services.retrieval.indexer [--lean-dir lean/] [--corpus-dir data/corpus/] [--output data/corpus/index.faiss]

In production this will:
1. Scan .lean files for theorem/lemma declarations
2. Load any pre-existing corpus JSON/JSONL from data/corpus/
3. Embed all theorem statements with a sentence-transformer model
4. Build a FAISS index and write it to disk
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Lean file parser
# ---------------------------------------------------------------------------
_THEOREM_PATTERN = re.compile(
    r"^\s*(theorem|lemma|def|example)\s+(?P<name>\S+).*?:\s*(?P<stmt>.+)",
    re.MULTILINE,
)


def extract_theorems(lean_file: Path) -> list[dict]:
    """Extract theorem/lemma names and statements from a .lean file."""
    text = lean_file.read_text(errors="replace")
    results = []
    for m in _THEOREM_PATTERN.finditer(text):
        results.append(
            {
                "name": m.group("name"),
                "statement": m.group("stmt").strip().split(":=")[0].strip(),
                "module": str(lean_file),
                "source": "local",
            }
        )
    return results


def scan_lean_dir(lean_dir: Path) -> list[dict]:
    """Recursively scan a directory for .lean files and extract theorems."""
    all_theorems: list[dict] = []
    for lean_file in lean_dir.rglob("*.lean"):
        all_theorems.extend(extract_theorems(lean_file))
    return all_theorems


def load_corpus_dir(corpus_dir: Path) -> list[dict]:
    """Load theorem records from JSON/JSONL files in corpus_dir."""
    records: list[dict] = []
    if not corpus_dir.exists():
        return records
    for f in corpus_dir.iterdir():
        if f.suffix == ".json":
            data = json.loads(f.read_text())
            if isinstance(data, list):
                records.extend(data)
            else:
                records.append(data)
        elif f.suffix == ".jsonl":
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Index builder (stub)
# ---------------------------------------------------------------------------
def build_index(records: list[dict], output_path: Path) -> None:
    """Build a FAISS index from theorem records.

    Stub implementation -- writes a JSON manifest. In production:
    - Load sentence-transformers model
    - Embed all statements
    - Build FAISS IndexFlatIP or IndexIVFFlat
    - faiss.write_index(index, str(output_path))
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = output_path.with_suffix(".manifest.json")
    manifest = {
        "doc_count": len(records),
        "index_path": str(output_path),
        "note": "stub -- replace with real FAISS index",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    # Write records as JSONL for inspection
    records_path = output_path.with_suffix(".records.jsonl")
    with records_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"Indexed {len(records)} records")
    print(f"  Manifest: {manifest_path}")
    print(f"  Records:  {records_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Build retrieval index")
    parser.add_argument("--lean-dir", default="lean/", help="Path to Lean source directory")
    parser.add_argument("--corpus-dir", default="data/corpus/", help="Path to corpus data")
    parser.add_argument("--output", default="data/corpus/index.faiss", help="Output index path")
    args = parser.parse_args()

    lean_dir = Path(args.lean_dir)
    corpus_dir = Path(args.corpus_dir)
    output_path = Path(args.output)

    records: list[dict] = []

    if lean_dir.exists():
        print(f"Scanning {lean_dir} for .lean files...")
        lean_records = scan_lean_dir(lean_dir)
        print(f"  Found {len(lean_records)} declarations")
        records.extend(lean_records)

    corpus_records = load_corpus_dir(corpus_dir)
    if corpus_records:
        print(f"Loaded {len(corpus_records)} records from {corpus_dir}")
        records.extend(corpus_records)

    if not records:
        print("No records found. Nothing to index.")
        sys.exit(0)

    build_index(records, output_path)


if __name__ == "__main__":
    main()
