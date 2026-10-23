"""Tests for the ML detector (Phase 5, days 11-12). A small model is trained once for the module."""
from datetime import datetime

import pytest

from src.analytics import ml_detector as M
from src.analytics.rule_detector import run as rule_run
from src.simulator import anomaly_inject as A
from tests.test_rule_detector import Fixed, clean_mission


@pytest.fixture(scope="module")
def bundle():
    return M.train(n_flights=30, seed=9000, n_calib=20, calib_seed=9100)


def detect(rows, bundle, mode=M.DEFAULT_MODE):
    return M.detect_mission([{**r, "timestamp": str(r["timestamp"])} for r in rows], bundle, mode)


def near(alerts, kind, when, within_s=60):
    return [a for a in alerts if a.anomaly_type == kind
            and abs((datetime.fromisoformat(a.start_time) - when).total_seconds()) <= within_s]


def test_windows_cover_cruise_only_with_nine_features():
    rows = clean_mission()
    feats = M.window_features(rows)
    cruise = sum(r["flight_mode"] in M.CRUISE_MODES for r in rows)
    assert len(feats) == (cruise - M.WINDOW_S) // M.STEP_S + 1
    assert all(len(f) == len(M.FEATURES) for _, _, f in feats)


def test_thresholds_come_from_calibration_flights(bundle):
    assert bundle["margin"] in M.MARGINS
    assert bundle["calibrated_on"]["seed"] != bundle["trained_on"]["seed"]
    assert bundle["if_threshold"] > 0


def test_calm_normal_flight_raises_nothing(bundle):
    assert detect(clean_mission(), bundle) == []


def test_unknown_mode_is_rejected(bundle):
    with pytest.raises(ValueError):
        detect(clean_mission(), bundle, mode="magic")


def test_altitude_spike_is_caught(bundle):
    rows = clean_mission()
    when, _, _ = A.inject_sensor(rows, 3000, Fixed(45.0))
    assert near(detect(rows, bundle), "sensor", when)


def test_long_gps_degradation_is_caught(bundle):
    rows = clean_mission()
    when, _, _ = A.inject_gps(rows, 1500, 120, Fixed(1.0))  # 3 satellites, hdop 8
    assert near(detect(rows, bundle), "gps", when)


def test_battery_cliff_is_caught(bundle):
    rows = clean_mission()
    when, _, _ = A.inject_battery(rows, 2000, 300, Fixed(15.0))
    assert near(detect(rows, bundle), "battery", when, within_s=90)


def test_low_lipo_is_caught(bundle):
    rows = clean_mission()
    when, _, _ = A.inject_lipo(rows, Fixed(35.0))
    assert near(detect(rows, bundle), "lipo", when, within_s=120)


def test_forest_alone_misses_a_spike_far_outside_normal(bundle):
    # The day-11 finding, kept as a test: Isolation Forest scores saturate beyond the
    # training range, so a 45 m spike is not more abnormal to it than a gusty window.
    rows = clean_mission()
    A.inject_sensor(rows, 3000, Fixed(45.0))
    assert detect(rows, bundle, mode="iforest") == []


def test_run_replaces_ml_alerts_and_keeps_rule_alerts(temp_db, bundle):
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

    rule_run(temp_db)
    M.run(temp_db, bundle)
    M.run(temp_db, bundle)  # second run must not double the ml alerts
    stored = sorted(tuple(r) for r in temp_db.execute("SELECT detector, anomaly_type FROM alerts"))
    assert stored == [("ml", "sensor"), ("rule", "sensor")]
