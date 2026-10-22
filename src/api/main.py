"""
Ingestion API (Phase 4, day 2): receive telemetry in batches and validate it.

Run:   uvicorn src.api.main:app --reload      (from the project root)
Docs:  http://127.0.0.1:8000/docs

Design:
- Batches, not single rows: 162k rows one HTTP call each would be far too slow.
- Validate row by row: one bad row never fails the whole batch (blast radius).
  Good rows are accepted, bad rows come back with their index and reasons.
- Ground truth (injected_anomalies) is NOT loaded through this API.
  The path that receives telemetry never touches the answer key.
- Day 2 has no database yet: accepted rows are counted only. Day 3 adds SQLite.
"""
import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from src.api.models import BatchResult, RejectedRow, TelemetryIn

MAX_BATCH_SIZE = 5000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ingestion")

app = FastAPI(title="OpenClaw Drone Analytics: Ingestion API", version="0.1.0")


def _format_errors(exc: ValidationError) -> list[str]:
    """Turn Pydantic errors into short, readable lines: 'field: message'."""
    out = []
    for e in exc.errors():
        field = ".".join(str(p) for p in e["loc"]) or "(row)"
        out.append(f"{field}: {e['msg']}")
    return out


def validate_batch(rows: list[Any]) -> tuple[list[TelemetryIn], list[RejectedRow]]:
    """Validate each row on its own. Returns (accepted, rejected)."""
    accepted: list[TelemetryIn] = []
    rejected: list[RejectedRow] = []
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            rejected.append(RejectedRow(index=i, errors=["(row): must be a JSON object"]))
            continue
        try:
            accepted.append(TelemetryIn.model_validate(raw))
        except ValidationError as exc:
            rejected.append(RejectedRow(index=i, errors=_format_errors(exc)))
    return accepted, rejected


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/telemetry/batch", response_model=BatchResult)
def ingest_telemetry(rows: list[Any]) -> BatchResult:
    if not rows:
        raise HTTPException(status_code=422, detail="batch is empty")
    if len(rows) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"batch has {len(rows)} rows; max is {MAX_BATCH_SIZE}",
        )

    accepted, rejected = validate_batch(rows)

    log.info("batch received=%d accepted=%d rejected=%d", len(rows), len(accepted), len(rejected))
    for r in rejected[:5]:  # keep the log readable on a very bad batch
        log.warning("rejected row %d: %s", r.index, "; ".join(r.errors))

    return BatchResult(received=len(rows), accepted=len(accepted), rejected=rejected)
