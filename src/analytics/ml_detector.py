"""
ML detector (Phase 5, days 11-12): learned from normal flights only.

The point is a fair comparison with the rule-based detector:
- the model gets plain window statistics, not the domain logic built into the rules
- it learns only from NORMAL simulated flights (calm, windy, multipath), never faults
- thresholds are set on a second batch of normal flights (calibration), so the
  answer key, the dev set and the test set are never used to set them

Two parts, both learned from the same normal windows:
  iforest   Isolation Forest on all 9 features at once (catches odd combinations)
  envelope  per feature, the range seen in normal flights, widened by a margin
            (catches one feature far outside anything normal)

Why both (finding from the dev set, day 11): Isolation Forest alone caught 0 of 14.
Its score saturates: a value 100x beyond the training range is isolated about as
fast as the most extreme normal value, so a 23 m altitude spike scored 0.60 while
gusty normal windows scored 0.75+. The envelope measures HOW FAR outside normal a
value is, which is exactly what the forest cannot see.

Steps
  1. cut each flight's cruise into 30 s windows (every 10 s)
  2. per window: wobble and range of altitude, speed wobble, GPS quality,
     cruise battery slope and biggest one-second drop, VTOL level
  3. a window is abnormal when the forest score or any feature is beyond its
     calibrated limit (mode: iforest | envelope | hybrid)

Dev set (seed 42), thresholds from calibration flights only:
  iforest   precision 0 %  recall 0 %   1 false alarm
  envelope  precision 92 % recall 86 %  1 false alarm   <- default
  hybrid    precision 86 % recall 86 %  2 false alarms  (the forest adds only a false alarm)
  4. the feature furthest outside normal names the anomaly type
  5. abnormal windows close together become one alert, detector = 'ml'

Run:
  python -m src.analytics.ml_detector train                  # -> models/iforest.joblib
  python -m src.analytics.ml_detector detect [--mode envelope]  # writes ml alerts to DRONE_DB_PATH or data/drone.db
"""
import argparse
import logging
import random
import sqlite3
import statistics
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from src.analytics.rule_detector import Alert, _group
from src.db.database import connect, init_db

log = logging.getLogger("ml_detector")

MODEL_PATH = Path("models/iforest.joblib")
CRUISE_MODES = ("transit_out", "survey", "transit_back")
WINDOW_S, STEP_S = 30, 10
MODES = ("iforest", "envelope", "hybrid")
DEFAULT_MODE = "envelope"             # chosen on the dev set (day 11); the others stay for the comparison
MAX_FLAGGED_FLIGHTS = 0.01            # calibration target: at most 1 % of normal flights raise anything
MARGINS = (0.0, 0.1, 0.25, 0.5, 1.0)  # envelope widening tried, smallest that meets the target wins

# feature name -> anomaly type it points to
FEATURES = {
    "alt_std": "sensor",
    "alt_max_dev": "sensor",
    "speed_std": "sensor",
    "hdop_mean": "gps",
    "hdop_max": "gps",
    "sats_min": "gps",
    "cruise_slope": "battery",
    "cruise_max_step": "battery",
    "vtol_pct": "lipo",
}
TYPES = list(FEATURES.values())

# v3: which side of the normal range is a fault. Every fault we look for makes a feature
# worse in ONE direction (more wobble, worse HDOP, fewer satellites, faster drain, lower
# VTOL pack). Unusually steady flight is not a fault: the SITL QuadPlane (no wind, almost
# no sensor noise) flies steadier than any simulated flight, and a two-sided envelope
# raised 8 false alarms on it, all for speed_std BELOW normal.
SIDES = {
    "alt_std": "high", "alt_max_dev": "high", "speed_std": "high",
    "hdop_mean": "high", "hdop_max": "high", "sats_min": "low",
    "cruise_slope": "high", "cruise_max_step": "high", "vtol_pct": "low",
}
MODEL_VERSION = 3   # 2: two-sided envelope; 3: one-sided (SIDES)


def window_features(rows) -> list[tuple[str, str, list[float]]]:
    """(window start, window end, feature vector) for each cruise window of one flight."""
    cruise = [r for r in rows if r["flight_mode"] in CRUISE_MODES]
    out = []
    for s in range(0, len(cruise) - WINDOW_S + 1, STEP_S):
        w = cruise[s:s + WINDOW_S]
        alt = [r["altitude_m"] for r in w]
        bat = [r["battery_cruise_pct"] for r in w]
        med = statistics.median(alt)
        out.append((str(w[0]["timestamp"]), str(w[-1]["timestamp"]), [
            statistics.pstdev(alt),
            max(abs(a - med) for a in alt),
            statistics.pstdev([r["ground_speed_mps"] for r in w]),
            statistics.fmean([r["hdop"] for r in w]),
            max(r["hdop"] for r in w),
            float(min(r["satellite_count"] for r in w)),
            (bat[0] - bat[-1]) / WINDOW_S,
            max(bat[i - 1] - bat[i] for i in range(1, len(bat))),
            float("nan") if w[0]["battery_vtol_pct"] is None else w[0]["battery_vtol_pct"],
        ]))
    return out


def normal_flights(n: int, seed: int):
    """Normal flights from the simulator: weather yes, faults no. Own seed, separate from dev/test."""
    from src.simulator.disturbance import apply_weather
    from src.simulator.simulator import generate_mission

    random.seed(seed)
    for k in range(n):
        rows = generate_mission("A", f"TRAIN_{k:04d}", datetime(2026, 1, 1, 8) + timedelta(days=k))
        apply_weather(rows, random.Random(f"train:{seed}:{k}"))
        yield rows


def _matrix(rows) -> np.ndarray:
    return np.array([f for _, _, f in window_features(rows)])


def _excess(X: np.ndarray, bundle: dict, margin: float) -> np.ndarray:
    """How far each feature is outside the normal envelope, in units of its normal spread (0 = inside)."""
    med, lo, hi = bundle["median"], bundle["low"], bundle["high"]
    up_span = np.maximum(hi - med, 1e-6)
    down_span = np.maximum(med - lo, 1e-6)
    above = (X - (hi + margin * up_span)) / up_span
    below = ((lo - margin * down_span) - X) / down_span
    sides = bundle.get("sides")                     # a v2 bundle has none: two-sided
    if sides is not None:
        above = np.where([sides[f] == "high" for f in FEATURES], above, 0.0)
        below = np.where([sides[f] == "low" for f in FEATURES], below, 0.0)
    return np.maximum(np.maximum(above, below), 0.0)


def train(n_flights: int = 100, seed: int = 9000, n_calib: int = 100, calib_seed: int = 9100) -> dict:
    """Fit on one batch of normal flights, set thresholds on a second batch."""
    X = np.array([f for rows in normal_flights(n_flights, seed) for _, _, f in window_features(rows)])
    model = IsolationForest(n_estimators=200, random_state=0).fit(X)
    bundle = {
        "model": model,
        "median": np.median(X, axis=0),
        "low": X.min(axis=0),
        "high": X.max(axis=0),
        "features": list(FEATURES),
        "sides": dict(SIDES),
        "version": MODEL_VERSION,
        "trained_on": {"flights": n_flights, "seed": seed, "windows": len(X)},
    }

    calib = [_matrix(rows) for rows in normal_flights(n_calib, calib_seed)]
    allowed = int(MAX_FLAGGED_FLIGHTS * n_calib)

    # forest: the per-flight worst score that only `allowed` calibration flights exceed
    worst = np.sort([(-model.score_samples(C)).max() for C in calib])
    bundle["if_threshold"] = float(worst[-(allowed + 1)])

    # envelope: smallest margin that keeps flagged calibration flights within `allowed`
    for m in MARGINS:
        flagged = sum(bool((_excess(C, bundle, m) > 0).any()) for C in calib)
        if flagged <= allowed:
            break
    bundle["margin"] = m
    bundle["calibrated_on"] = {"flights": n_calib, "seed": calib_seed, "max_flagged": MAX_FLAGGED_FLIGHTS}
    log.info("trained on %d normal flights (%d windows); if_threshold=%.4f margin=%.2f",
             n_flights, len(X), bundle["if_threshold"], m)
    return bundle


def detect_mission(rows, bundle: dict, mode: str = DEFAULT_MODE, merge_gap_s: int = 15) -> list[Alert]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not rows:
        return []
    feats = window_features(rows)
    if not feats:
        return []
    X = np.array([f for _, _, f in feats])
    # A feature the source does not report (NaN, e.g. no VTOL pack in SITL) is set to its normal
    # median: it then neither raises nor hides anything, and the other features still count.
    X = np.where(np.isnan(X), bundle["median"], X)
    forest_score = -bundle["model"].score_samples(X)
    forest = forest_score > bundle["if_threshold"]
    excess = _excess(X, bundle, bundle["margin"])
    outside = (excess > 0).any(axis=1)
    hit = {"iforest": forest, "envelope": outside, "hybrid": forest | outside}[mode]

    # distance from normal per feature, to name the type (envelope excess first, else spread from median)
    spread = (bundle["high"] - bundle["low"]) + 1e-6
    closeness = np.abs(X - bundle["median"]) / spread

    by_type: dict[str, list[tuple[str, float]]] = {}
    ends: dict[str, str] = {}
    for (start, end, _), flag, ex, cl, fs in zip(feats, hit, excess, closeness, forest_score):
        if not flag:
            continue
        if ex.max() > 0:   # score = how many normal spreads outside the envelope
            j, score = int(np.argmax(ex)), float(ex.max())
        else:              # only the forest flagged it: score = forest score
            j, score = int(np.argmax(cl)), float(fs)
        by_type.setdefault(TYPES[j], []).append((start, score))
        ends[start] = end

    alerts = []
    for kind, flags in by_type.items():
        for start, last_start, worst in _group(flags, merge_gap_s):
            alerts.append(Alert(rows[0]["drone_id"], rows[0]["mission_id"], kind,
                                start, ends[last_start], round(worst, 2)))
    return alerts


def run(conn: sqlite3.Connection, bundle: dict, mode: str = DEFAULT_MODE) -> list[Alert]:
    """Detect on every flight mission and replace the stored ml alerts."""
    init_db(conn)
    missions = [r["mission_id"] for r in conn.execute(
        "SELECT mission_id FROM missions WHERE role IN ('AM', 'PM') ORDER BY mission_id")]
    alerts: list[Alert] = []
    for mid in missions:
        rows = conn.execute(
            "SELECT * FROM telemetry_logs WHERE mission_id = ? ORDER BY timestamp", (mid,)).fetchall()
        alerts.extend(detect_mission(rows, bundle, mode))
    try:
        conn.execute("DELETE FROM alerts WHERE detector = 'ml'")
        conn.executemany(
            "INSERT INTO alerts (detector, drone_id, mission_id, anomaly_type, start_time, end_time, score) "
            "VALUES ('ml', :drone_id, :mission_id, :anomaly_type, :start_time, :end_time, :score)",
            [asdict(a) for a in alerts],
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        log.exception("writing alerts failed; previous ml alerts kept")
        raise
    log.info("mode=%s missions=%d alerts=%d %s", mode, len(missions), len(alerts),
             dict(Counter(a.anomaly_type for a in alerts)))
    return alerts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="ML detector learned from normal flights")
    ap.add_argument("command", choices=["train", "detect"])
    ap.add_argument("--flights", type=int, default=100)
    ap.add_argument("--mode", choices=MODES, default=DEFAULT_MODE)
    args = ap.parse_args()

    if args.command == "train":
        bundle = train(args.flights)
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(bundle, MODEL_PATH)
        log.info("saved %s", MODEL_PATH)
        return

    if not MODEL_PATH.exists():
        log.error("%s not found. Train first: python -m src.analytics.ml_detector train", MODEL_PATH)
        raise SystemExit(2)
    bundle = joblib.load(MODEL_PATH)
    if bundle.get("version") != MODEL_VERSION:
        log.error("%s is a version %s model; this code is version %d. Retrain: "
                  "python -m src.analytics.ml_detector train", MODEL_PATH, bundle.get("version", 2), MODEL_VERSION)
        raise SystemExit(2)
    conn = connect()
    try:
        alerts = run(conn, bundle, args.mode)
    finally:
        conn.close()
    for a in alerts:
        print(f"{a.mission_id}  {a.anomaly_type:<7}  {a.start_time} -> {a.end_time[11:]}  score={a.score}")


if __name__ == "__main__":
    main()
