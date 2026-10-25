"""Tests for the dashboard (Phase 6): pages render, empty states don't crash, nothing is written."""
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from src.analytics.rule_detector import run as rule_run
from src.dashboard import queries as Q
from src.db.database import connect, init_db
from src.simulator import anomaly_inject as A
from tests.test_rule_detector import Fixed, clean_mission

PAGES = ["page_overview", "page_missions", "page_alerts", "page_energy", "page_evaluation"]


@pytest.fixture
def dashboard_db(tmp_path, monkeypatch):
    """One flight with a 45 m altitude spike, rule alerts and the matching answer key."""
    path = str(tmp_path / "dash.db")
    conn = connect(path)
    init_db(conn)
    conn.execute("INSERT INTO drones VALUES ('A','VTOL-QuadPlane-X8','v1.0',100,'camera',90,'active')")
    conn.execute("INSERT INTO missions VALUES ('D0001_A_AM','A','AM','2026-06-01 08:00:00',"
                 "'2026-06-01 09:29:59',90,90,'completed')")
    rows = clean_mission()
    when, _, params = A.inject_sensor(rows, 3000, Fixed(45.0))
    conn.executemany(
        "INSERT INTO telemetry_logs VALUES (:timestamp,:drone_id,:mission_id,:flight_mode,:battery_vtol_pct,"
        ":battery_cruise_pct,:lat,:lon,:altitude_m,:ground_speed_mps,:satellite_count,:hdop)",
        [{**r, "timestamp": str(r["timestamp"])} for r in rows],
    )
    conn.execute("INSERT INTO injected_anomalies VALUES (1,'A','D0001_A_AM','sensor','spike',?,?,'{}')",
                 (str(when), str(when)))
    conn.commit()
    rule_run(conn)
    conn.close()
    monkeypatch.setenv("DRONE_DB_PATH", path)
    return path


def run_page(page: str) -> AppTest:
    script = f"from src.dashboard import views as V\nV.sidebar()\nV.{page}()\n"
    return AppTest.from_string(script, default_timeout=60).run()


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_with_data(dashboard_db, page):
    at = run_page(page)
    assert not at.exception, at.exception
    assert not at.info


def test_overview_counts_the_flight(dashboard_db):
    at = run_page("page_overview")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Flights"] == "1" and metrics["Rule alerts"] == "1"


def test_evaluation_page_warns_that_it_reads_the_answer_key(dashboard_db):
    at = run_page("page_evaluation")
    assert "answer key" in at.warning[0].value
    assert any("100% / 100%" in m.value for m in at.metric)


@pytest.mark.parametrize("page", PAGES)
def test_empty_database_shows_help_instead_of_crashing(page):
    at = run_page(page)  # conftest points DRONE_DB_PATH at a fresh, empty database
    assert not at.exception and "no telemetry" in at.info[0].value


def test_missing_database_shows_help(tmp_path, monkeypatch):
    monkeypatch.setenv("DRONE_DB_PATH", str(tmp_path / "nope.db"))
    at = run_page("page_overview")
    assert not at.exception and "No database" in at.info[0].value
    assert not (tmp_path / "nope.db").exists()  # looking must not create the file


def test_dashboard_connection_cannot_write(dashboard_db):
    conn = Q.connect_ro(dashboard_db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM alerts")


def test_downsampled_altitude_keeps_the_spike(dashboard_db):
    conn = Q.connect_ro(dashboard_db)
    tel = Q.mission_telemetry(conn, "D0001_A_AM")
    assert len(tel) == 540  # 90 min at one point per 10 s
    assert tel["altitude_max_m"].max() == 45.0


def test_vtol_is_used_only_in_takeoff_and_landing(dashboard_db):
    e = Q.energy_by_phase(Q.connect_ro(dashboard_db)).set_index("flight_mode")
    assert (e.loc[["transit_out", "survey", "transit_back"], "vtol_used_pct"] == 0).all()
    assert (e.loc[["takeoff", "landing"], "vtol_used_pct"] > 20).all()


def test_sidebar_offers_only_databases_that_exist(dashboard_db, tmp_path, monkeypatch):
    # Inside Docker there is no data/test.db; offering it would only lead to an empty page.
    monkeypatch.chdir(tmp_path)  # no data/ folder here
    at = run_page("page_overview")
    assert at.sidebar.selectbox[0].options == [dashboard_db]
