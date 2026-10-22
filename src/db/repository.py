"""
Writes to the database. Each function works on one batch inside one transaction:
either the whole batch's accepted rows are stored, or none are (on a DB error).
"""
import logging
import sqlite3
from dataclasses import dataclass, field

from src.api.models import TelemetryIn

log = logging.getLogger("repository")

TELEMETRY_COLUMNS = [
    "timestamp", "drone_id", "mission_id", "flight_mode",
    "battery_vtol_pct", "battery_cruise_pct", "lat", "lon", "altitude_m",
    "ground_speed_mps", "satellite_count", "hdop",
]
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class WriteResult:
    inserted: int = 0
    duplicates: int = 0
    # (index in the list passed in, reason)
    rejected: list[tuple[int, str]] = field(default_factory=list)


def _mission_owners(conn: sqlite3.Connection, mission_ids: set[str]) -> dict[str, str]:
    """mission_id -> drone_id for the missions that exist."""
    if not mission_ids:
        return {}
    marks = ",".join("?" * len(mission_ids))
    rows = conn.execute(
        f"SELECT mission_id, drone_id FROM missions WHERE mission_id IN ({marks})",
        list(mission_ids),
    ).fetchall()
    return {r["mission_id"]: r["drone_id"] for r in rows}


def insert_telemetry(conn: sqlite3.Connection, rows: list[TelemetryIn]) -> WriteResult:
    """
    Store validated telemetry.
    - Unknown mission, or a mission that belongs to another drone -> rejected with reason.
    - Same (drone_id, timestamp) already stored, or twice in this batch -> counted as duplicate.
    """
    result = WriteResult()
    owners = _mission_owners(conn, {r.mission_id for r in rows})

    to_write = []
    for i, r in enumerate(rows):
        owner = owners.get(r.mission_id)
        if owner is None:
            result.rejected.append((i, f"mission_id: unknown mission '{r.mission_id}'"))
        elif owner != r.drone_id:
            result.rejected.append(
                (i, f"drone_id: mission '{r.mission_id}' belongs to drone {owner}, not {r.drone_id}")
            )
        else:
            values = r.model_dump()
            values["timestamp"] = r.timestamp.strftime(TS_FORMAT)
            to_write.append(tuple(values[c] for c in TELEMETRY_COLUMNS))

    sql = (
        f"INSERT INTO telemetry_logs ({', '.join(TELEMETRY_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(TELEMETRY_COLUMNS))}) "
        "ON CONFLICT (drone_id, timestamp) DO NOTHING"
    )
    try:
        before = conn.total_changes
        conn.executemany(sql, to_write)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        log.exception("telemetry write failed; batch rolled back")
        raise

    result.inserted = conn.total_changes - before
    result.duplicates = len(to_write) - result.inserted
    log.info(
        "telemetry write inserted=%d duplicates=%d reference_errors=%d",
        result.inserted, result.duplicates, len(result.rejected),
    )
    return result
