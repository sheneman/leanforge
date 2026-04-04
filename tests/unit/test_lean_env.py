"""Unit tests for the lean_env service."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.lean_env.main import (
    LeanRunner,
    classify_diagnostic,
    diagnostics_to_status,
)
from services.schemas import CompileCheckResult, DiagnosticItem, ProofStatus


# ---------------------------------------------------------------------------
# classify_diagnostic
# ---------------------------------------------------------------------------

class TestClassifyDiagnostic:
    def test_unknown_identifier(self):
        assert classify_diagnostic("unknown identifier 'Nat.foo'") == "unknown_identifier"

    def test_unknown_constant(self):
        assert classify_diagnostic("unknown constant 'bar'") == "unknown_identifier"

    def test_type_mismatch(self):
        assert classify_diagnostic("type mismatch\n  expected Nat\n  got Int") == "type_mismatch"

    def test_application_type_mismatch(self):
        assert classify_diagnostic("application type mismatch") == "type_mismatch"

    def test_elaboration_error(self):
        assert classify_diagnostic("elaboration error at ...") == "elaboration_error"

    def test_failed_to_synthesize(self):
        assert classify_diagnostic("failed to synthesize instance") == "elaboration_error"

    def test_sorry(self):
        assert classify_diagnostic("declaration uses 'sorry'") == "elaboration_error"

    def test_timeout(self):
        assert classify_diagnostic("deterministic timeout") == "timeout"

    def test_maximum_recursion(self):
        assert classify_diagnostic("maximum recursion depth has been reached") == "timeout"

    def test_plain_timeout(self):
        assert classify_diagnostic("timeout") == "timeout"

    def test_unrecognised_message(self):
        assert classify_diagnostic("something completely different") == ""

    def test_empty_message(self):
        assert classify_diagnostic("") == ""

    def test_case_insensitive(self):
        assert classify_diagnostic("Unknown Identifier 'X'") == "unknown_identifier"
        assert classify_diagnostic("TYPE MISMATCH") == "type_mismatch"


# ---------------------------------------------------------------------------
# diagnostics_to_status
# ---------------------------------------------------------------------------

class TestDiagnosticsToStatus:
    def test_no_diagnostics_means_verified(self):
        assert diagnostics_to_status([]) == ProofStatus.VERIFIED

    def test_timeout_takes_precedence(self):
        diags = [
            DiagnosticItem(severity="error", message="timeout", category="timeout"),
            DiagnosticItem(severity="error", message="unknown", category="unknown_identifier"),
        ]
        assert diagnostics_to_status(diags) == ProofStatus.TIMEOUT

    def test_unknown_identifier(self):
        diags = [
            DiagnosticItem(severity="error", message="unknown identifier", category="unknown_identifier"),
        ]
        assert diagnostics_to_status(diags) == ProofStatus.UNKNOWN_IDENTIFIER

    def test_type_mismatch(self):
        diags = [
            DiagnosticItem(severity="error", message="type mismatch", category="type_mismatch"),
        ]
        assert diagnostics_to_status(diags) == ProofStatus.TYPE_MISMATCH

    def test_elaboration_error(self):
        diags = [
            DiagnosticItem(severity="error", message="failed", category="elaboration_error"),
        ]
        assert diagnostics_to_status(diags) == ProofStatus.ELABORATION_ERROR

    def test_generic_error(self):
        diags = [
            DiagnosticItem(severity="error", message="something else", category=""),
        ]
        assert diagnostics_to_status(diags) == ProofStatus.FAILED

    def test_warnings_only_means_verified(self):
        diags = [
            DiagnosticItem(severity="warning", message="unused var", category=""),
        ]
        assert diagnostics_to_status(diags) == ProofStatus.VERIFIED


# ---------------------------------------------------------------------------
# LeanRunner.compile_source (mocked subprocess)
# ---------------------------------------------------------------------------

class TestLeanRunnerCompile:
    @pytest.mark.asyncio
    async def test_compile_success(self, tmp_path):
        """Successful compilation returns success=True with no error diagnostics."""
        runner = LeanRunner(lean_bin="lean", lake_bin="lake", project_dir="", timeout=30)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch("services.lean_env.main.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.compile_source("theorem foo : True := trivial")

        assert isinstance(result, CompileCheckResult)
        assert result.success is True
        assert result.elapsed_secs > 0

    @pytest.mark.asyncio
    async def test_compile_failure_with_error(self, tmp_path):
        """Compilation that emits a Lean error diagnostic returns success=False."""
        runner = LeanRunner(lean_bin="lean", lake_bin="lake", project_dir="", timeout=30)

        stderr = b"test.lean:1:0: error: unknown identifier 'bad'\n"
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", stderr))
        mock_proc.returncode = 1

        with patch("services.lean_env.main.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.compile_source("bad")

        assert result.success is False
        assert len(result.diagnostics) >= 1
        assert result.diagnostics[0].severity == "error"
        assert result.diagnostics[0].category == "unknown_identifier"

    @pytest.mark.asyncio
    async def test_compile_timeout(self):
        """When subprocess times out, result contains a timeout diagnostic."""
        runner = LeanRunner(lean_bin="lean", lake_bin="lake", project_dir="", timeout=1)

        async def slow_communicate():
            await asyncio.sleep(10)
            return (b"", b"")

        mock_proc = AsyncMock()
        mock_proc.communicate = slow_communicate

        with patch("services.lean_env.main.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.compile_source("-- slow")

        assert result.success is False
        assert any(d.category == "timeout" for d in result.diagnostics)

    @pytest.mark.asyncio
    async def test_compile_binary_not_found(self):
        """When lean binary is not found, result contains an appropriate error."""
        runner = LeanRunner(
            lean_bin="/nonexistent/lean",
            lake_bin="/nonexistent/lake",
            project_dir="",
            timeout=10,
        )

        with patch(
            "services.lean_env.main.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("lean not found"),
        ):
            result = await runner.compile_source("-- test")

        assert result.success is False
        assert len(result.diagnostics) >= 1
        assert "not found" in result.diagnostics[0].message
