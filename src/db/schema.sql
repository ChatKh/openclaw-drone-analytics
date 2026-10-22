-- OpenClaw Drone Analytics: database schema v1 (Phase 4)
-- Times are TEXT 'YYYY-MM-DD HH:MM:SS' (local mission time).
-- Deferred tables (designed, not created yet): coverage_points, maintenance_events.

-- Dimension: one row per drone
CREATE TABLE IF NOT EXISTS drones (
    drone_id             TEXT PRIMARY KEY CHECK (length(drone_id) = 1),
    model                TEXT NOT NULL,
    firmware             TEXT NOT NULL,
    battery_health_pct   REAL NOT NULL CHECK (battery_health_pct BETWEEN 0 AND 100),
    payload_type         TEXT NOT NULL,
    max_flight_time_min  INTEGER NOT NULL CHECK (max_flight_time_min > 0),
    status               TEXT NOT NULL
);

-- Dimension: one row per mission slot (flight or standby day).
-- No "has_injected_anomaly" here on purpose: that is ground truth and lives
-- only in injected_anomalies, so a detector reading missions cannot see it.
CREATE TABLE IF NOT EXISTS missions (
    mission_id            TEXT PRIMARY KEY,
    drone_id              TEXT NOT NULL REFERENCES drones(drone_id),
    role                  TEXT NOT NULL CHECK (role IN ('AM', 'PM', 'standby')),
    start_time            TEXT NOT NULL,
    end_time              TEXT NOT NULL,
    planned_duration_min  REAL NOT NULL,
    actual_duration_min   REAL NOT NULL,
    mission_status        TEXT NOT NULL,
    CHECK (end_time >= start_time)
);

-- Fact: telemetry, 1 Hz in flight. One drone cannot report twice in one second.
CREATE TABLE IF NOT EXISTS telemetry_logs (
    timestamp           TEXT NOT NULL,
    drone_id            TEXT NOT NULL REFERENCES drones(drone_id),
    mission_id          TEXT NOT NULL REFERENCES missions(mission_id),
    flight_mode         TEXT NOT NULL CHECK (flight_mode IN
                          ('rest', 'takeoff', 'transit_out', 'survey', 'transit_back', 'landing')),
    battery_vtol_pct    REAL NOT NULL CHECK (battery_vtol_pct BETWEEN 0 AND 100),
    battery_cruise_pct  REAL NOT NULL CHECK (battery_cruise_pct BETWEEN 0 AND 100),
    lat                 REAL NOT NULL,
    lon                 REAL NOT NULL,
    altitude_m          REAL NOT NULL,
    ground_speed_mps    REAL NOT NULL,
    satellite_count     INTEGER NOT NULL,
    hdop                REAL NOT NULL,
    PRIMARY KEY (drone_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_telemetry_mission ON telemetry_logs (mission_id, timestamp);

-- Ground truth: what was injected on purpose (the answer key).
CREATE TABLE IF NOT EXISTS injected_anomalies (
    anomaly_id    INTEGER PRIMARY KEY,
    drone_id      TEXT NOT NULL REFERENCES drones(drone_id),
    mission_id    TEXT NOT NULL REFERENCES missions(mission_id),
    anomaly_type  TEXT NOT NULL CHECK (anomaly_type IN ('battery', 'gps', 'sensor', 'lipo')),
    subtype       TEXT NOT NULL,
    start_time    TEXT NOT NULL,
    end_time      TEXT NOT NULL,
    params        TEXT NOT NULL CHECK (json_valid(params)),
    CHECK (end_time >= start_time)
);

-- Analytics output: what the detectors think happened (filled in Phase 5).
-- No link to injected_anomalies on purpose; matching is done by the evaluator.
CREATE TABLE IF NOT EXISTS alerts (
    alert_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    detector      TEXT NOT NULL CHECK (detector IN ('rule', 'ml')),
    drone_id      TEXT NOT NULL REFERENCES drones(drone_id),
    mission_id    TEXT NOT NULL REFERENCES missions(mission_id),
    anomaly_type  TEXT NOT NULL,
    start_time    TEXT NOT NULL,
    end_time      TEXT NOT NULL,
    score         REAL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (end_time >= start_time)
);
