"""
Anomaly injection: change telemetry rows on purpose and return what was done.

Each function returns (start_time, end_time, params). params records the
magnitude actually injected, so the evaluator knows how hard each case was.

Magnitudes are drawn to straddle the Phase 5 detection thresholds, so some
injected anomalies are marginal and hard to catch (like borderline defects on
a production line). Detection thresholds, for reference:
  battery: cruise battery falls > 15 % within a short window
  gps:     satellite_count < 6 or hdop > 5
  sensor:  altitude changes > 10 m within one second
  lipo:    VTOL battery < 45 % when the return leg starts
"""
import random

# Ranges for the injected magnitude (low = easy to miss, high = obvious).
BATTERY_DROP_PCT = (5.0, 25.0)
GPS_SATELLITES = (7, 3)       # severity 0 -> 7 satellites, severity 1 -> 3
GPS_HDOP = (2.0, 8.0)         # severity 0 -> 2.0, severity 1 -> 8.0
SPIKE_ALTITUDE_M = (25.0, 45.0)
LIPO_LEVEL_PCT = (30.0, 50.0)  # VTOL level when the return leg starts


def inject_battery(rows, start_idx, duration, rng: random.Random):
    """Cruise battery drops at once and does not recover (a cell failing)."""
    drop_pct = round(rng.uniform(*BATTERY_DROP_PCT), 1)
    for i in range(start_idx, len(rows)):
        rows[i]["battery_cruise_pct"] = round(max(0.0, rows[i]["battery_cruise_pct"] - drop_pct), 2)
    end = min(start_idx + duration, len(rows) - 1)
    return rows[start_idx]["timestamp"], rows[end]["timestamp"], {"drop_pct": drop_pct, "field": "battery_cruise_pct"}


def inject_gps(rows, start_idx, duration, rng: random.Random):
    """GPS quality degrades for a while, then recovers."""
    severity = rng.random()
    sat = round(GPS_SATELLITES[0] + (GPS_SATELLITES[1] - GPS_SATELLITES[0]) * severity)
    hdop = round(GPS_HDOP[0] + (GPS_HDOP[1] - GPS_HDOP[0]) * severity, 1)
    end = min(start_idx + duration, len(rows))
    for i in range(start_idx, end):
        rows[i]["satellite_count"] = sat
        rows[i]["hdop"] = hdop
    return rows[start_idx]["timestamp"], rows[end - 1]["timestamp"], {"satellite_count": sat, "hdop": hdop}


def inject_sensor(rows, spike_idx, rng: random.Random, field="altitude_m"):
    """One bad altitude reading, then back to normal."""
    value = round(rng.uniform(*SPIKE_ALTITUDE_M), 1)
    rows[spike_idx][field] = value
    return rows[spike_idx]["timestamp"], rows[spike_idx]["timestamp"], {"field": field, "value": value}


def inject_lipo(rows, rng: random.Random):
    """
    VTOL battery starts the return leg too low. Landing still drains it at the
    normal rate, so a low enough level reaches 0 % before touchdown.
    """
    start = next((i for i, r in enumerate(rows) if r["flight_mode"] == "transit_back"), None)
    if start is None:
        return None
    level = round(rng.uniform(*LIPO_LEVEL_PCT), 1)
    offset = rows[start]["battery_vtol_pct"] - level
    if offset <= 0:  # already at or below that level: nothing to inject
        return None
    for i in range(start, len(rows)):
        rows[i]["battery_vtol_pct"] = round(max(0.0, rows[i]["battery_vtol_pct"] - offset), 2)
    return rows[start]["timestamp"], rows[-1]["timestamp"], {
        "vtol_level": level,
        "vtol_at_touchdown": rows[-1]["battery_vtol_pct"],
    }
