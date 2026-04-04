"""Unit tests for Pydantic schemas."""
from __future__ import annotations

import uuid

import pytest

from services.schemas import (
    BranchState,
    CompileCheckResult,
    DiagnosticItem,
    ProofStatus,
    ProofTask,
    VerificationResult,
)


# ---- ProofTask ----

class TestProofTask:
    def test_creation_with_defaults(self):
        task = ProofTask(theorem_statement="theorem foo : True")
        assert task.theorem_statement == "theorem foo : True"
        assert task.context == ""
        assert task.imports == []
        assert task.max_branches == 50
        assert task.timeout_secs == 120
        assert task.metadata == {}
        # task_id should be a valid UUID
        uuid.UUID(task.task_id)

    def test_creation_with_all_fields(self):
        task = ProofTask(
            task_id="custom-id",
            theorem_statement="theorem bar : 1 = 1",
            context="open Nat",
            imports=["Mathlib.Tactic.Ring"],
            max_branches=20,
            timeout_secs=60,
            metadata={"source": "benchmark"},
        )
        assert task.task_id == "custom-id"
        assert task.imports == ["Mathlib.Tactic.Ring"]
        assert task.max_branches == 20

    def test_unique_task_ids(self):
        t1 = ProofTask(theorem_statement="theorem a : True")
        t2 = ProofTask(theorem_statement="theorem b : True")
        assert t1.task_id != t2.task_id


# ---- ProofStatus ----

class TestProofStatus:
    def test_enum_values(self):
        assert ProofStatus.VERIFIED == "verified"
        assert ProofStatus.PARTIAL == "partial"
        assert ProofStatus.FAILED == "failed"
        assert ProofStatus.ELABORATION_ERROR == "elaboration_error"
        assert ProofStatus.TIMEOUT == "timeout"
        assert ProofStatus.UNKNOWN_IDENTIFIER == "unknown_identifier"
        assert ProofStatus.TYPE_MISMATCH == "type_mismatch"
        assert ProofStatus.PENDING == "pending"

    def test_all_members_present(self):
        expected = {
            "VERIFIED", "PARTIAL", "FAILED", "ELABORATION_ERROR",
            "TIMEOUT", "UNKNOWN_IDENTIFIER", "TYPE_MISMATCH", "PENDING",
        }
        assert set(ProofStatus.__members__.keys()) == expected

    def test_string_comparison(self):
        assert ProofStatus.VERIFIED == "verified"
        assert str(ProofStatus.FAILED) == "ProofStatus.FAILED"


# ---- VerificationResult ----

class TestVerificationResult:
    def test_serialization_roundtrip(self):
        result = VerificationResult(
            task_id="abc-123",
            status=ProofStatus.VERIFIED,
            proof_text="exact Nat.add_zero n",
            diagnostics=[],
            lean_output="",
            elapsed_secs=1.5,
        )
        data = result.model_dump()
        assert data["task_id"] == "abc-123"
        assert data["status"] == "verified"
        assert data["proof_text"] == "exact Nat.add_zero n"
        assert data["elapsed_secs"] == 1.5
        assert data["verified_at"] is None

        restored = VerificationResult.model_validate(data)
        assert restored.task_id == result.task_id
        assert restored.status == result.status

    def test_defaults(self):
        result = VerificationResult(task_id="x", status=ProofStatus.PENDING)
        assert result.proof_text == ""
        assert result.diagnostics == []
        assert result.lean_output == ""
        assert result.elapsed_secs == 0.0
        assert result.verified_at is None


# ---- BranchState ----

class TestBranchState:
    def test_default_values(self):
        branch = BranchState(task_id="task-1")
        uuid.UUID(branch.branch_id)  # valid UUID
        assert branch.task_id == "task-1"
        assert branch.parent_id is None
        assert branch.tactics == []
        assert branch.goals_before == []
        assert branch.goals_after == []
        assert branch.status == ProofStatus.PENDING
        assert branch.score == 0.0
        assert branch.depth == 0

    def test_with_tactics(self):
        branch = BranchState(
            task_id="task-1",
            tactics=["simp", "ring"],
            depth=2,
            score=0.8,
            status=ProofStatus.VERIFIED,
        )
        assert branch.tactics == ["simp", "ring"]
        assert branch.depth == 2
        assert branch.score == 0.8


# ---- CompileCheckResult ----

class TestCompileCheckResult:
    def test_with_diagnostics(self):
        diag = DiagnosticItem(
            severity="error",
            message="unknown identifier 'foo'",
            line=10,
            column=4,
            category="unknown_identifier",
        )
        result = CompileCheckResult(
            source="theorem foo : True := by sorry",
            success=False,
            diagnostics=[diag],
            elapsed_secs=0.3,
        )
        assert not result.success
        assert len(result.diagnostics) == 1
        assert result.diagnostics[0].category == "unknown_identifier"

    def test_success_no_diagnostics(self):
        result = CompileCheckResult(source="-- ok", success=True)
        assert result.success
        assert result.diagnostics == []
        assert result.elapsed_secs == 0.0


# ---- DiagnosticItem ----

class TestDiagnosticItem:
    def test_categories(self):
        categories = [
            "unknown_identifier",
            "type_mismatch",
            "elaboration_error",
            "timeout",
            "",
        ]
        for cat in categories:
            item = DiagnosticItem(severity="error", message="msg", category=cat)
            assert item.category == cat

    def test_optional_fields(self):
        item = DiagnosticItem(severity="warning", message="unused variable")
        assert item.line is None
        assert item.column is None
        assert item.category == ""

    def test_all_severities(self):
        for sev in ("error", "warning", "info"):
            item = DiagnosticItem(severity=sev, message="test")
            assert item.severity == sev
