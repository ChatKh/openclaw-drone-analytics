"""
Load reference data (drones, missions) from the simulator CSVs into the DB.
Must run before telemetry is sent, because telemetry points at missions.

Run:  python -m src.ingest.load_reference [--data-dir data]
Safe to run again: rows already stored are skipped.
"""
import argparse
import csv
import logging
from pathlib import Path

from src.db.database import connect, init_db

log = logging.getLogger("load_reference")

DRONE_COLS = ["drone_id", "model", "firmware", "battery_health_pct",
              "payload_type", "max_flight_time_min", "status"]
# has_injected_anomaly from missions.csv is NOT stored: it is ground truth.
MISSION_COLS = ["mission_id", "drone_id", "role", "start_time", "end_time",
                "planned_duration_min", "actual_duration_min", "mission_status"]


def _insert(conn, table: str, cols: list[str], rows: list[dict], key: str) -> int:
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT ({key}) DO NOTHING")
    before = conn.total_changes
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return conn.total_changes - before


def load_reference(data_dir: Path, conn) -> dict:
    init_db(conn)
    drones = list(csv.DictReader(open(data_dir / "drones.csv", encoding="utf-8")))
    missions = list(csv.DictReader(open(data_dir / "missions.csv", encoding="utf-8")))
    try:
        new_drones = _insert(conn, "drones", DRONE_COLS, drones, "drone_id")
        new_missions = _insert(conn, "missions", MISSION_COLS, missions, "mission_id")
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("reference load failed; nothing stored")
        raise
    log.info("drones: %d in file, %d new | missions: %d in file, %d new",
             len(drones), new_drones, len(missions), new_missions)
    return {"drones": new_drones, "missions": new_missions}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--data-dir", default="data", type=Path)
    args = ap.parse_args()
    conn = connect()
    try:
        load_reference(args.data_dir, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
