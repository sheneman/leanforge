"""Shared schemas for forge-lean-prover services."""
from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
from datetime import datetime
import uuid

class ProofStatus(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"
    ELABORATION_ERROR = "elaboration_error"
    TIMEOUT = "timeout"
    UNKNOWN_IDENTIFIER = "unknown_identifier"
    TYPE_MISMATCH = "type_mismatch"
    PENDING = "pending"

class ProofTask(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    theorem_statement: str
    context: str = ""
    imports: list[str] = Field(default_factory=list)
    max_branches: int = 50
    timeout_secs: int = 120
    metadata: dict = Field(default_factory=dict)

class VerificationResult(BaseModel):
    task_id: str
    status: ProofStatus
    proof_text: str = ""
    diagnostics: list[str] = Field(default_factory=list)
    lean_output: str = ""
    elapsed_secs: float = 0.0
    verified_at: Optional[datetime] = None

class TheoremMatch(BaseModel):
    name: str
    statement: str
    module: str = ""
    score: float = 0.0
    source: str = "mathlib"

class RetrievalResult(BaseModel):
    query: str
    results: list[TheoremMatch] = Field(default_factory=list)
    source: str = "local"  # local | web

# Fix forward ref
RetrievalResult.model_rebuild()

class BranchState(BaseModel):
    branch_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    parent_id: Optional[str] = None
    tactics: list[str] = Field(default_factory=list)
    goals_before: list[str] = Field(default_factory=list)
    goals_after: list[str] = Field(default_factory=list)
    status: ProofStatus = ProofStatus.PENDING
    score: float = 0.0
    depth: int = 0

class TheoremSearchRequest(BaseModel):
    query: str
    top_k: int = 10
    filters: dict = Field(default_factory=dict)

class DiagnosticItem(BaseModel):
    severity: str  # error | warning | info
    message: str
    line: Optional[int] = None
    column: Optional[int] = None
    category: str = ""  # elaboration_error, type_mismatch, unknown_identifier, etc.

class CompileCheckResult(BaseModel):
    source: str
    success: bool
    diagnostics: list[DiagnosticItem] = Field(default_factory=list)
    elapsed_secs: float = 0.0

CompileCheckResult.model_rebuild()

class TelemetryEvent(BaseModel):
    event_type: str
    task_id: str = ""
    data: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
