"""Tests for fleet health (Phase 5, day 5): small hand-made missions with known answers."""
from pathlib import Path

from src.analytics.fleet_health import fleet_health, mission_summary


def add_mission(conn, mission_id, drone_id, role, readings):
    """readings: list of (vtol, cruise, altitude, satellites, hdop), one per second."""
    conn.execute(
        "INSERT INTO missions VALUES (?, ?, ?, '2026-06-01 08:00:00', '2026-06-01 09:29:59', 90, 90, 'completed')",
        (mission_id, drone_id, role),
    )
    conn.executemany(
        "INSERT INTO telemetry_logs VALUES (?, ?, ?, 'survey', ?, ?, 8.4, 99.9, ?, 15, ?, ?)",
        [
            (f"2026-06-01 08:00:{i:02d}", drone_id, mission_id, v, c, alt, sat, hdop)
            for i, (v, c, alt, sat, hdop) in enumerate(readings)
        ],
    )
    conn.commit()


def seed(conn):
    conn.executemany(
        "INSERT INTO drones VALUES (?, 'VTOL-QuadPlane-X8', 'v1.0', 100, 'camera', 90, 'active')",
        [("A",), ("B",)],
    )
    # A: calm flight, then a 20 m altitude jump at second 2
    add_mission(conn, "D0001_A_AM", "A", "AM", [
        (100, 100, 20.0, 10, 0.9),
        (90, 99, 20.5, 9, 1.0),
        (80, 98, 40.5, 4, 6.0),
        (70, 97, 20.4, 10, 0.9),
    ])
    # B: standby only, must not count as a flight
    conn.execute(
        "INSERT INTO missions VALUES ('D0001_B_IDLE', 'B', 'standby', '2026-06-01 06:00:00', "
        "'2026-06-01 18:00:00', 0, 0, 'standby')"
    )
    conn.commit()


def test_mission_summary_numbers(temp_db):
    seed(temp_db)
    [m] = mission_summary(temp_db)
    assert m["mission_id"] == "D0001_A_AM"
    assert m["rows"] == 4
    assert m["vtol_used_pct"] == 30.0          # 100 -> 70
    assert m["cruise_used_pct"] == 3.0         # 100 -> 97
    assert m["vtol_at_touchdown_pct"] == 70
    assert m["min_satellites"] == 4
    assert m["max_hdop"] == 6.0
    assert m["max_alt_step_m"] == 20.1         # 40.5 - 20.4 on the way back down


def test_fleet_health_per_drone(temp_db):
    seed(temp_db)
    health = {h["drone_id"]: h for h in fleet_health(temp_db)}
    assert health["A"]["flights"] == 1 and health["A"]["standby_days"] == 0
    assert health["B"]["flights"] == 0 and health["B"]["standby_days"] == 1
    assert health["B"]["flight_hours"] == 0


def test_empty_database_gives_rows_not_errors(temp_db):
    assert mission_summary(temp_db) == []
    assert fleet_health(temp_db) == []


def test_analytics_never_reads_the_answer_key():
    # Detectors and health numbers must be built from telemetry only.
    for path in Path("src/analytics").glob("*.py"):
        assert "injected_anomalies" not in path.read_text(encoding="utf-8"), f"{path} reads the answer key"
