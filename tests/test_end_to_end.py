"""
End-to-end test for Phase 4: simulator -> CSV -> loaders -> API -> SQLite.
Uses 3 simulated days (not 15) to stay fast; the path is the same.
Run: python -m pytest -q
"""
import csv

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.ingest.load_ground_truth import load_ground_truth
from src.ingest.load_reference import load_reference
from src.ingest.send_telemetry import read_rows, send_rows
from src.simulator.fleet import build_drones, generate_all

DAYS = 3


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture(scope="module")
def simulated():
    telemetry, injected, missions, _ = generate_all(num_days=DAYS, seed=42)
    return {"telemetry": telemetry, "injected": injected, "missions": missions, "drones": build_drones()}


@pytest.fixture
def data_dir(tmp_path, simulated):
    d = tmp_path / "data"
    d.mkdir()
    write_csv(d / "telemetry_logs.csv", simulated["telemetry"])
    write_csv(d / "injected_anomalies.csv", simulated["injected"])
    write_csv(d / "missions.csv", simulated["missions"])
    write_csv(d / "drones.csv", simulated["drones"])
    return d


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_full_path_stores_every_row(data_dir, simulated, temp_db):
    load_reference(data_dir, temp_db)
    load_ground_truth(data_dir, temp_db)

    with TestClient(app) as client:
        totals = send_rows(client, read_rows(data_dir / "telemetry_logs.csv"), batch_size=5000)

    n = len(simulated["telemetry"])
    assert (totals.sent, totals.accepted, totals.duplicates, totals.rejected) == (n, n, 0, 0)
    assert totals.batches == -(-n // 5000)  # ceiling division
    assert count(temp_db, "telemetry_logs") == n
    assert count(temp_db, "missions") == len(simulated["missions"])
    assert count(temp_db, "injected_anomalies") == len(simulated["injected"])
    assert temp_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_sending_everything_twice_doubles_nothing(data_dir, simulated, temp_db):
    load_reference(data_dir, temp_db)
    with TestClient(app) as client:
        send_rows(client, read_rows(data_dir / "telemetry_logs.csv"))
        again = send_rows(client, read_rows(data_dir / "telemetry_logs.csv"))

    n = len(simulated["telemetry"])
    assert (again.accepted, again.duplicates) == (0, n)
    assert count(temp_db, "telemetry_logs") == n


def test_telemetry_before_reference_data_is_rejected_not_lost(data_dir, temp_db):
    # Forgot load_reference: every row is rejected with a reason, nothing is stored half-way.
    with TestClient(app) as client:
        totals = send_rows(client, read_rows(data_dir / "telemetry_logs.csv"))
    assert totals.accepted == 0
    assert totals.rejected == totals.sent
    assert "unknown mission" in totals.examples[0][1][0]
    assert totals.examples[0][0] == 2  # first data line of the CSV
    assert count(temp_db, "telemetry_logs") == 0
