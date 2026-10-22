"""
Ingestion API (Phase 4): receive telemetry in batches, validate it, store it.

Run:   uvicorn src.api.main:app --reload      (from the project root)
Docs:  http://127.0.0.1:8000/docs

Design:
- Batches, not single rows: 162k rows one HTTP call each would be far too slow.
- Validate row by row: one bad row never fails the whole batch (blast radius).
  Good rows are stored, bad rows come back with their index and reasons.
- Steps per batch: 1) value checks (Pydantic)  2) reference checks (mission
  exists and belongs to this drone)  3) write; repeats count as duplicates.
- Ground truth (injected_anomalies) is NOT loaded through this API.
  The path that receives telemetry never touches the answer key.
- Reference data (drones, missions) must be loaded first:
  python -m src.ingest.load_reference
"""
import logging
import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import ValidationError

from src.api.models import BatchResult, RejectedRow, TelemetryIn
from src.db.database import get_db
from src.db.repository import insert_telemetry

MAX_BATCH_SIZE = 5000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ingestion")

app = FastAPI(title="OpenClaw Drone Analytics: Ingestion API", version="0.2.0")


def _format_errors(exc: ValidationError) -> list[str]:
    """Turn Pydantic errors into short, readable lines: 'field: message'."""
    out = []
    for e in exc.errors():
        field = ".".join(str(p) for p in e["loc"]) or "(row)"
        out.append(f"{field}: {e['msg']}")
    return out


def validate_batch(rows: list[Any]) -> tuple[list[tuple[int, TelemetryIn]], list[RejectedRow]]:
    """Validate each row on its own. Returns ([(index, row)], rejected)."""
    accepted: list[tuple[int, TelemetryIn]] = []
    rejected: list[RejectedRow] = []
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            rejected.append(RejectedRow(index=i, errors=["(row): must be a JSON object"]))
            continue
        try:
            accepted.append((i, TelemetryIn.model_validate(raw)))
        except ValidationError as exc:
            rejected.append(RejectedRow(index=i, errors=_format_errors(exc)))
    return accepted, rejected


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/telemetry/batch", response_model=BatchResult)
def ingest_telemetry(rows: list[Any], conn: sqlite3.Connection = Depends(get_db)) -> BatchResult:
    if not rows:
        raise HTTPException(status_code=422, detail="batch is empty")
    if len(rows) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"batch has {len(rows)} rows; max is {MAX_BATCH_SIZE}",
        )

    valid, rejected = validate_batch(rows)
    log.info("batch received=%d valid=%d invalid=%d", len(rows), len(valid), len(rejected))

    try:
        written = insert_telemetry(conn, [r for _, r in valid])
    except sqlite3.Error:
        raise HTTPException(status_code=500, detail="database write failed; batch not stored")

    for pos, reason in written.rejected:  # positions in `valid` -> index in the request
        rejected.append(RejectedRow(index=valid[pos][0], errors=[reason]))
    rejected.sort(key=lambda r: r.index)

    for r in rejected[:5]:  # keep the log readable on a very bad batch
        log.warning("rejected row %d: %s", r.index, "; ".join(r.errors))

    return BatchResult(
        received=len(rows),
        accepted=written.inserted,
        duplicates=written.duplicates,
        rejected=rejected,
    )
