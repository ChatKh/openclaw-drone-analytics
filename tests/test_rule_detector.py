"""Tests for the rule-based detector v2 (Phase 5, day 9): cases with known answers."""
import random
from datetime import datetime

from src.analytics.rule_detector import RuleConfig, detect_mission, run
from src.simulator import anomaly_inject as A
from src.simulator.disturbance import apply_weather
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


class Windy(random.Random):
    """Weather RNG that always makes a strong-wind flight without multipath."""

    def __init__(self, seed=3):
        super().__init__(seed)
        self.calls = 0

    def random(self):
        self.calls += 1
        if self.calls == 1:
            return 0.0           # windy
        if self.calls == 2:
            return 0.99          # no multipath
        return super().random()

    def uniform(self, a, b):
        return b if (a, b) == (0.3, 1.0) else super().uniform(a, b)  # wind level 1.0


def clean_mission(seed=1):
    random.seed(seed)  # simulator noise uses the global RNG
    return generate_mission("A", "D0001_A_AM", datetime(2026, 6, 1, 8))


def windy_mission(seed=1):
    rows = clean_mission(seed)
    apply_weather(rows, Windy(seed))
    return rows


def detect(rows, cfg=CFG):
    return detect_mission([{**r, "timestamp": str(r["timestamp"])} for r in rows], cfg)


def kinds(alerts):
    return sorted(a.anomaly_type for a in alerts)


# ---------- normal flights must stay quiet ----------

def test_calm_flight_raises_nothing():
    assert detect(clean_mission()) == []


def test_strong_wind_flight_raises_nothing():
    # gust sag, wobbling altitude and extra VTOL use are normal
    for seed in (1, 2, 3):
        assert detect(windy_mission(seed)) == [], f"seed {seed}"


def test_short_multipath_is_not_a_gps_fault():
    rows = clean_mission()
    for r in rows[1500:1520]:  # 20 s of bad GPS under canopy
        r["hdop"], r["satellite_count"] = 3.5, 5
    assert detect(rows) == []


# ---------- faults must be caught ----------

def test_battery_drop_that_stays_is_caught():
    rows = clean_mission()
    start, _, _ = A.inject_battery(rows, 2000, 300, Fixed(8.0))
    [alert] = detect(rows)
    assert alert.anomaly_type == "battery"
    assert abs((datetime.fromisoformat(alert.start_time) - start).total_seconds()) <= 60


def test_battery_drop_is_caught_even_in_strongest_wind():
    rows = windy_mission()
    start, _, _ = A.inject_battery(rows, 2000, 300, Fixed(8.0))
    near = [a for a in detect(rows) if a.anomaly_type == "battery"
            and abs((datetime.fromisoformat(a.start_time) - start).total_seconds()) <= 90]
    assert near  # caught; strongest wind may still add a false alarm elsewhere (known limit)


def test_drop_smaller_than_threshold_is_missed_by_design():
    rows = clean_mission()
    A.inject_battery(rows, 2000, 300, Fixed(2.0))
    assert detect(rows) == []


def test_long_gps_degradation_is_one_alert_covering_it():
    rows = clean_mission()
    start, end, _ = A.inject_gps(rows, 1500, 120, Fixed(1.0))  # 3 satellites, hdop 8
    [alert] = detect(rows)
    assert (alert.anomaly_type, alert.start_time, alert.end_time) == ("gps", str(start), str(end))


def test_single_altitude_spike_is_caught():
    rows = clean_mission()
    A.inject_sensor(rows, 3000, Fixed(25.0))  # only ~4.5 m above normal, but alone
    assert kinds(detect(rows)) == ["sensor"]


def test_low_lipo_at_return_leg():
    rows = clean_mission()
    A.inject_lipo(rows, Fixed(48.0))  # would land with ~8 %: below landing cost + reserve
    assert kinds(detect(rows)) == ["lipo"]


# ---------- storage ----------

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
