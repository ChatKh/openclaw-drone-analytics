"""Contract v2: a source may have no separate VTOL pack (battery_vtol_pct = NULL)."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.analytics import ml_detector
from src.analytics.rule_detector import RuleConfig, detect_mission
from src.api.main import app
from src.db.database import SCHEMA_PATH, connect, init_db
from tests.test_api import good_row
from tests.test_rule_detector import clean_mission

def make_v1_database(path: str) -> None:
    """A database as schema v1 created it: the same tables, battery_vtol_pct NOT NULL, one row."""
    v2 = SCHEMA_PATH.read_text(encoding="utf-8")
    v2_line = "    battery_vtol_pct    REAL CHECK (battery_vtol_pct BETWEEN 0 AND 100),  -- NULL: no VTOL pack in this source"
    assert v2_line in v2
    raw = sqlite3.connect(path)
    raw.executescript(v2.replace(v2_line, "    battery_vtol_pct    REAL NOT NULL CHECK (battery_vtol_pct BETWEEN 0 AND 100),"))
    raw.executescript("""
INSERT INTO drones VALUES ('A','X8','v1',100,'camera',90,'active');
INSERT INTO missions VALUES ('D0001_A_AM','A','AM','2026-06-01 08:00:00','2026-06-01 09:30:00',90,90,'ok');
INSERT INTO telemetry_logs VALUES ('2026-06-01 08:00:00','A','D0001_A_AM','takeoff',99.5,100,8.4,99.9,0,0,10,0.9);
""")
    raw.close()


def test_api_accepts_null_and_empty_vtol(seeded_db):
    client = TestClient(app)
    r = client.post("/telemetry/batch", json=[
        good_row(battery_vtol_pct=None),
        good_row(timestamp="2026-06-01 08:00:01", battery_vtol_pct=""),  # empty CSV cell
    ])
    assert r.json()["accepted"] == 2
    stored = [row[0] for row in seeded_db.execute("SELECT battery_vtol_pct FROM telemetry_logs")]
    assert stored == [None, None]


def test_api_still_rejects_a_missing_vtol_column(seeded_db):
    row = good_row()
    del row["battery_vtol_pct"]
    r = TestClient(app).post("/telemetry/batch", json=[row])
    assert r.json()["accepted"] == 0 and "battery_vtol_pct" in r.json()["rejected"][0]["errors"][0]


def test_v1_database_is_migrated_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "v1.db")
    make_v1_database(path)
    conn = connect(path)
    init_db(conn)  # migrates
    notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(telemetry_logs)")}
    assert notnull["battery_vtol_pct"] == 0 and notnull["battery_cruise_pct"] == 1
    assert conn.execute("SELECT battery_vtol_pct FROM telemetry_logs").fetchone()[0] == 99.5
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    indexes = [r[1] for r in conn.execute("PRAGMA index_list(telemetry_logs)")]
    assert "idx_telemetry_mission" in indexes
    conn.execute("INSERT INTO telemetry_logs VALUES ('2026-06-01 08:00:01','A','D0001_A_AM','takeoff',"
                 "NULL,100,8.4,99.9,0,0,10,0.9)")
    init_db(conn)  # a second run changes nothing
    assert conn.execute("SELECT COUNT(*) FROM telemetry_logs").fetchone()[0] == 2


def test_read_only_connection_leaves_a_v1_database_readable(tmp_path):
    path = str(tmp_path / "v1.db")
    make_v1_database(path)
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    init_db(ro)  # must not raise
    assert ro.execute("SELECT COUNT(*) FROM telemetry_logs").fetchone()[0] == 1


def flight_without_vtol():
    return [{**r, "timestamp": str(r["timestamp"]), "battery_vtol_pct": None} for r in clean_mission()]


def test_rules_skip_the_vtol_check_when_there_is_no_vtol_pack():
    assert detect_mission(flight_without_vtol(), RuleConfig()) == []


@pytest.fixture(scope="module")
def small_bundle():
    return ml_detector.train(n_flights=10, seed=9000, n_calib=10, calib_seed=9100)


def test_ml_ignores_a_missing_feature_instead_of_crashing(small_bundle):
    assert ml_detector.detect_mission(flight_without_vtol(), small_bundle) == []
