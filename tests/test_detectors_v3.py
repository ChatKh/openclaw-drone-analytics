"""
Detector v3: the two changes the SITL flights asked for (sitl/README.md, findings 4 and 5).

battery rule  learns the normal drain from the flight itself, compares only windows inside cruise
ML envelope   one-sided: a flight steadier than any simulated one is not a fault
"""
from datetime import datetime

import joblib
import pytest

from src.analytics import ml_detector as M
from src.analytics.rule_detector import CRUISE_MODES, RuleConfig, detect_mission, flight_drain_per_s
from src.pipeline import Config, run
from src.simulator import anomaly_inject as A
from tests.test_rule_detector import Fixed, clean_mission

CFG = RuleConfig()
SIM_DRAIN = 0.0136          # the simulator's cruise drain, % per second


def as_rows(rows):
    return [{**r, "timestamp": str(r["timestamp"])} for r in rows]


def sitl_like(rows, factor=7, cruise_s=700):
    """A short flight whose pack drains `factor` times faster in cruise, like the SITL QuadPlane's
    (~0.1 %/s): take-off, the first `cruise_s` seconds of cruise, landing. The pack stays above empty."""
    cruise = [r for r in rows if r["flight_mode"] in CRUISE_MODES][:cruise_s]
    rows = [r for r in rows if r["flight_mode"] == "takeoff"] + cruise + \
        [r for r in rows if r["flight_mode"] == "landing"]
    out, level, prev = [], 100.0, None
    for r in rows:
        if prev is not None and r["flight_mode"] in CRUISE_MODES:
            # keep the flight's own changes (normal drain, an injected drop) and add the extra drain
            level += (r["battery_cruise_pct"] - prev) - SIM_DRAIN * (factor - 1)
        prev = r["battery_cruise_pct"]
        out.append({**r, "battery_cruise_pct": round(level, 2)})
    assert min(r["battery_cruise_pct"] for r in out) > 0
    return out


# ---------- battery rule ----------

def test_learned_drain_is_the_simulators_on_a_normal_flight():
    assert flight_drain_per_s(as_rows(clean_mission())) == pytest.approx(SIM_DRAIN, rel=0.05)


def test_a_fault_does_not_move_the_learned_drain():
    rows = clean_mission()
    A.inject_battery(rows, 2000, 300, Fixed(20.0))
    assert flight_drain_per_s(as_rows(rows)) == pytest.approx(SIM_DRAIN, rel=0.05)


def test_fast_draining_pack_is_not_a_fault():
    rows = as_rows(sitl_like(clean_mission()))
    assert flight_drain_per_s(rows) == pytest.approx(7 * SIM_DRAIN, rel=0.05)
    assert [a for a in detect_mission(rows, CFG) if a.anomaly_type == "battery"] == []


def test_v2s_fixed_drain_would_have_alarmed_on_it():
    rows = as_rows(sitl_like(clean_mission()))
    drop_in_300_s = rows[300]["battery_cruise_pct"] - rows[600]["battery_cruise_pct"]
    assert drop_in_300_s - SIM_DRAIN * 300 > CFG.battery_drop_pct     # v2: expected 4.1 %, saw ~29 %


def test_a_drop_is_still_caught_on_a_fast_draining_pack():
    rows = clean_mission()
    first_cruise = next(i for i, r in enumerate(rows) if r["flight_mode"] in CRUISE_MODES)
    start, _, _ = A.inject_battery(rows, first_cruise + 350, 300, Fixed(8.0))
    rows = as_rows(sitl_like(rows))
    alerts = [a for a in detect_mission(rows, CFG) if a.anomaly_type == "battery"]
    assert len(alerts) == 1
    assert abs((datetime.fromisoformat(alerts[0].start_time) - start).total_seconds()) <= 60


def test_an_empty_pack_does_not_hide_the_drain():
    rows = as_rows(sitl_like(clean_mission()))
    cruise = [i for i, r in enumerate(rows) if r["flight_mode"] in CRUISE_MODES]
    for i in cruise[500:]:
        rows[i]["battery_cruise_pct"] = 0.0                                # flat at empty
    assert flight_drain_per_s(rows) == pytest.approx(7 * SIM_DRAIN, rel=0.1)


def test_windows_that_reach_into_takeoff_are_not_compared():
    # v2's false alarm on the long SITL flight came from a 'before' window holding the VTOL climb
    rows = as_rows(clean_mission())
    first_cruise = next(i for i, r in enumerate(rows) if r["flight_mode"] in CRUISE_MODES)
    for r in rows[:first_cruise]:
        r["battery_cruise_pct"] = 100.0
    rows[first_cruise]["battery_cruise_pct"] = 100.0
    steep = [dict(r) for r in rows]                       # cruise starts 30 % lower than takeoff
    for r in steep[first_cruise:]:
        r["battery_cruise_pct"] = round(r["battery_cruise_pct"] - 30.0, 2)
    assert [a for a in detect_mission(steep, CFG) if a.anomaly_type == "battery"] == []


def test_too_little_cruise_means_no_battery_check():
    rows = as_rows(clean_mission())
    short = [r for r in rows if r["flight_mode"] not in CRUISE_MODES] [:200] + \
        [r for r in rows if r["flight_mode"] in CRUISE_MODES][:400]      # < 2 x (180 + 60) s
    assert flight_drain_per_s(short) is not None
    assert [a for a in detect_mission(short, CFG) if a.anomaly_type == "battery"] == []


# ---------- ML envelope ----------

@pytest.fixture(scope="module")
def bundle():
    return M.train(n_flights=30, seed=9000, n_calib=20, calib_seed=9100)


def steady(rows):
    """The flight as SITL flies it without wind: speed and altitude dead steady in cruise."""
    out = []
    for r in rows:
        if r["flight_mode"] in CRUISE_MODES:
            r = {**r, "ground_speed_mps": 18.0 if r["flight_mode"] != "survey" else 10.0, "altitude_m": 20.0}
        out.append(r)
    return out


def test_one_sided_envelope_ignores_a_steadier_than_normal_flight(bundle):
    assert M.detect_mission(as_rows(steady(clean_mission())), bundle) == []


def test_the_two_sided_envelope_did_flag_it(bundle):
    two_sided = {k: v for k, v in bundle.items() if k != "sides"}       # what v2 did
    alerts = M.detect_mission(as_rows(steady(clean_mission())), two_sided)
    assert alerts and {a.anomaly_type for a in alerts} == {"sensor"}


def test_every_feature_has_a_fault_side():
    assert set(M.SIDES) == set(M.FEATURES)
    assert set(M.SIDES.values()) <= {"high", "low"}
    assert M.SIDES["sats_min"] == "low" and M.SIDES["vtol_pct"] == "low" and M.SIDES["speed_std"] == "high"


def test_a_spike_is_still_caught_on_a_steady_flight(bundle):
    rows = steady(clean_mission())
    spike_at = next(i for i, r in enumerate(rows) if r["flight_mode"] == "survey") + 600
    rows[spike_at] = {**rows[spike_at], "altitude_m": 45.0}
    alerts = M.detect_mission(as_rows(rows), bundle)
    assert [a for a in alerts if a.anomaly_type == "sensor"]


def test_bundle_records_its_version_and_sides(bundle):
    assert bundle["version"] == M.MODEL_VERSION == 3 and bundle["sides"] == M.SIDES


def test_pipeline_retrains_a_v2_model(tmp_path):
    old = M.train(n_flights=5, seed=9000, n_calib=5, calib_seed=9100)
    del old["sides"], old["version"]                                  # as v2 saved it
    path = tmp_path / "iforest.joblib"
    joblib.dump(old, path)
    run(Config(days=1, seed=42, data_dir=tmp_path / "d", db=tmp_path / "x.db", fresh=True,
               model_path=path, ml_flights=5, ml_calib_flights=5))
    assert joblib.load(path)["version"] == M.MODEL_VERSION
