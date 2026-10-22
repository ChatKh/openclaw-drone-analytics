"""Tests for anomaly injection with variable severity (Phase 5 prep)."""
import json
import random

import pytest

from src.simulator import anomaly_inject as A
from src.simulator.fleet import generate_all
from src.simulator.simulator import generate_mission
from datetime import datetime


@pytest.fixture(scope="module")
def dev_set():
    return generate_all(num_days=15, seed=42)


def mission():
    return generate_mission("A", "D0001_A_AM", datetime(2026, 6, 1, 8))


def test_same_seed_gives_identical_data(dev_set):
    again = generate_all(num_days=15, seed=42)
    assert again[1] == dev_set[1]          # same answer key
    assert again[0][5000] == dev_set[0][5000]


def test_magnitudes_stay_in_their_ranges(dev_set):
    for rec in dev_set[1]:
        p = json.loads(rec["params"])
        t = rec["anomaly_type"]
        if t == "battery":
            assert A.BATTERY_DROP_PCT[0] <= p["drop_pct"] <= A.BATTERY_DROP_PCT[1]
        elif t == "gps":
            assert 3 <= p["satellite_count"] <= 7 and 2.0 <= p["hdop"] <= 8.0
        elif t == "sensor":
            assert A.SPIKE_ALTITUDE_M[0] <= p["value"] <= A.SPIKE_ALTITUDE_M[1]
        else:
            assert A.LIPO_LEVEL_PCT[0] <= p["vtol_level"] <= A.LIPO_LEVEL_PCT[1]


def test_some_anomalies_are_below_the_detection_thresholds(dev_set):
    # The point of variable severity: not every case is easy.
    marginal = 0
    for rec in dev_set[1]:
        p = json.loads(rec["params"])
        t = rec["anomaly_type"]
        marginal += (
            (t == "battery" and p["drop_pct"] <= 15)
            or (t == "gps" and p["satellite_count"] >= 6 and p["hdop"] <= 5)
            or (t == "sensor" and p["value"] < 31)
            or (t == "lipo" and p["vtol_level"] >= 45)
        )
    assert 0 < marginal < len(dev_set[1])


def test_gps_severity_moves_both_signals_together():
    rows = mission()
    _, _, p = A.inject_gps(rows, 1000, 60, random.Random(1))
    assert rows[1000]["satellite_count"] == p["satellite_count"]
    assert rows[1000]["hdop"] == p["hdop"]
    assert rows[1060]["hdop"] < 2.0  # recovered after the 60 s window (injected hdop is >= 2.0)


def test_low_lipo_runs_out_before_touchdown():
    rows = mission()
    normal_end = rows[-1]["battery_vtol_pct"]

    class Fixed(random.Random):
        def uniform(self, a, b):
            return 30.0

    _, _, p = A.inject_lipo(rows, Fixed())
    assert p["vtol_level"] == 30.0
    assert p["vtol_at_touchdown"] == 0.0 < normal_end
    landing = [r["battery_vtol_pct"] for r in rows if r["flight_mode"] == "landing"]
    assert landing[0] > landing[-1]  # still drains during landing, not a flat line
