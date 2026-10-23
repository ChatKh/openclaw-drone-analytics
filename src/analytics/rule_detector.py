"""
Rule-based detector (Phase 5, day 6): fixed thresholds on telemetry -> alerts.

Reads telemetry_logs and missions only. Writes rows to alerts with
detector = 'rule'. Running it again replaces the previous rule alerts.

Rules (thresholds live in RuleConfig so day 9 can tune them):
  battery  cruise battery falls more than battery_drop_pct within battery_window_s
  gps      satellite_count < min_satellites or hdop > max_hdop, for at least gps_min_s
  sensor   altitude changes more than max_alt_step_m between two readings
  lipo     VTOL battery below min_vtol_return_pct when the return leg starts

Run:  python -m src.analytics.rule_detector     (uses DRONE_DB_PATH or data/drone.db)
"""
import logging
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from src.db.database import connect, init_db

log = logging.getLogger("rule_detector")

TS = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class RuleConfig:
    battery_drop_pct: float = 15.0
    battery_window_s: int = 60
    min_satellites: int = 6
    max_hdop: float = 5.0
    gps_min_s: int = 1
    max_alt_step_m: float = 10.0
    min_vtol_return_pct: float = 45.0
    merge_gap_s: int = 5  # flagged readings closer than this become one alert


@dataclass
class Alert:
    drone_id: str
    mission_id: str
    anomaly_type: str
    start_time: str
    end_time: str
    score: float  # size of the problem in the rule's own unit (%, satellites, m)


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


def detect_mission(rows: list[sqlite3.Row], cfg: RuleConfig) -> list[Alert]:
    """Apply all four rules to one flight mission (rows ordered by time, 1 Hz)."""
    if not rows:
        return []
    drone, mission = rows[0]["drone_id"], rows[0]["mission_id"]
    alerts: list[Alert] = []

    def add(kind: str, flags: list[tuple[str, float]], min_len_s: int = 0) -> None:
        for start, end, score in _group(flags, cfg.merge_gap_s):
            if (_ts(end) - _ts(start)).total_seconds() + 1 >= min_len_s:
                alerts.append(Alert(drone, mission, kind, start, end, round(score, 2)))

    # battery: compare with the reading battery_window_s seconds earlier
    w = cfg.battery_window_s
    add("battery", [
        (rows[i]["timestamp"], rows[i - w]["battery_cruise_pct"] - rows[i]["battery_cruise_pct"])
        for i in range(w, len(rows))
        if rows[i - w]["battery_cruise_pct"] - rows[i]["battery_cruise_pct"] > cfg.battery_drop_pct
    ])

    # gps: bad quality held for at least gps_min_s seconds
    add("gps", [
        (r["timestamp"], float(cfg.min_satellites - r["satellite_count"]))
        for r in rows
        if r["satellite_count"] < cfg.min_satellites or r["hdop"] > cfg.max_hdop
    ], min_len_s=cfg.gps_min_s)

    # sensor: altitude jump between two readings
    add("sensor", [
        (rows[i]["timestamp"], abs(rows[i]["altitude_m"] - rows[i - 1]["altitude_m"]))
        for i in range(1, len(rows))
        if abs(rows[i]["altitude_m"] - rows[i - 1]["altitude_m"]) > cfg.max_alt_step_m
    ])

    # lipo: VTOL level at the first reading of the return leg
    back = next((r for r in rows if r["flight_mode"] == "transit_back"), None)
    if back is not None and back["battery_vtol_pct"] < cfg.min_vtol_return_pct:
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
