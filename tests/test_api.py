"""Tests for the ingestion API (Phase 4, day 2). Run: python -m pytest -q"""
import pytest
from fastapi.testclient import TestClient

from src.api.main import MAX_BATCH_SIZE, app, validate_batch
from src.simulator.fleet import generate_all

client = TestClient(app)

pytestmark = pytest.mark.usefixtures("seeded_db")


def good_row(**overrides):
    row = {
        "timestamp": "2026-06-01 08:00:00",
        "drone_id": "A",
        "mission_id": "D0001_A_AM",
        "flight_mode": "takeoff",
        "battery_vtol_pct": 100.0,
        "battery_cruise_pct": 100.0,
        "lat": 8.4304,
        "lon": 99.9631,
        "altitude_m": 0.0,
        "ground_speed_mps": 0.0,
        "satellite_count": 10,
        "hdop": 0.9,
    }
    row.update(overrides)
    return row


def post(rows):
    return client.post("/telemetry/batch", json=rows)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_good_batch_is_fully_accepted():
    r = post([good_row(), good_row(timestamp="2026-06-01 08:00:01")])
    assert r.status_code == 200
    assert r.json() == {"received": 2, "accepted": 2, "duplicates": 0, "rejected": []}


@pytest.mark.parametrize(
    "bad, field",
    [
        ({"battery_vtol_pct": 150}, "battery_vtol_pct"),
        ({"battery_cruise_pct": -1}, "battery_cruise_pct"),
        ({"flight_mode": "hover"}, "flight_mode"),
        ({"mission_id": "mission-1"}, "mission_id"),
        ({"drone_id": "drone_a"}, "drone_id"),
        ({"hdop": 0}, "hdop"),
        ({"satellite_count": -3}, "satellite_count"),
        ({"lat": 123.0}, "lat"),
        ({"timestamp": "yesterday"}, "timestamp"),
        ({"wind_speed_mps": 5.0}, "wind_speed_mps"),  # unknown column = contract change
    ],
)
def test_bad_value_is_rejected_with_reason(bad, field):
    body = post([good_row(**bad)]).json()
    assert body["accepted"] == 0
    assert body["rejected"][0]["index"] == 0
    assert any(e.startswith(field) for e in body["rejected"][0]["errors"])


def test_missing_field_is_rejected():
    row = good_row()
    del row["hdop"]
    body = post([row]).json()
    assert body["rejected"][0]["errors"] == ["hdop: Field required"]


def test_one_bad_row_does_not_fail_the_batch():
    rows = [good_row(), good_row(battery_vtol_pct=150), good_row(timestamp="2026-06-01 08:00:01"), "not a row"]
    body = post(rows).json()
    assert body["received"] == 4
    assert body["accepted"] == 2
    assert [r["index"] for r in body["rejected"]] == [1, 3]


def test_injected_anomaly_values_are_still_valid_data():
    # Anomalies are detected in Phase 5, not filtered out at ingestion.
    spike = good_row(flight_mode="survey", altitude_m=45.0)
    gps = good_row(flight_mode="survey", satellite_count=3, hdop=8.0, timestamp="2026-06-01 08:00:01")
    assert post([spike, gps]).json()["accepted"] == 2


def test_empty_batch_is_refused():
    assert post([]).status_code == 422


def test_oversized_batch_is_refused():
    assert post([good_row()] * (MAX_BATCH_SIZE + 1)).status_code == 413


def test_simulator_output_matches_the_contract():
    # The schema is the contract: everything the simulator makes must pass.
    telemetry, _, _, _ = generate_all(num_days=2, seed=42)
    rows = [{**r, "timestamp": str(r["timestamp"])} for r in telemetry]
    valid, rejected = validate_batch(rows)
    assert rejected == []
    assert len(valid) == len(telemetry)
