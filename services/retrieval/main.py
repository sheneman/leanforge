"""Retrieval service for forge-lean-prover.

Provides theorem search over a FAISS vector index of mathlib declarations
with a keyword-indexed fallback corpus covering common proof targets.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx
import numpy as np
import structlog
from dotenv import load_dotenv
from fastapi import FastAPI

from services.schemas import (
    RetrievalResult,
    TheoremMatch,
    TheoremSearchRequest,
)

load_dotenv()

log = structlog.get_logger()

INDEX_PATH = os.getenv("INDEX_PATH", "data/vectors/index.faiss")
METADATA_PATH = os.getenv("METADATA_PATH", "data/vectors/metadata.jsonl")
NEMOTRON_API_KEY = os.environ.get("NEMOTRON_API_KEY", "")
NEMOTRON_API_BASE = os.environ.get("NEMOTRON_API_BASE", "https://mindrouter.uidaho.edu/v1")

app = FastAPI(title="Retrieval Service", version="0.1.0")


# ---------------------------------------------------------------------------
# Built-in corpus — ~80 commonly needed mathlib theorems by topic
# ---------------------------------------------------------------------------

def _t(name: str, stmt: str, module: str, tags: str = "") -> TheoremMatch:
    """Helper to build a TheoremMatch with search tags embedded in source."""
    return TheoremMatch(name=name, statement=stmt, module=module, source=f"mathlib|{tags}")

_CORPUS: list[TheoremMatch] = [
    # ── Parity / Even / Odd ──────────────────────────────────────────────
    _t("Even.add", "theorem Even.add {a b : α} (ha : Even a) (hb : Even b) : Even (a + b)",
       "Mathlib.Algebra.Group.Even", "even add sum parity"),
    _t("Even.add_odd", "lemma Even.add_odd : Even a → Odd b → Odd (a + b)",
       "Mathlib.Algebra.Ring.Parity", "even odd add parity"),
    _t("Odd.add_even", "lemma Odd.add_even (ha : Odd a) (hb : Even b) : Odd (a + b)",
       "Mathlib.Algebra.Ring.Parity", "odd even add parity"),
    _t("Odd.add_odd", "lemma Odd.add_odd : Odd a → Odd b → Even (a + b)",
       "Mathlib.Algebra.Ring.Parity", "odd add even parity"),
    _t("even_iff_two_dvd", "lemma even_iff_two_dvd : Even a ↔ 2 ∣ a",
       "Mathlib.Algebra.Ring.Parity", "even dvd divisible two parity"),
    _t("even_iff_exists_two_mul", "lemma even_iff_exists_two_mul : Even a ↔ ∃ b, a = 2 * b",
       "Mathlib.Algebra.Ring.Parity", "even mul two exists parity"),
    _t("even_two", "lemma even_two : Even (2 : α)",
       "Mathlib.Algebra.Ring.Parity", "even two parity"),
    _t("even_two_mul", "lemma even_two_mul (a : α) : Even (2 * a)",
       "Mathlib.Algebra.Ring.Parity", "even two mul parity"),
    _t("Nat.even_add", "lemma even_add' : Even (m + n) ↔ (Odd m ↔ Odd n)",
       "Mathlib.Algebra.Ring.Parity", "even add nat parity"),
    _t("Nat.even_or_odd", "lemma even_or_odd (n : ℕ) : Even n ∨ Odd n",
       "Mathlib.Algebra.Ring.Parity", "even odd nat parity"),

    # ── Divisibility ─────────────────────────────────────────────────────
    _t("dvd_add", "theorem dvd_add {a b c : α} (h₁ : a ∣ b) (h₂ : a ∣ c) : a ∣ b + c",
       "Mathlib.Algebra.Divisibility.Basic", "dvd add divisible"),
    _t("dvd_mul_left", "theorem dvd_mul_left (a b : α) : a ∣ b * a",
       "Mathlib.Algebra.Divisibility.Basic", "dvd mul divisible"),
    _t("dvd_mul_right", "theorem dvd_mul_right (a b : α) : a ∣ a * b",
       "Mathlib.Algebra.Divisibility.Basic", "dvd mul divisible"),
    _t("dvd_refl", "theorem dvd_refl (a : α) : a ∣ a",
       "Mathlib.Algebra.Divisibility.Basic", "dvd refl divisible"),
    _t("dvd_trans", "theorem dvd_trans {a b c : α} (h₁ : a ∣ b) (h₂ : b ∣ c) : a ∣ c",
       "Mathlib.Algebra.Divisibility.Basic", "dvd trans divisible"),

    # ── Nat arithmetic ───────────────────────────────────────────────────
    _t("Nat.add_comm", "theorem Nat.add_comm (n m : Nat) : n + m = m + n",
       "Mathlib.Data.Nat.Basic", "nat add comm commutative"),
    _t("Nat.add_assoc", "theorem Nat.add_assoc (n m k : Nat) : n + m + k = n + (m + k)",
       "Mathlib.Data.Nat.Basic", "nat add assoc associative"),
    _t("Nat.mul_comm", "theorem Nat.mul_comm (n m : Nat) : n * m = m * n",
       "Mathlib.Data.Nat.Basic", "nat mul comm commutative"),
    _t("Nat.mul_assoc", "theorem Nat.mul_assoc (n m k : Nat) : n * m * k = n * (m * k)",
       "Mathlib.Data.Nat.Basic", "nat mul assoc associative"),
    _t("Nat.zero_add", "theorem Nat.zero_add (n : Nat) : 0 + n = n",
       "Mathlib.Data.Nat.Basic", "nat zero add identity"),
    _t("Nat.add_zero", "theorem Nat.add_zero (n : Nat) : n + 0 = n",
       "Mathlib.Data.Nat.Basic", "nat add zero identity"),
    _t("Nat.succ_ne_zero", "theorem Nat.succ_ne_zero (n : Nat) : n + 1 ≠ 0",
       "Mathlib.Data.Nat.Basic", "nat succ zero"),
    _t("Nat.sub_add_cancel", "theorem Nat.sub_add_cancel {n m : Nat} (h : m ≤ n) : n - m + m = n",
       "Mathlib.Data.Nat.Basic", "nat sub add cancel"),

    # ── Int arithmetic ───────────────────────────────────────────────────
    _t("Int.add_comm", "theorem Int.add_comm (a b : Int) : a + b = b + a",
       "Mathlib.Data.Int.Basic", "int add comm commutative"),
    _t("Int.mul_comm", "theorem Int.mul_comm (a b : Int) : a * b = b * a",
       "Mathlib.Data.Int.Basic", "int mul comm commutative"),

    # ── Primes ───────────────────────────────────────────────────────────
    _t("Nat.Prime", "def Nat.Prime (p : ℕ) : Prop := 2 ≤ p ∧ ∀ m, m ∣ p → m = 1 ∨ m = p",
       "Mathlib.Data.Nat.Prime.Defs", "prime nat definition"),
    _t("Nat.prime_two", "theorem Nat.prime_two : Nat.Prime 2",
       "Mathlib.Data.Nat.Prime.Basic", "prime two"),
    _t("Nat.exists_infinite_primes", "theorem Nat.exists_infinite_primes : ∀ n : ℕ, ∃ p, n ≤ p ∧ Nat.Prime p",
       "Mathlib.Data.Nat.Prime.Basic", "prime infinite euclid"),
    _t("Nat.Prime.one_lt", "theorem Nat.Prime.one_lt {p : ℕ} (hp : Nat.Prime p) : 1 < p",
       "Mathlib.Data.Nat.Prime.Basic", "prime greater one"),

    # ── GCD / Coprime ────────────────────────────────────────────────────
    _t("Nat.gcd_comm", "theorem Nat.gcd_comm (m n : ℕ) : Nat.gcd m n = Nat.gcd n m",
       "Mathlib.Data.Nat.GCD.Basic", "gcd comm commutative"),
    _t("Nat.Coprime", "def Nat.Coprime (m n : ℕ) : Prop := Nat.gcd m n = 1",
       "Mathlib.Data.Nat.GCD.Basic", "coprime gcd definition"),

    # ── Order / Inequality ───────────────────────────────────────────────
    _t("Nat.le_refl", "theorem Nat.le_refl (n : ℕ) : n ≤ n",
       "Mathlib.Data.Nat.Basic", "nat le refl order"),
    _t("Nat.lt_irrefl", "theorem Nat.lt_irrefl (n : ℕ) : ¬ n < n",
       "Mathlib.Data.Nat.Basic", "nat lt irrefl order"),
    _t("le_antisymm", "theorem le_antisymm {a b : α} (h₁ : a ≤ b) (h₂ : b ≤ a) : a = b",
       "Mathlib.Order.Basic", "le antisymm order"),
    _t("lt_trans", "theorem lt_trans {a b c : α} (h₁ : a < b) (h₂ : b < c) : a < c",
       "Mathlib.Order.Basic", "lt trans order"),
    _t("sq_nonneg", "theorem sq_nonneg (a : α) : 0 ≤ a ^ 2",
       "Mathlib.Algebra.Order.Ring.Lemmas", "square nonneg nonnegative"),
    _t("mul_self_nonneg", "theorem mul_self_nonneg (a : α) [LinearOrderedRing α] : 0 ≤ a * a",
       "Mathlib.Algebra.Order.Ring.Lemmas", "mul self nonneg nonnegative square"),
    _t("abs_nonneg", "theorem abs_nonneg (a : α) : 0 ≤ |a|",
       "Mathlib.Algebra.Order.AbsoluteValue", "abs nonneg absolute value"),

    # ── Logic / Propositional ────────────────────────────────────────────
    _t("And.intro", "theorem And.intro {a b : Prop} (ha : a) (hb : b) : a ∧ b",
       "Init.Prelude", "and intro conjunction"),
    _t("And.comm", "theorem And.comm : a ∧ b ↔ b ∧ a",
       "Mathlib.Logic.Basic", "and comm commutative"),
    _t("Or.comm", "theorem Or.comm : a ∨ b ↔ b ∨ a",
       "Mathlib.Logic.Basic", "or comm commutative disjunction"),
    _t("not_and_or", "theorem not_and_or : ¬(a ∧ b) ↔ ¬a ∨ ¬b",
       "Mathlib.Logic.Basic", "de morgan not and or"),
    _t("not_or", "theorem not_or : ¬(a ∨ b) ↔ ¬a ∧ ¬b",
       "Mathlib.Logic.Basic", "de morgan not or and"),
    _t("Classical.em", "theorem Classical.em (p : Prop) : p ∨ ¬p",
       "Init.Classical", "excluded middle classical logic"),
    _t("Iff.intro", "theorem Iff.intro (h₁ : a → b) (h₂ : b → a) : a ↔ b",
       "Init.Prelude", "iff intro biconditional"),

    # ── Sets ─────────────────────────────────────────────────────────────
    _t("Set.mem_union", "theorem Set.mem_union {a : α} {s t : Set α} : a ∈ s ∪ t ↔ a ∈ s ∨ a ∈ t",
       "Mathlib.Data.Set.Basic", "set union member"),
    _t("Set.mem_inter_iff", "theorem Set.mem_inter_iff {a : α} {s t : Set α} : a ∈ s ∩ t ↔ a ∈ s ∧ a ∈ t",
       "Mathlib.Data.Set.Basic", "set inter intersection member"),
    _t("Set.subset_def", "theorem Set.subset_def {s t : Set α} : s ⊆ t ↔ ∀ x, x ∈ s → x ∈ t",
       "Mathlib.Data.Set.Basic", "set subset definition"),
    _t("Set.countable_univ", "theorem Set.countable_univ [Countable α] : Set.Countable (Set.univ : Set α)",
       "Mathlib.Data.Set.Countable", "set countable univ"),

    # ── Topology ─────────────────────────────────────────────────────────
    _t("isOpen_empty", "theorem isOpen_empty {X : Type*} [TopologicalSpace X] : IsOpen (∅ : Set X)",
       "Mathlib.Topology.Basic", "open empty topology"),
    _t("isOpen_univ", "theorem isOpen_univ {X : Type*} [TopologicalSpace X] : IsOpen (Set.univ : Set X)",
       "Mathlib.Topology.Basic", "open univ topology"),
    _t("isCompact_Icc", "theorem isCompact_Icc : IsCompact (Set.Icc a b)",
       "Mathlib.Topology.Compactness.Compact", "compact interval icc topology"),
    _t("Continuous.comp", "theorem Continuous.comp {f : β → γ} {g : α → β} (hf : Continuous f) (hg : Continuous g) : Continuous (f ∘ g)",
       "Mathlib.Topology.Basic", "continuous comp composition topology"),

    # ── Algebra / Groups / Rings ─────────────────────────────────────────
    _t("add_comm", "theorem add_comm [AddCommMonoid α] (a b : α) : a + b = b + a",
       "Mathlib.Algebra.Group.Basic", "add comm commutative"),
    _t("add_assoc", "theorem add_assoc [AddMonoid α] (a b c : α) : a + b + c = a + (b + c)",
       "Mathlib.Algebra.Group.Basic", "add assoc associative"),
    _t("mul_comm", "theorem mul_comm [CommMonoid α] (a b : α) : a * b = b * a",
       "Mathlib.Algebra.Group.Basic", "mul comm commutative"),
    _t("mul_assoc", "theorem mul_assoc [Monoid α] (a b c : α) : a * b * c = a * (b * c)",
       "Mathlib.Algebra.Group.Basic", "mul assoc associative"),
    _t("add_zero", "theorem add_zero [AddMonoid α] (a : α) : a + 0 = a",
       "Mathlib.Algebra.Group.Basic", "add zero identity"),
    _t("zero_add", "theorem zero_add [AddMonoid α] (a : α) : 0 + a = a",
       "Mathlib.Algebra.Group.Basic", "zero add identity"),
    _t("mul_one", "theorem mul_one [Monoid α] (a : α) : a * 1 = a",
       "Mathlib.Algebra.Group.Basic", "mul one identity"),
    _t("one_mul", "theorem one_mul [Monoid α] (a : α) : 1 * a = a",
       "Mathlib.Algebra.Group.Basic", "one mul identity"),
    _t("neg_neg", "theorem neg_neg [AddGroup α] (a : α) : - -a = a",
       "Mathlib.Algebra.Group.Basic", "neg double negation"),
    _t("add_left_cancel", "theorem add_left_cancel [AddLeftCancelMonoid α] {a b c : α} (h : a + b = a + c) : b = c",
       "Mathlib.Algebra.Group.Basic", "add left cancel"),
    _t("mul_left_cancel", "theorem mul_left_cancel [LeftCancelMonoid α] {a b c : α} (h : a * b = a * c) : b = c",
       "Mathlib.Algebra.Group.Basic", "mul left cancel"),

    # ── Ring / Field ─────────────────────────────────────────────────────
    _t("mul_add", "theorem mul_add [Distrib α] (a b c : α) : a * (b + c) = a * b + a * c",
       "Mathlib.Algebra.Ring.Defs", "mul add distributive ring"),
    _t("add_mul", "theorem add_mul [Distrib α] (a b c : α) : (a + b) * c = a * c + b * c",
       "Mathlib.Algebra.Ring.Defs", "add mul distributive ring"),
    _t("two_mul", "theorem two_mul (a : α) : 2 * a = a + a",
       "Mathlib.Algebra.Ring.Defs", "two mul double"),
    _t("mul_two", "theorem mul_two (a : α) : a * 2 = a + a",
       "Mathlib.Algebra.Ring.Defs", "mul two double"),

    # ── List ─────────────────────────────────────────────────────────────
    _t("List.length_append", "theorem List.length_append (l1 l2 : List α) : (l1 ++ l2).length = l1.length + l2.length",
       "Mathlib.Data.List.Basic", "list length append"),
    _t("List.reverse_reverse", "theorem List.reverse_reverse (l : List α) : l.reverse.reverse = l",
       "Mathlib.Data.List.Basic", "list reverse involution"),
    _t("List.length_reverse", "theorem List.length_reverse (l : List α) : l.reverse.length = l.length",
       "Mathlib.Data.List.Basic", "list length reverse"),

    # ── Finset / Combinatorics ───────────────────────────────────────────
    _t("Finset.sum_range_succ", "theorem Finset.sum_range_succ (f : ℕ → α) (n : ℕ) : (Finset.range (n+1)).sum f = (Finset.range n).sum f + f n",
       "Mathlib.Algebra.BigOperators.Group.Finset.Basic", "finset sum range succ induction"),
    _t("Finset.card_union_add_card_inter", "theorem Finset.card_union_add_card_inter (s t : Finset α) : (s ∪ t).card + (s ∩ t).card = s.card + t.card",
       "Mathlib.Data.Finset.Basic", "finset card union inter inclusion exclusion"),

    # ── Power / Exponent ─────────────────────────────────────────────────
    _t("pow_succ", "theorem pow_succ (a : α) (n : ℕ) : a ^ (n + 1) = a ^ n * a",
       "Mathlib.Algebra.GroupPower.Basic", "pow succ power"),
    _t("pow_zero", "theorem pow_zero (a : α) : a ^ 0 = 1",
       "Mathlib.Algebra.GroupPower.Basic", "pow zero power"),
    _t("one_pow", "theorem one_pow (n : ℕ) : (1 : α) ^ n = 1",
       "Mathlib.Algebra.GroupPower.Basic", "one pow power"),
    _t("sq", "theorem sq (a : α) : a ^ 2 = a * a",
       "Mathlib.Algebra.GroupPower.Basic", "sq square power"),

    # ── Tactic hints (pseudo-theorems for tactic discovery) ──────────────
    _t("tactic.norm_num", "tactic norm_num : closes numeric goals like 1 + 1 = 2, 2 < 5, etc.",
       "Mathlib.Tactic", "norm_num numeric compute arithmetic tactic"),
    _t("tactic.omega", "tactic omega : decides linear arithmetic over ℕ and ℤ",
       "Mathlib.Tactic", "omega linear arithmetic nat int tactic"),
    _t("tactic.ring", "tactic ring : proves equalities in commutative (semi)rings",
       "Mathlib.Tactic", "ring equality commutative tactic"),
    _t("tactic.simp", "tactic simp : simplification using simp lemmas",
       "Mathlib.Tactic", "simp simplify simplification tactic"),
    _t("tactic.linarith", "tactic linarith : proves linear arithmetic goals from hypotheses",
       "Mathlib.Tactic", "linarith linear arithmetic inequality tactic"),
    _t("tactic.exact?", "tactic exact? : searches the library for a term that closes the goal",
       "Mathlib.Tactic", "exact? search library lemma tactic"),
    _t("tactic.apply?", "tactic apply? : searches the library for a lemma whose conclusion matches the goal",
       "Mathlib.Tactic", "apply? search library lemma tactic"),
    _t("tactic.aesop", "tactic aesop : automated reasoning combining multiple strategies",
       "Mathlib.Tactic", "aesop automated reasoning tactic"),
    _t("tactic.decide", "tactic decide : decides decidable propositions by computation",
       "Mathlib.Tactic", "decide decidable computation tactic"),
    _t("tactic.rcases", "tactic rcases : recursive case split on inductive hypotheses",
       "Mathlib.Tactic", "rcases cases split inductive existential tactic"),
]


# ---------------------------------------------------------------------------
# Search engine
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    """Lowercase and split into tokens, keeping alphanumeric + dots + underscores."""
    return set(re.findall(r"[a-z0-9_.]+", text.lower()))


def _score(query_tokens: set[str], doc_tokens: set[str]) -> float:
    """Jaccard-like overlap score between query and document tokens."""
    if not query_tokens:
        return 0.0
    intersection = query_tokens & doc_tokens
    # Weight: each matching token contributes, bonus for exact name match
    return len(intersection) / max(len(query_tokens), 1)


class VectorIndex:
    """Theorem retrieval using FAISS vector search with keyword fallback."""

    def __init__(self, index_path: str = INDEX_PATH, metadata_path: str = METADATA_PATH) -> None:
        self.index_path = index_path
        self.metadata_path = metadata_path
        self._use_faiss = False
        self._faiss_index = None
        self._faiss_metadata: list[dict] = []
        self._http_client: httpx.Client | None = None

        # Keyword fallback corpus
        self._corpus: list[TheoremMatch] = list(_CORPUS)
        self._doc_tokens: list[set[str]] = []
        for thm in self._corpus:
            text = f"{thm.name} {thm.statement} {thm.source} {thm.module}"
            self._doc_tokens.append(_tokenize(text))

        # Try to load FAISS index
        self._try_load_faiss()

    def _try_load_faiss(self) -> None:
        """Attempt to load FAISS index and metadata from disk."""
        index_p = Path(self.index_path)
        meta_p = Path(self.metadata_path)

        if not index_p.exists() or not meta_p.exists():
            log.info("faiss_not_available", index_exists=index_p.exists(), meta_exists=meta_p.exists())
            return

        try:
            import faiss
            self._faiss_index = faiss.read_index(str(index_p))
            self._faiss_metadata = []
            with open(meta_p) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._faiss_metadata.append(json.loads(line))
            self._use_faiss = True
            log.info("faiss_loaded", vectors=self._faiss_index.ntotal, metadata=len(self._faiss_metadata))
        except Exception as e:
            log.error("faiss_load_failed", error=str(e))
            self._use_faiss = False

    def _get_http_client(self) -> httpx.Client:
        """Return a cached httpx client for embedding queries."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.Client(timeout=10.0)
        return self._http_client

    def _embed_query(self, query: str) -> np.ndarray | None:
        """Embed a single query string via mindrouter."""
        if not NEMOTRON_API_KEY:
            log.warning("no_api_key_for_embedding")
            return None

        url = f"{NEMOTRON_API_BASE}/embeddings"
        try:
            client = self._get_http_client()
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {NEMOTRON_API_KEY}"},
                json={"model": "Qwen/Qwen3-Embedding-8B", "input": [query]},
            )
            resp.raise_for_status()
            vec = np.array(resp.json()["data"][0]["embedding"], dtype=np.float32)
            # Normalize for cosine similarity
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            return vec
        except Exception as e:
            log.error("query_embedding_failed", error=str(e))
            return None

    def search(self, query: str, top_k: int = 10, filters: dict | None = None) -> list[TheoremMatch]:
        """Search for theorems matching the query."""
        if self._use_faiss:
            return self._search_faiss(query, top_k)
        return self._search_keyword(query, top_k)

    def _search_faiss(self, query: str, top_k: int) -> list[TheoremMatch]:
        """Search using FAISS vector index."""
        vec = self._embed_query(query)
        if vec is None:
            log.warning("faiss_fallback_to_keyword", reason="embedding_failed")
            return self._search_keyword(query, top_k)

        vec_2d = vec.reshape(1, -1)
        scores, indices = self._faiss_index.search(vec_2d, top_k)

        results: list[TheoremMatch] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._faiss_metadata):
                continue
            meta = self._faiss_metadata[idx]
            results.append(TheoremMatch(
                name=meta["name"],
                statement=meta["statement"],
                module=meta.get("module", ""),
                score=round(float(score), 4),
                source="mathlib",
            ))
        return results

    def _search_keyword(self, query: str, top_k: int) -> list[TheoremMatch]:
        """Fallback keyword search over built-in corpus."""
        query_tokens = _tokenize(query)

        scored: list[tuple[float, int]] = []
        for i, doc_tok in enumerate(self._doc_tokens):
            s = _score(query_tokens, doc_tok)
            name_lower = self._corpus[i].name.lower()
            for qt in query_tokens:
                if qt in name_lower:
                    s += 0.3
                    break
            scored.append((s, i))

        scored.sort(key=lambda x: x[0], reverse=True)

        results: list[TheoremMatch] = []
        for s, i in scored[:top_k]:
            if s <= 0.0:
                continue
            thm = self._corpus[i].model_copy()
            thm.source = thm.source.split("|")[0]
            thm.score = round(s, 4)
            results.append(thm)
        return results

    def reindex(self) -> dict:
        """Trigger re-indexing and reload FAISS if available."""
        log.info("reindex_triggered")
        self._try_load_faiss()
        return {"status": "complete", "doc_count": self._faiss_index.ntotal if self._use_faiss else len(self._corpus)}

    def stats(self) -> dict:
        return {
            "use_faiss": self._use_faiss,
            "faiss_vectors": self._faiss_index.ntotal if self._use_faiss and self._faiss_index else 0,
            "keyword_corpus_size": len(self._corpus),
            "index_path": self.index_path,
        }


_index = VectorIndex()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "retrieval"}


@app.post("/search")
async def search(req: TheoremSearchRequest):
    t0 = time.monotonic()
    matches = _index.search(req.query, req.top_k, req.filters)
    elapsed = time.monotonic() - t0
    log.info("search", query=req.query, results=len(matches), elapsed=elapsed)
    return RetrievalResult(
        query=req.query,
        results=matches,
        source="local",
    ).model_dump()


@app.post("/index")
async def reindex():
    result = _index.reindex()
    return result


@app.get("/stats")
async def stats():
    return _index.stats()
