"""Tests for the rule-based detector (Phase 5, day 6): cases with known answers."""
import random
from datetime import datetime

from src.analytics.rule_detector import RuleConfig, detect_mission, run
from src.simulator import anomaly_inject as A
from src.simulator.simulator import generate_mission

CFG = RuleConfig()


class Fixed(random.Random):
    """Injects an exact magnitude, so each test knows the answer."""

    def __init__(self, value):
        super().__init__(0)
        self.value = value

    def uniform(self, a, b):
        return self.value

    def random(self):
        return self.value


def clean_mission(seed=1):
    random.seed(seed)  # simulator noise uses the global RNG
    return generate_mission("A", "D0001_A_AM", datetime(2026, 6, 1, 8))


def detect(rows, cfg=CFG):
    return detect_mission([{**r, "timestamp": str(r["timestamp"])} for r in rows], cfg)


def kinds(alerts):
    return [a.anomaly_type for a in alerts]


def test_clean_mission_raises_nothing():
    assert detect(clean_mission()) == []


def test_clear_battery_drop_gives_one_alert_at_the_drop():
    rows = clean_mission()
    start, _, _ = A.inject_battery(rows, 2000, 300, Fixed(20.0))
    [alert] = detect(rows)
    assert alert.anomaly_type == "battery"
    assert alert.start_time == str(start)
    assert alert.score > 15


def test_small_battery_drop_is_missed_by_design():
    rows = clean_mission()
    A.inject_battery(rows, 2000, 300, Fixed(8.0))
    assert detect(rows) == []


def test_bad_gps_window_is_one_alert_covering_it():
    rows = clean_mission()
    start, end, _ = A.inject_gps(rows, 1500, 120, Fixed(1.0))  # severity 1 -> 3 satellites, hdop 8
    [alert] = detect(rows)
    assert (alert.anomaly_type, alert.start_time, alert.end_time) == ("gps", str(start), str(end))


def test_altitude_spike_up_and_down_is_one_alert():
    rows = clean_mission()
    A.inject_sensor(rows, 3000, Fixed(45.0))
    [alert] = detect(rows)
    assert alert.anomaly_type == "sensor"
    assert alert.score > 20


def test_low_lipo_at_return_leg():
    rows = clean_mission()
    A.inject_lipo(rows, Fixed(35.0))
    assert kinds(detect(rows)) == ["lipo"]


def test_lipo_above_threshold_is_missed_by_design():
    rows = clean_mission()
    A.inject_lipo(rows, Fixed(48.0))
    assert detect(rows) == []


def test_short_gps_blip_can_be_filtered_by_min_duration():
    rows = clean_mission()
    rows[1500]["satellite_count"] = 5  # one noisy second
    assert kinds(detect(rows)) == ["gps"]
    assert detect(rows, RuleConfig(gps_min_s=3)) == []


def test_run_replaces_previous_rule_alerts(temp_db):
    temp_db.execute("INSERT INTO drones VALUES ('A','VTOL-QuadPlane-X8','v1.0',100,'camera',90,'active')")
    temp_db.execute("INSERT INTO missions VALUES ('D0001_A_AM','A','AM','2026-06-01 08:00:00',"
                    "'2026-06-01 09:29:59',90,90,'completed')")
    rows = clean_mission()
    A.inject_sensor(rows, 3000, Fixed(45.0))
    temp_db.executemany(
        "INSERT INTO telemetry_logs VALUES (:timestamp,:drone_id,:mission_id,:flight_mode,:battery_vtol_pct,"
        ":battery_cruise_pct,:lat,:lon,:altitude_m,:ground_speed_mps,:satellite_count,:hdop)",
        [{**r, "timestamp": str(r["timestamp"])} for r in rows],
    )
    temp_db.commit()

    run(temp_db)
    run(temp_db)  # second run must not double the alerts
    stored = temp_db.execute("SELECT detector, anomaly_type FROM alerts").fetchall()
    assert [tuple(r) for r in stored] == [("rule", "sensor")]

