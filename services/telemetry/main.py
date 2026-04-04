"""Telemetry service for forge-lean-prover.

Collects and serves structured events for observability and debugging.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI

from services.schemas import TelemetryEvent

load_dotenv()

log = structlog.get_logger()

LOG_DIR = os.getenv("TELEMETRY_LOG_DIR", "data/logs")
FLUSH_THRESHOLD = int(os.getenv("TELEMETRY_FLUSH_THRESHOLD", "1000"))

app = FastAPI(title="Telemetry Service", version="0.1.0")


# ---------------------------------------------------------------------------
# In-memory event store
# ---------------------------------------------------------------------------
_events: list[TelemetryEvent] = []
_flushed_count: int = 0


def _flush_to_disk() -> int:
    """Write events to a JSONL file in LOG_DIR and clear memory."""
    global _events, _flushed_count
    if not _events:
        return 0

    log_path = Path(LOG_DIR)
    log_path.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    outfile = log_path / f"events_{ts}.jsonl"

    count = len(_events)
    with outfile.open("w") as f:
        for ev in _events:
            f.write(ev.model_dump_json() + "\n")

    log.info("events_flushed", count=count, path=str(outfile))
    _flushed_count += count
    _events = []
    return count


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "telemetry"}


@app.post("/events")
async def log_event(event: TelemetryEvent):
    _events.append(event)
    log.debug("event_logged", event_type=event.event_type, task_id=event.task_id)

    # Auto-flush when threshold reached
    if len(_events) >= FLUSH_THRESHOLD:
        _flush_to_disk()

    return {"accepted": True, "buffer_size": len(_events)}


@app.get("/events/{task_id}")
async def get_events(task_id: str):
    matching = [ev for ev in _events if ev.task_id == task_id]
    return {
        "task_id": task_id,
        "count": len(matching),
        "events": [ev.model_dump() for ev in matching],
    }


@app.get("/metrics")
async def metrics():
    type_counts = Counter(ev.event_type for ev in _events)
    task_counts = Counter(ev.task_id for ev in _events if ev.task_id)
    return {
        "total_buffered": len(_events),
        "total_flushed": _flushed_count,
        "by_event_type": dict(type_counts),
        "by_task_id": dict(task_counts),
    }


@app.post("/flush")
async def flush():
    count = _flush_to_disk()
    return {"flushed": count}
