"""
Data for the dashboard (Phase 6). Every function takes a READ-ONLY connection.

Rules
- read only: the dashboard opens SQLite with mode=ro, so a bug cannot change data
- aggregate in SQL before Streamlit sees it: 163k telemetry rows never reach the
  browser; a mission chart gets one point per BUCKET_S seconds
- an empty or missing database is a normal state, not a crash
- the answer key is read only on the evaluation page, through src.evaluation
"""
import sqlite3
from pathlib import Path

import pandas as pd

from src.analytics.fleet_health import FLEET_HEALTH_SQL
from src.evaluation.evaluator import TYPES, evaluate

BUCKET_S = 10
MATCH_TOL_S = 30  # two detectors "agree" when their alerts overlap within this, same as the evaluator
PHASES = ["takeoff", "transit_out", "survey", "transit_back", "landing"]


class NoDatabase(Exception):
    """The database file does not exist yet."""


def connect_ro(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        raise NoDatabase(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def counts(conn) -> dict[str, int]:
    """Row counts per table; 0 when the table is missing."""
    out = {}
    for t in ("drones", "missions", "telemetry_logs", "alerts", "injected_anomalies"):
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if _table_exists(conn, t) else 0
    return out


def _df(conn, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


# ---------- overview ----------

def overview(conn) -> dict:
    row = conn.execute("""
        SELECT COUNT(DISTINCT m.mission_id) AS flights,
               ROUND(COUNT(t.timestamp) / 3600.0, 1) AS flight_hours,
               MIN(m.start_time) AS first_day, MAX(m.start_time) AS last_day
        FROM missions m LEFT JOIN telemetry_logs t ON t.mission_id = m.mission_id
        WHERE m.role IN ('AM', 'PM')""").fetchone()
    alerts = dict(conn.execute("SELECT detector, COUNT(*) FROM alerts GROUP BY detector").fetchall())
    return {**dict(row), "rule_alerts": alerts.get("rule", 0), "ml_alerts": alerts.get("ml", 0)}


def fleet_table(conn) -> pd.DataFrame:
    return _df(conn, FLEET_HEALTH_SQL)


def alerts_per_drone(conn) -> pd.DataFrame:
    return _df(conn, """
        SELECT drone_id, detector, anomaly_type, COUNT(*) AS alerts
        FROM alerts GROUP BY drone_id, detector, anomaly_type ORDER BY drone_id""")


# ---------- missions ----------

def mission_list(conn) -> pd.DataFrame:
    return _df(conn, """
        SELECT m.mission_id, m.drone_id, m.role, m.start_time,
               SUM(a.detector = 'rule') AS rule_alerts, SUM(a.detector = 'ml') AS ml_alerts
        FROM missions m LEFT JOIN alerts a ON a.mission_id = m.mission_id
        WHERE m.role IN ('AM', 'PM')
        GROUP BY m.mission_id ORDER BY m.mission_id""").fillna({"rule_alerts": 0, "ml_alerts": 0})


def mission_telemetry(conn, mission_id: str, bucket_s: int = BUCKET_S) -> pd.DataFrame:
    """One row per bucket. MAX altitude and worst GPS keep short spikes visible after averaging."""
    df = _df(conn, """
        SELECT CAST(strftime('%s', timestamp) AS INTEGER) / ? * ? AS bucket,
               MIN(timestamp)             AS time,
               MAX(altitude_m)            AS altitude_max_m,
               AVG(altitude_m)            AS altitude_avg_m,
               MIN(battery_cruise_pct)    AS battery_cruise_pct,
               MIN(battery_vtol_pct)      AS battery_vtol_pct,
               MAX(hdop)                  AS hdop_max,
               MIN(satellite_count)       AS satellites_min,
               AVG(ground_speed_mps)      AS speed_avg_mps,
               MIN(flight_mode)           AS flight_mode
        FROM telemetry_logs WHERE mission_id = ?
        GROUP BY bucket ORDER BY bucket""", (bucket_s, bucket_s, mission_id))
    df["time"] = pd.to_datetime(df["time"])
    return df.drop(columns="bucket")


def mission_alerts(conn, mission_id: str) -> pd.DataFrame:
    df = _df(conn, """
        SELECT detector, anomaly_type, start_time, end_time, score
        FROM alerts WHERE mission_id = ? ORDER BY start_time""", (mission_id,))
    for c in ("start_time", "end_time"):
        df[c] = pd.to_datetime(df[c])
    return df


# ---------- alerts ----------

def alerts_with_agreement(conn, tol_s: int = MATCH_TOL_S) -> pd.DataFrame:
    """All alerts, each marked with whether the OTHER detector raised an overlapping alert of the same type."""
    df = _df(conn, "SELECT detector, drone_id, mission_id, anomaly_type, start_time, end_time, score FROM alerts")
    if df.empty:
        return df.assign(other_detector_agrees=pd.Series(dtype=bool))
    s, e = pd.to_datetime(df["start_time"]), pd.to_datetime(df["end_time"])
    tol = pd.Timedelta(seconds=tol_s)
    agrees = []
    for i, a in df.iterrows():
        other = (df["detector"] != a["detector"]) & (df["mission_id"] == a["mission_id"]) \
            & (df["anomaly_type"] == a["anomaly_type"]) & (s - tol <= e[i]) & (s[i] - tol <= e)
        agrees.append(bool(other.any()))
    return df.assign(other_detector_agrees=agrees).sort_values(["start_time", "detector"]).reset_index(drop=True)


# ---------- energy ----------

ENERGY_SQL = """
WITH ph AS (
    SELECT t.mission_id, t.drone_id, t.flight_mode, t.battery_vtol_pct, t.battery_cruise_pct,
           ROW_NUMBER() OVER (PARTITION BY t.mission_id, t.flight_mode ORDER BY t.timestamp)      AS first_n,
           ROW_NUMBER() OVER (PARTITION BY t.mission_id, t.flight_mode ORDER BY t.timestamp DESC) AS last_n
    FROM telemetry_logs t JOIN missions m ON m.mission_id = t.mission_id
    WHERE m.role IN ('AM', 'PM')
)
SELECT mission_id, drone_id, flight_mode,
       COUNT(*) / 60.0 AS minutes,
       MAX(CASE WHEN first_n = 1 THEN battery_vtol_pct END)
         - MAX(CASE WHEN last_n = 1 THEN battery_vtol_pct END)   AS vtol_used_pct,
       MAX(CASE WHEN first_n = 1 THEN battery_cruise_pct END)
         - MAX(CASE WHEN last_n = 1 THEN battery_cruise_pct END) AS cruise_used_pct
FROM ph GROUP BY mission_id, flight_mode
"""


def energy_by_phase(conn) -> pd.DataFrame:
    """Average battery used per flight phase and drone: the VTOL motors work only in takeoff and landing."""
    per_mission = _df(conn, ENERGY_SQL)
    if per_mission.empty:
        return per_mission
    out = (per_mission.groupby(["drone_id", "flight_mode"], as_index=False)
           .agg(flights=("mission_id", "nunique"), minutes=("minutes", "mean"),
                vtol_used_pct=("vtol_used_pct", "mean"), cruise_used_pct=("cruise_used_pct", "mean")))
    out["vtol_pct_per_min"] = out["vtol_used_pct"] / out["minutes"]
    out["cruise_pct_per_min"] = out["cruise_used_pct"] / out["minutes"]
    out["flight_mode"] = pd.Categorical(out["flight_mode"], PHASES, ordered=True)
    return out.sort_values(["drone_id", "flight_mode"]).round(2).reset_index(drop=True)


# ---------- evaluation (the only place the answer key is read) ----------

def evaluation(conn) -> pd.DataFrame:
    """Precision / recall per detector and type, from the stored alerts. Empty if there is no answer key."""
    if not counts(conn)["injected_anomalies"]:
        return pd.DataFrame()
    rows = []
    for det in ("rule", "ml"):
        if not conn.execute("SELECT 1 FROM alerts WHERE detector = ? LIMIT 1", (det,)).fetchone():
            continue  # that detector has not been run on this database
        rep = evaluate(conn, det)
        for r in [*(rep.by_type[t] for t in TYPES), rep.total]:
            rows.append({"detector": det, "type": r.anomaly_type, "injected": r.injected, "caught": r.detected,
                         "alerts": r.alerts, "false_alarms": r.false_alarms,
                         "precision": r.precision, "recall": r.recall})
    return pd.DataFrame(rows)
