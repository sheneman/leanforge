"""Shared pytest fixtures for forge-lean-prover tests."""
from __future__ import annotations

import pytest

from services.schemas import ProofTask


@pytest.fixture
def sample_proof_task() -> ProofTask:
    """Return a minimal ProofTask for testing."""
    return ProofTask(
        theorem_statement="theorem add_zero (n : Nat) : n + 0 = n",
        context="",
        imports=["Mathlib.Data.Nat.Basic"],
        max_branches=10,
        timeout_secs=30,
    )


@pytest.fixture
def sample_theorem_statement() -> str:
    """Return a simple theorem statement string."""
    return "theorem add_zero (n : Nat) : n + 0 = n"
