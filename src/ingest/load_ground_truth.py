"""
Load the answer key (injected_anomalies) into the DB.

This is deliberately a separate script, not an API endpoint: the path that
receives telemetry never touches the ground truth. Only the evaluator
(Phase 5) reads this table.

Run:  python -m src.ingest.load_ground_truth [--data-dir data]
Needs reference data first (python -m src.ingest.load_reference).
Safe to run again: anomalies already stored are skipped.
"""
import argparse
import csv
import logging
from pathlib import Path

from src.db.database import connect, init_db

log = logging.getLogger("load_ground_truth")

COLS = ["anomaly_id", "drone_id", "mission_id", "anomaly_type", "subtype",
        "start_time", "end_time", "params"]


def load_ground_truth(data_dir: Path, conn) -> int:
    init_db(conn)
    rows = list(csv.DictReader(open(data_dir / "injected_anomalies.csv", encoding="utf-8")))
    sql = (f"INSERT INTO injected_anomalies ({', '.join(COLS)}) "
           f"VALUES ({', '.join('?' * len(COLS))}) ON CONFLICT (anomaly_id) DO NOTHING")
    try:
        before = conn.total_changes
        conn.executemany(sql, [tuple(r[c] for c in COLS) for r in rows])
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("ground truth load failed; nothing stored")
        raise
    new = conn.total_changes - before
    log.info("injected_anomalies: %d in file, %d new", len(rows), new)
    return new


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--data-dir", default="data", type=Path)
    args = ap.parse_args()
    conn = connect()
    try:
        load_ground_truth(args.data_dir, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
