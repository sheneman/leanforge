"""Lean environment service for forge-lean-prover.

Provides compilation, diagnostic classification, and Pantograph session management.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.schemas import (
    CompileCheckResult,
    DiagnosticItem,
    ProofStatus,
)

load_dotenv()

log = structlog.get_logger()

LEAN_BIN = os.getenv("LEAN_BIN", "lean")
LAKE_BIN = os.getenv("LAKE_BIN", "lake")
LEAN_PROJECT_DIR = os.getenv("LEAN_PROJECT_DIR", "")
COMPILE_TIMEOUT = int(os.getenv("COMPILE_TIMEOUT", "60"))

app = FastAPI(title="Lean Environment Service", version="0.1.0")


# ---------------------------------------------------------------------------
# Request models (endpoint-specific)
# ---------------------------------------------------------------------------
class CompileRequest(BaseModel):
    source: str


class CompileFileRequest(BaseModel):
    path: str


class TacticRequest(BaseModel):
    tactic: str
    goal_index: int = 0


# ---------------------------------------------------------------------------
# Diagnostic classifier
# ---------------------------------------------------------------------------
_DIAGNOSTIC_PATTERNS: list[tuple[str, str]] = [
    (r"unknown identifier", "unknown_identifier"),
    (r"unknown constant", "unknown_identifier"),
    (r"type mismatch", "type_mismatch"),
    (r"application type mismatch", "type_mismatch"),
    (r"elaboration error", "elaboration_error"),
    (r"failed to synthesize", "elaboration_error"),
    (r"(deterministic)?\s*timeout", "timeout"),
    (r"maximum recursion depth", "timeout"),
    (r"declaration uses 'sorry'", "elaboration_error"),
]


def classify_diagnostic(message: str) -> str:
    """Classify a Lean diagnostic message into a category."""
    lower = message.lower()
    for pattern, category in _DIAGNOSTIC_PATTERNS:
        if re.search(pattern, lower):
            return category
    return ""


def diagnostics_to_status(diagnostics: list[DiagnosticItem]) -> ProofStatus:
    """Derive a ProofStatus from a list of diagnostics."""
    if not diagnostics:
        return ProofStatus.VERIFIED

    categories = {d.category for d in diagnostics if d.severity == "error"}
    if "timeout" in categories:
        return ProofStatus.TIMEOUT
    if "unknown_identifier" in categories:
        return ProofStatus.UNKNOWN_IDENTIFIER
    if "type_mismatch" in categories:
        return ProofStatus.TYPE_MISMATCH
    if "elaboration_error" in categories:
        return ProofStatus.ELABORATION_ERROR
    if any(d.severity == "error" for d in diagnostics):
        return ProofStatus.FAILED
    return ProofStatus.VERIFIED


# ---------------------------------------------------------------------------
# LeanRunner
# ---------------------------------------------------------------------------
class LeanRunner:
    """Compile Lean source by shelling out to ``lake env lean`` or ``lean``."""

    def __init__(
        self,
        lean_bin: str = LEAN_BIN,
        lake_bin: str = LAKE_BIN,
        project_dir: str = LEAN_PROJECT_DIR,
        timeout: int = COMPILE_TIMEOUT,
    ) -> None:
        self.lean_bin = lean_bin
        self.lake_bin = lake_bin
        self.project_dir = project_dir
        self.timeout = timeout

    async def compile_source(self, source: str) -> CompileCheckResult:
        """Write source to a temp file and compile it."""
        t0 = time.monotonic()
        tmp = tempfile.NamedTemporaryFile(suffix=".lean", delete=False, mode="w")
        try:
            tmp.write(source)
            tmp.close()
            result = await self._run_lean(tmp.name, source)
            result.elapsed_secs = time.monotonic() - t0
            return result
        finally:
            os.unlink(tmp.name)

    async def compile_file(self, path: str) -> CompileCheckResult:
        """Compile an existing .lean file."""
        if not Path(path).exists():
            return CompileCheckResult(
                source=path,
                success=False,
                diagnostics=[
                    DiagnosticItem(severity="error", message=f"file not found: {path}")
                ],
            )
        t0 = time.monotonic()
        source_text = Path(path).read_text()
        result = await self._run_lean(path, source_text)
        result.elapsed_secs = time.monotonic() - t0
        return result

    async def _run_lean(self, filepath: str, source_text: str) -> CompileCheckResult:
        """Run lean on a file path and parse output."""
        cmd: list[str]
        if self.project_dir and shutil.which(self.lake_bin):
            cmd = [self.lake_bin, "env", self.lean_bin, filepath]
        else:
            cmd = [self.lean_bin, filepath]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_dir or None,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            return CompileCheckResult(
                source=source_text,
                success=False,
                diagnostics=[
                    DiagnosticItem(
                        severity="error",
                        message=f"compilation timed out after {self.timeout}s",
                        category="timeout",
                    )
                ],
            )
        except FileNotFoundError:
            return CompileCheckResult(
                source=source_text,
                success=False,
                diagnostics=[
                    DiagnosticItem(
                        severity="error",
                        message=f"lean binary not found: {cmd[0]}",
                    )
                ],
            )

        output = (stdout or b"").decode() + (stderr or b"").decode()
        diagnostics = self._parse_diagnostics(output)
        success = proc.returncode == 0 and not any(
            d.severity == "error" for d in diagnostics
        )
        return CompileCheckResult(
            source=source_text,
            success=success,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _parse_diagnostics(output: str) -> list[DiagnosticItem]:
        """Parse Lean compiler output into DiagnosticItem list."""
        items: list[DiagnosticItem] = []
        # Lean outputs lines like: file.lean:10:4: error: message
        pattern = re.compile(
            r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<sev>error|warning|info):\s*(?P<msg>.+)",
            re.MULTILINE,
        )
        for m in pattern.finditer(output):
            msg = m.group("msg").strip()
            items.append(
                DiagnosticItem(
                    severity=m.group("sev"),
                    message=msg,
                    line=int(m.group("line")),
                    column=int(m.group("col")),
                    category=classify_diagnostic(msg),
                )
            )
        # If there was output but no structured matches, add raw
        if not items and output.strip():
            items.append(
                DiagnosticItem(
                    severity="info",
                    message=output.strip()[:2000],
                )
            )
        return items


_runner = LeanRunner()


# ---------------------------------------------------------------------------
# PantographAdapter (stub)
# ---------------------------------------------------------------------------
class PantographAdapter:
    """Interface for structured Lean interaction via Pantograph / PyPantograph.

    This is a stub -- full implementation will use the Pantograph server
    protocol to maintain proof states, apply tactics, and inspect goals.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}

    def create_session(self, theorem: str = "", imports: list[str] | None = None) -> str:
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = {
            "id": session_id,
            "theorem": theorem,
            "imports": imports or [],
            "goals": ["⊢ " + theorem] if theorem else [],
            "tactics_applied": [],
            "snapshots": {},
        }
        return session_id

    def _get(self, session_id: str) -> dict:
        if session_id not in self._sessions:
            raise HTTPException(status_code=404, detail="session not found")
        return self._sessions[session_id]

    def apply_tactic(self, session_id: str, tactic: str, goal_index: int = 0) -> dict:
        sess = self._get(session_id)
        sess["tactics_applied"].append(tactic)
        # Stub: in production, Pantograph would return new goals
        return {
            "success": True,
            "goals_before": list(sess["goals"]),
            "goals_after": sess["goals"],  # unchanged in stub
            "tactic": tactic,
        }

    def get_goals(self, session_id: str) -> list[str]:
        return self._get(session_id)["goals"]

    def snapshot(self, session_id: str) -> str:
        sess = self._get(session_id)
        snap_id = str(uuid.uuid4())
        import copy
        sess["snapshots"][snap_id] = copy.deepcopy(sess)
        return snap_id

    def restore(self, session_id: str, snapshot_id: str) -> bool:
        sess = self._get(session_id)
        snap = sess["snapshots"].get(snapshot_id)
        if not snap:
            return False
        # Restore mutable state
        sess["goals"] = snap["goals"]
        sess["tactics_applied"] = snap["tactics_applied"]
        return True


_pantograph = PantographAdapter()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "lean_env"}


@app.post("/compile")
async def compile_source(req: CompileRequest):
    result = await _runner.compile_source(req.source)
    return result.model_dump()


@app.post("/compile-file")
async def compile_file(req: CompileFileRequest):
    result = await _runner.compile_file(req.path)
    return result.model_dump()


@app.post("/session/create")
async def session_create(theorem: str = "", imports: list[str] | None = None):
    sid = _pantograph.create_session(theorem, imports)
    log.info("session_created", session_id=sid)
    return {"session_id": sid}


@app.post("/session/{session_id}/tactic")
async def session_tactic(session_id: str, req: TacticRequest):
    result = _pantograph.apply_tactic(session_id, req.tactic, req.goal_index)
    return result


@app.get("/session/{session_id}/goals")
async def session_goals(session_id: str):
    goals = _pantograph.get_goals(session_id)
    return {"session_id": session_id, "goals": goals}


@app.post("/session/{session_id}/snapshot")
async def session_snapshot(session_id: str):
    snap_id = _pantograph.snapshot(session_id)
    return {"session_id": session_id, "snapshot_id": snap_id}


@app.post("/session/{session_id}/restore")
async def session_restore(session_id: str, snapshot_id: str):
    ok = _pantograph.restore(session_id, snapshot_id)
    if not ok:
        raise HTTPException(status_code=404, detail="snapshot not found")
    return {"restored": True}
