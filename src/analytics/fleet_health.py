"""
Fleet health (Phase 5, day 5): descriptive numbers per mission and per drone.

Reads only telemetry_logs, missions and drones, never the answer key.
Everything in src/analytics/ follows this rule (a test checks it); only the
evaluator in src/evaluation/ may read the ground truth.

Run:  python -m src.analytics.fleet_health        (uses DRONE_DB_PATH or data/drone.db)
"""
import logging
import sqlite3

from src.db.database import connect

log = logging.getLogger("fleet_health")

# One row per flight mission. First and last telemetry rows give battery used;
# the readings in between give the worst GPS and the biggest one-second jump.
MISSION_SUMMARY_SQL = """
WITH ordered AS (
    SELECT t.*,
           ROW_NUMBER() OVER (PARTITION BY t.mission_id ORDER BY t.timestamp)      AS first_n,
           ROW_NUMBER() OVER (PARTITION BY t.mission_id ORDER BY t.timestamp DESC) AS last_n,
           ABS(t.altitude_m - LAG(t.altitude_m) OVER (
               PARTITION BY t.mission_id ORDER BY t.timestamp))                    AS alt_step_m
    FROM telemetry_logs t
    JOIN missions m ON m.mission_id = t.mission_id
    WHERE m.role IN ('AM', 'PM')
)
SELECT
    o.mission_id,
    o.drone_id,
    m.role,
    COUNT(*)                                                         AS rows,
    ROUND(COUNT(*) / 60.0, 1)                                        AS duration_min,
    ROUND(MAX(CASE WHEN first_n = 1 THEN battery_vtol_pct END)
        - MAX(CASE WHEN last_n  = 1 THEN battery_vtol_pct END), 1)   AS vtol_used_pct,
    ROUND(MAX(CASE WHEN first_n = 1 THEN battery_cruise_pct END)
        - MAX(CASE WHEN last_n  = 1 THEN battery_cruise_pct END), 1) AS cruise_used_pct,
    MAX(CASE WHEN last_n = 1 THEN battery_vtol_pct END)              AS vtol_at_touchdown_pct,
    MAX(CASE WHEN last_n = 1 THEN battery_cruise_pct END)            AS cruise_at_touchdown_pct,
    MIN(satellite_count)                                             AS min_satellites,
    MAX(hdop)                                                        AS max_hdop,
    ROUND(MAX(alt_step_m), 1)                                        AS max_alt_step_m
FROM ordered o
JOIN missions m ON m.mission_id = o.mission_id
GROUP BY o.mission_id
ORDER BY o.mission_id
"""

# One row per drone, built on the mission summary.
FLEET_HEALTH_SQL = f"""
WITH ms AS ({MISSION_SUMMARY_SQL})
SELECT
    d.drone_id,
    COUNT(ms.mission_id)                                  AS flights,
    ROUND(COALESCE(SUM(ms.duration_min), 0) / 60.0, 1)    AS flight_hours,
    (SELECT COUNT(*) FROM missions s
      WHERE s.drone_id = d.drone_id AND s.role = 'standby') AS standby_days,
    ROUND(AVG(ms.vtol_used_pct), 1)                       AS avg_vtol_used_pct,
    ROUND(AVG(ms.cruise_used_pct), 1)                     AS avg_cruise_used_pct,
    MIN(ms.vtol_at_touchdown_pct)                         AS lowest_vtol_at_touchdown_pct,
    MIN(ms.cruise_at_touchdown_pct)                       AS lowest_cruise_at_touchdown_pct,
    MIN(ms.min_satellites)                                AS fewest_satellites,
    MAX(ms.max_hdop)                                      AS worst_hdop,
    MAX(ms.max_alt_step_m)                                AS biggest_alt_step_m
FROM drones d
LEFT JOIN ms ON ms.drone_id = d.drone_id
GROUP BY d.drone_id
ORDER BY d.drone_id
"""


def mission_summary(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(MISSION_SUMMARY_SQL)]


def fleet_health(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(FLEET_HEALTH_SQL)]


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no data)")
        return
    cols = list(rows[0].keys())
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = connect()
    try:
        health = fleet_health(conn)
    finally:
        conn.close()
    if not health or all(h["flights"] == 0 for h in health):
        log.warning("no flight telemetry found. Load data first (see README).")
    print("\nFleet health per drone")
    _print_table(health)


if __name__ == "__main__":
    main()
