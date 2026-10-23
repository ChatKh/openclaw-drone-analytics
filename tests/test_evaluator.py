"""Tests for the evaluator (Phase 5, day 8): small cases where the right score is obvious."""
import json

import pytest

from src.evaluation.evaluator import evaluate, format_report


@pytest.fixture
def db(temp_db):
    temp_db.execute("INSERT INTO drones VALUES ('A','VTOL-QuadPlane-X8','v1.0',100,'camera',90,'active')")
    temp_db.executemany(
        "INSERT INTO missions VALUES (?, 'A', 'AM', '2026-06-01 08:00:00', '2026-06-01 09:29:59', 90, 90, 'completed')",
        [("D0001_A_AM",), ("D0002_A_AM",)],
    )
    # 3600 flight rows = 1 flight hour
    temp_db.executemany(
        "INSERT INTO telemetry_logs VALUES (?, 'A', 'D0001_A_AM', 'survey', 50, 50, 8.4, 99.9, 20, 15, 10, 1.0)",
        [(f"2026-06-01 08:{i // 60:02d}:{i % 60:02d}",) for i in range(3600)],
    )
    temp_db.commit()
    return temp_db


def truth(conn, aid, mission, kind, start, end, **params):
    conn.execute("INSERT INTO injected_anomalies VALUES (?, 'A', ?, ?, 'x', ?, ?, ?)",
                 (aid, mission, kind, start, end, json.dumps(params)))


def alert(conn, mission, kind, start, end, detector="rule"):
    conn.execute("INSERT INTO alerts (detector, drone_id, mission_id, anomaly_type, start_time, end_time, score) "
                 "VALUES (?, 'A', ?, ?, ?, ?, 1.0)", (detector, mission, kind, start, end))


def test_perfect_detector(db):
    truth(db, 1, "D0001_A_AM", "gps", "2026-06-01 08:10:00", "2026-06-01 08:12:00")
    alert(db, "D0001_A_AM", "gps", "2026-06-01 08:10:05", "2026-06-01 08:11:50")
    rep = evaluate(db)
    assert (rep.total.precision, rep.total.recall) == (1.0, 1.0)
    assert rep.total.latencies_s == [5.0]


def test_wrong_type_wrong_mission_and_far_away_are_false_alarms(db):
    truth(db, 1, "D0001_A_AM", "gps", "2026-06-01 08:10:00", "2026-06-01 08:12:00")
    alert(db, "D0001_A_AM", "sensor", "2026-06-01 08:10:00", "2026-06-01 08:10:01")  # wrong type
    alert(db, "D0002_A_AM", "gps", "2026-06-01 08:10:00", "2026-06-01 08:12:00")     # wrong mission
    alert(db, "D0001_A_AM", "gps", "2026-06-01 08:40:00", "2026-06-01 08:41:00")     # far in time
    rep = evaluate(db)
    assert rep.total.correct_alerts == 0 and rep.total.false_alarms == 3
    assert rep.total.recall == 0.0
    assert rep.false_alarms_per_hour == 3.0  # 3 false alarms in 1 flight hour


def test_tolerance_is_30_seconds(db):
    truth(db, 1, "D0001_A_AM", "sensor", "2026-06-01 08:10:00", "2026-06-01 08:10:00")
    alert(db, "D0001_A_AM", "sensor", "2026-06-01 08:10:30", "2026-06-01 08:10:31")  # 30 s after: counts
    alert(db, "D0001_A_AM", "sensor", "2026-06-01 08:09:29", "2026-06-01 08:09:29")  # 31 s before: does not
    rep = evaluate(db)
    assert (rep.total.correct_alerts, rep.total.false_alarms) == (1, 1)


def test_two_alerts_on_one_anomaly_count_once_for_recall(db):
    truth(db, 1, "D0001_A_AM", "gps", "2026-06-01 08:10:00", "2026-06-01 08:15:00")
    alert(db, "D0001_A_AM", "gps", "2026-06-01 08:10:00", "2026-06-01 08:11:00")
    alert(db, "D0001_A_AM", "gps", "2026-06-01 08:13:00", "2026-06-01 08:14:00")
    rep = evaluate(db)
    assert rep.total.recall == 1.0 and rep.total.detected == 1
    assert rep.total.precision == 1.0 and rep.total.alerts == 2


def test_missed_list_carries_the_injected_magnitude(db):
    truth(db, 1, "D0001_A_AM", "battery", "2026-06-01 08:10:00", "2026-06-01 08:15:00", drop_pct=7.5)
    rep = evaluate(db)
    assert rep.missed[0]["params"] == {"drop_pct": 7.5}
    assert "drop_pct" in format_report(rep)


def test_only_the_chosen_detector_is_scored(db):
    truth(db, 1, "D0001_A_AM", "gps", "2026-06-01 08:10:00", "2026-06-01 08:12:00")
    alert(db, "D0001_A_AM", "gps", "2026-06-01 08:10:00", "2026-06-01 08:12:00", detector="ml")
    assert evaluate(db, "rule").total.recall == 0.0
    assert evaluate(db, "ml").total.recall == 1.0


def test_no_alerts_gives_undefined_precision_not_a_crash(db):
    truth(db, 1, "D0001_A_AM", "gps", "2026-06-01 08:10:00", "2026-06-01 08:12:00")
    rep = evaluate(db)
    assert rep.total.precision is None
    assert "n/a" in format_report(rep)
