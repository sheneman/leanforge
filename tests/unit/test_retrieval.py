"""Unit tests for the retrieval service VectorIndex."""
from __future__ import annotations

import pytest

from services.retrieval.main import VectorIndex, _FALLBACK_CORPUS


@pytest.fixture
def index() -> VectorIndex:
    return VectorIndex()


class TestVectorIndexSearch:
    def test_search_returns_results(self, index: VectorIndex):
        results = index.search("Nat add comm")
        assert len(results) > 0
        # The top result should be related to Nat.add_comm
        names = [r.name for r in results]
        assert "Nat.add_comm" in names

    def test_search_scores_are_set(self, index: VectorIndex):
        results = index.search("Nat add comm")
        top = results[0]
        assert top.score > 0.0

    def test_search_mul(self, index: VectorIndex):
        results = index.search("mul comm")
        names = [r.name for r in results]
        assert "Nat.mul_comm" in names

    def test_empty_query(self, index: VectorIndex):
        results = index.search("")
        # Should still return results (all score 0)
        assert len(results) > 0

    def test_empty_query_scores_zero(self, index: VectorIndex):
        results = index.search("")
        assert all(r.score == 0.0 for r in results)

    def test_top_k_limiting(self, index: VectorIndex):
        results_3 = index.search("Nat", top_k=3)
        assert len(results_3) <= 3

        results_1 = index.search("Nat", top_k=1)
        assert len(results_1) == 1

    def test_top_k_larger_than_corpus(self, index: VectorIndex):
        results = index.search("Nat", top_k=1000)
        assert len(results) == len(_FALLBACK_CORPUS)

    def test_results_sorted_by_score(self, index: VectorIndex):
        results = index.search("Nat add")
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_result_fields_populated(self, index: VectorIndex):
        results = index.search("Nat.add_comm")
        for r in results:
            assert r.name != ""
            assert r.statement != ""
            assert r.source == "mathlib"
