"""
Rule-based detector v3: rules that look at the SHAPE of a signal, not only its size,
so normal disturbances (wind gusts, GPS multipath) are not mistaken for faults.

v3 (after the SITL flights): the battery rule learns the normal drain from the flight
itself (flight_drain_per_s) instead of a fixed 0.0136 %/s, and compares only windows
that lie inside cruise. v2 is v3 with the fixed drain and no cruise check.

Reads telemetry_logs and missions only. Writes rows to alerts with
detector = 'rule'. Running it again replaces the previous rule alerts.
This is post-flight analysis, so a rule may look a few minutes ahead.

Rules (all thresholds in RuleConfig):
  battery  the cruise battery level drops and STAYS down.
           Gust sag only pushes the reading down for a while, so the highest
           reading in a window ignores it. Compare the highest reading in a
           window before with the highest reading in a window after; a drop
           larger than this flight's normal drain + battery_drop_pct is a fault.
  gps      satellite_count < min_satellites or hdop > max_hdop, held for at
           least gps_min_s seconds (multipath under canopy lasts 5-25 s).
  sensor   one altitude reading far from its neighbours, measured against how
           much the neighbours themselves wobble (gusts make them wobble).
  lipo     VTOL battery at the start of the return leg below
           min_vtol_return_pct = landing cost (~40 %) + a reserve.

v1 (fixed size thresholds) scored precision 78 % / recall 50 % on dev.

Run:  python -m src.analytics.rule_detector     (uses DRONE_DB_PATH or data/drone.db)
"""
import logging
import sqlite3
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from src.db.database import connect, init_db

log = logging.getLogger("rule_detector")

TS = "%Y-%m-%d %H:%M:%S"
CRUISE_MODES = ("transit_out", "survey", "transit_back")


@dataclass(frozen=True)
class RuleConfig:
    # battery: persistent drop beyond normal drain
    battery_drop_pct: float = 5.0
    battery_window_s: int = 180   # before / after windows; longer than the longest gust (120 s)
    battery_gap_s: int = 60       # gap between each window and the moment checked
    battery_step_s: int = 5       # check every N seconds (speed)
    # gps: bad quality that lasts
    min_satellites: int = 6
    max_hdop: float = 2.5
    gps_min_s: int = 45
    # sensor: isolated altitude outlier
    spike_score: float = 14.0     # distance from neighbours / their wobble
    spike_min_m: float = 3.0      # and at least this many metres
    # lipo: not enough VTOL battery to land with a reserve
    min_vtol_return_pct: float = 52.0
    # flagged readings closer than this become one alert
    merge_gap_s: int = 5


@dataclass
class Alert:
    drone_id: str
    mission_id: str
    anomaly_type: str
    start_time: str
    end_time: str
    score: float  # size of the problem in the rule's own unit


def _ts(s: str) -> datetime:
    return datetime.strptime(s, TS)


def _group(flags: list[tuple[str, float]], gap_s: int) -> list[tuple[str, str, float]]:
    """Merge flagged (timestamp, score) readings into (start, end, worst score) runs."""
    runs: list[tuple[str, str, float]] = []
    for ts, score in flags:
        if runs and _ts(ts) - _ts(runs[-1][1]) <= timedelta(seconds=gap_s):
            start, _, worst = runs[-1]
            runs[-1] = (start, ts, max(worst, score))
        else:
            runs.append((ts, ts, score))
    return runs


def flight_drain_per_s(rows, lag_s: int = 60) -> float | None:
    """This flight's normal cruise drain (% per second): the median drop over `lag_s` seconds.

    A fault is a step that stays down, so it moves only the few lag pairs that straddle it;
    the median of all pairs is the drain the flight shows the rest of the time. Measured per
    flight, it fits any pack and airframe: the simulator drains 0.0136 %/s, the SITL QuadPlane
    with ArduPilot's default 3300 mAh pack about 0.13 %/s (v2 had the simulator's value fixed in
    the code, and alarmed on the first long SITL flight).
    """
    c = [r["battery_cruise_pct"] for r in rows]
    cruise = [r["flight_mode"] in CRUISE_MODES for r in rows]
    # an empty pack reads 0 from then on: those flat pairs would make the drain look like 0
    pairs = [(c[j] - c[j + lag_s]) / lag_s for j in range(len(c) - lag_s)
             if cruise[j] and cruise[j + lag_s] and c[j + lag_s] > 0]
    return max(0.0, statistics.median(pairs)) if pairs else None


def battery_check_points(rows, cfg: RuleConfig) -> list[int]:
    """Row positions the battery rule compares at: both windows inside cruise (takeoff and
    landing drain at another, VTOL, rate). Empty when the flight has under 2 x (window + gap)
    seconds of cruise: then the battery rule has nothing to say, which is not the same as 'no fault'."""
    w, g = cfg.battery_window_s, cfg.battery_gap_s
    cruise = [r["flight_mode"] in CRUISE_MODES for r in rows]
    return [i for i in range(w + g, len(rows) - g - w, cfg.battery_step_s)
            if cruise[i - g - w] and cruise[i + g + w - 1] and cruise[i]]


def _battery_flags(rows, cfg: RuleConfig) -> list[tuple[str, float]]:
    c = [r["battery_cruise_pct"] for r in rows]
    w, g = cfg.battery_window_s, cfg.battery_gap_s
    drain = flight_drain_per_s(rows)
    if drain is None:
        return []
    expected = drain * (w + 2 * g)  # normal drain between the two window starts
    flags = []
    for i in battery_check_points(rows, cfg):
        before = max(c[i - g - w:i - g])
        after = max(c[i + g:i + g + w])
        excess = (before - after) - expected
        if excess > cfg.battery_drop_pct:
            flags.append((rows[i]["timestamp"], excess))
    return flags


def _sensor_flags(rows, cfg: RuleConfig) -> list[tuple[str, float]]:
    a = [r["altitude_m"] for r in rows]
    flags = []
    for i in range(10, len(a) - 10):
        if rows[i]["flight_mode"] not in CRUISE_MODES:
            continue
        near = a[i - 3:i] + a[i + 1:i + 4]
        dev = abs(a[i] - statistics.median(near))
        if dev < cfg.spike_min_m:
            continue
        around = a[i - 10:i] + a[i + 1:i + 11]
        mid = statistics.median(around)
        wobble = max(statistics.median([abs(x - mid) for x in around]), 0.3)
        if dev / wobble > cfg.spike_score:
            flags.append((rows[i]["timestamp"], dev))
    return flags


def detect_mission(rows, cfg: RuleConfig) -> list[Alert]:
    """Apply all four rules to one flight mission (rows ordered by time, 1 Hz)."""
    if not rows:
        return []
    drone, mission = rows[0]["drone_id"], rows[0]["mission_id"]
    alerts: list[Alert] = []

    def add(kind: str, flags: list[tuple[str, float]], min_len_s: int = 0) -> None:
        for start, end, score in _group(flags, cfg.merge_gap_s):
            if (_ts(end) - _ts(start)).total_seconds() + 1 >= min_len_s:
                alerts.append(Alert(drone, mission, kind, start, end, round(score, 2)))

    add("battery", _battery_flags(rows, cfg))
    add("gps", [
        (r["timestamp"], float(max(cfg.min_satellites - r["satellite_count"], r["hdop"] - cfg.max_hdop)))
        for r in rows
        if r["satellite_count"] < cfg.min_satellites or r["hdop"] > cfg.max_hdop
    ], min_len_s=cfg.gps_min_s)
    add("sensor", _sensor_flags(rows, cfg))

    back = next((r for r in rows if r["flight_mode"] == "transit_back"), None)
    # no VTOL pack reported (e.g. SITL): nothing to check
    if back is not None and back["battery_vtol_pct"] is not None \
            and back["battery_vtol_pct"] < cfg.min_vtol_return_pct:
        alerts.append(Alert(drone, mission, "lipo", back["timestamp"], rows[-1]["timestamp"],
                            round(cfg.min_vtol_return_pct - back["battery_vtol_pct"], 2)))
    return alerts


def run(conn: sqlite3.Connection, cfg: RuleConfig = RuleConfig()) -> list[Alert]:
    """Detect on every flight mission and replace the stored rule alerts."""
    init_db(conn)
    missions = [r["mission_id"] for r in conn.execute(
        "SELECT mission_id FROM missions WHERE role IN ('AM', 'PM') ORDER BY mission_id")]
    alerts: list[Alert] = []
    for mid in missions:
        rows = conn.execute(
            "SELECT * FROM telemetry_logs WHERE mission_id = ? ORDER BY timestamp", (mid,)).fetchall()
        alerts.extend(detect_mission(rows, cfg))

    try:
        conn.execute("DELETE FROM alerts WHERE detector = 'rule'")
        conn.executemany(
            "INSERT INTO alerts (detector, drone_id, mission_id, anomaly_type, start_time, end_time, score) "
            "VALUES ('rule', :drone_id, :mission_id, :anomaly_type, :start_time, :end_time, :score)",
            [asdict(a) for a in alerts],
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        log.exception("writing alerts failed; previous rule alerts kept")
        raise

    log.info("missions=%d alerts=%d %s config=%s",
             len(missions), len(alerts), dict(Counter(a.anomaly_type for a in alerts)), asdict(cfg))
    return alerts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = connect()
    try:
        alerts = run(conn)
    finally:
        conn.close()
    for a in alerts:
        print(f"{a.mission_id}  {a.anomaly_type:<7}  {a.start_time} -> {a.end_time[11:]}  score={a.score}")


if __name__ == "__main__":
    main()
