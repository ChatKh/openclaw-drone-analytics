"""Tests for the database layer and loaders (Phase 4, day 3). Run: python -m pytest -q"""
import csv
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.ingest.load_ground_truth import load_ground_truth
from src.ingest.load_reference import load_reference
from tests.test_api import good_row

client = TestClient(app)


def post(rows):
    return client.post("/telemetry/batch", json=rows).json()


# ---------- schema ----------

def test_schema_has_the_v1_tables(temp_db):
    names = {r[0] for r in temp_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"drones", "missions", "telemetry_logs", "injected_anomalies", "alerts"} <= names


def test_foreign_keys_are_on(temp_db):
    assert temp_db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_missions_table_does_not_hold_ground_truth(temp_db):
    cols = {r["name"] for r in temp_db.execute("PRAGMA table_info(missions)")}
    assert "has_injected_anomaly" not in cols


def test_db_refuses_telemetry_for_unknown_mission(seeded_db):
    with pytest.raises(sqlite3.IntegrityError):
        seeded_db.execute(
            "INSERT INTO telemetry_logs VALUES ('2026-06-01 08:00:00','A','D9999_A_AM','survey',"
            "50,50,8.4,99.9,20,10,10,1.0)"
        )


def test_alert_detector_must_be_rule_or_ml(seeded_db):
    with pytest.raises(sqlite3.IntegrityError):
        seeded_db.execute(
            "INSERT INTO alerts (detector, drone_id, mission_id, anomaly_type, start_time, end_time) "
            "VALUES ('magic','A','D0001_A_AM','gps','2026-06-01 08:10:00','2026-06-01 08:11:00')"
        )


def test_ground_truth_params_must_be_json(seeded_db):
    with pytest.raises(sqlite3.IntegrityError):
        seeded_db.execute(
            "INSERT INTO injected_anomalies VALUES (1,'A','D0001_A_AM','sensor','spike',"
            "'2026-06-01 08:10:00','2026-06-01 08:10:00','{''field'': ''altitude_m''}')"
        )


# ---------- API writes ----------

@pytest.mark.usefixtures("seeded_db")
def test_accepted_rows_are_stored(temp_db):
    post([good_row(), good_row(timestamp="2026-06-01 08:00:01")])
    assert temp_db.execute("SELECT COUNT(*) FROM telemetry_logs").fetchone()[0] == 2


@pytest.mark.usefixtures("seeded_db")
def test_sending_the_same_batch_twice_counts_duplicates(temp_db):
    batch = [good_row(), good_row(timestamp="2026-06-01 08:00:01")]
    first, second = post(batch), post(batch)
    assert (first["accepted"], first["duplicates"]) == (2, 0)
    assert (second["accepted"], second["duplicates"]) == (0, 2)
    assert temp_db.execute("SELECT COUNT(*) FROM telemetry_logs").fetchone()[0] == 2


@pytest.mark.usefixtures("seeded_db")
def test_duplicate_inside_one_batch():
    body = post([good_row(), good_row()])
    assert (body["accepted"], body["duplicates"]) == (1, 1)


@pytest.mark.usefixtures("seeded_db")
def test_unknown_mission_is_rejected_not_crashed():
    body = post([good_row(), good_row(mission_id="D0099_A_AM", timestamp="2026-06-01 08:00:01")])
    assert body["accepted"] == 1
    assert body["rejected"][0]["index"] == 1
    assert "unknown mission" in body["rejected"][0]["errors"][0]


@pytest.mark.usefixtures("seeded_db")
def test_mission_of_another_drone_is_rejected():
    body = post([good_row(drone_id="B")])  # D0001_A_AM belongs to A
    assert body["accepted"] == 0
    assert "belongs to drone A" in body["rejected"][0]["errors"][0]


@pytest.mark.usefixtures("seeded_db")
def test_rejected_indexes_point_at_the_request(temp_db):
    # value error at 0, reference error at 2: both indexes must match the request
    rows = [good_row(hdop=0), good_row(), good_row(mission_id="D0099_A_AM", timestamp="2026-06-01 08:00:02")]
    body = post(rows)
    assert [r["index"] for r in body["rejected"]] == [0, 2]


# ---------- loaders ----------

def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    write_csv(d / "drones.csv", [
        {"drone_id": "A", "model": "VTOL-QuadPlane-X8", "firmware": "v1.0", "battery_health_pct": 100,
         "payload_type": "camera", "max_flight_time_min": 90, "status": "active"},
    ])
    write_csv(d / "missions.csv", [
        {"mission_id": "D0001_A_AM", "drone_id": "A", "role": "AM", "start_time": "2026-06-01 08:00:00",
         "end_time": "2026-06-01 09:29:59", "planned_duration_min": 90, "actual_duration_min": 90.0,
         "mission_status": "completed", "has_injected_anomaly": "True"},
    ])
    write_csv(d / "injected_anomalies.csv", [
        {"anomaly_id": 1, "drone_id": "A", "mission_id": "D0001_A_AM", "anomaly_type": "sensor",
         "subtype": "spike", "start_time": "2026-06-01 08:40:06", "end_time": "2026-06-01 08:40:06",
         "params": json.dumps({"field": "altitude_m", "value": 45.0})},
    ])
    return d


def test_loaders_store_reference_then_ground_truth(data_dir, temp_db):
    assert load_reference(data_dir, temp_db) == {"drones": 1, "missions": 1}
    assert load_ground_truth(data_dir, temp_db) == 1
    params = temp_db.execute("SELECT json_extract(params, '$.value') FROM injected_anomalies").fetchone()[0]
    assert params == 45.0


def test_loaders_can_run_twice(data_dir, temp_db):
    load_reference(data_dir, temp_db)
    load_ground_truth(data_dir, temp_db)
    assert load_reference(data_dir, temp_db) == {"drones": 0, "missions": 0}
    assert load_ground_truth(data_dir, temp_db) == 0


def test_ground_truth_needs_reference_data_first(data_dir, temp_db):
    with pytest.raises(sqlite3.IntegrityError):
        load_ground_truth(data_dir, temp_db)
