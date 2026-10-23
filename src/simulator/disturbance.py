"""
Normal disturbances: conditions that make telemetry look odd but are NOT faults.
They are not in the answer key. A good detector must not alarm on them.

Two kinds, applied to a mission's rows before any anomaly is injected:

wind (about 35 % of flights, strength 0.3-1.0)
  - takeoff and landing burn more VTOL battery (holding position in wind)
  - gust episodes of 20-120 s during cruise:
      altitude and ground speed wobble more,
      reported cruise battery sags while current is high, then recovers
      (voltage sag under load), and true drain runs 1.5x faster

multipath (about 30 % of flights, near tree canopy during survey)
  - episodes of 5-25 s: hdop rises by 1.0-2.5, 1-3 satellites lost

Each mission uses its own RNG, so adding weather does not change which
missions get anomalies or how strong those anomalies are.
"""
import random

WIND_PROB = 0.35
MULTIPATH_PROB = 0.30
CRUISE_MODES = ("transit_out", "survey", "transit_back")


def _episodes(rng: random.Random, idx: list[int], count: int, min_s: int, max_s: int) -> list[range]:
    """Pick `count` time windows inside the row positions `idx` (which are consecutive)."""
    out = []
    if not idx:
        return out
    for _ in range(count):
        length = rng.randint(min_s, max_s)
        start = rng.randint(idx[0], max(idx[0], idx[-1] - length))
        out.append(range(start, min(start + length, idx[-1] + 1)))
    return out


def _apply_wind(rows: list[dict], rng: random.Random, level: float) -> int:
    # 1) more VTOL drain during takeoff and landing, carried forward
    extra, prev = 0.0, rows[0]["battery_vtol_pct"]
    for r in rows:
        used = prev - r["battery_vtol_pct"]
        prev = r["battery_vtol_pct"]
        if r["flight_mode"] in ("takeoff", "landing") and used > 0:
            extra += used * 0.15 * level
        r["battery_vtol_pct"] = round(max(0.0, r["battery_vtol_pct"] - extra), 2)

    # 2) gust episodes during cruise
    cruise = [i for i, r in enumerate(rows) if r["flight_mode"] in CRUISE_MODES]
    gusts = _episodes(rng, cruise, round(2 + 6 * level), 20, 120)
    extra_drain = 0.0
    for i, r in enumerate(rows):
        in_gust = any(i in g for g in gusts)
        if in_gust:
            extra_drain += 0.0136 * 0.5  # 1.5x the normal cruise drain per second
        sag = 0.0
        if in_gust:
            g = next(g for g in gusts if i in g)
            sag = (2.0 + 5.0 * level) * min(1.0, (i - g.start + 1) / 5)  # builds up over ~5 s
            r["altitude_m"] = round(max(0.0, r["altitude_m"] + rng.gauss(0, 1.5 * level)), 2)
            r["ground_speed_mps"] = round(max(0.0, r["ground_speed_mps"] + rng.gauss(0, 2.0 * level)), 2)
        if r["flight_mode"] in CRUISE_MODES or extra_drain:
            r["battery_cruise_pct"] = round(max(0.0, r["battery_cruise_pct"] - extra_drain - sag), 2)
    return len(gusts)


def _apply_multipath(rows: list[dict], rng: random.Random) -> int:
    survey = [i for i, r in enumerate(rows) if r["flight_mode"] == "survey"]
    episodes = _episodes(rng, survey, rng.randint(1, 3), 5, 25)
    for ep in episodes:
        hdop_add = rng.uniform(1.0, 2.5)
        sats_lost = rng.randint(1, 3)
        for i in ep:
            rows[i]["hdop"] = round(rows[i]["hdop"] + hdop_add, 2)
            rows[i]["satellite_count"] = max(0, rows[i]["satellite_count"] - sats_lost)
    return len(episodes)


def apply_weather(rows: list[dict], rng: random.Random) -> dict:
    """Change rows in place. Returns what was applied (kept for analysis, not for detectors)."""
    cond = {"windy": False, "wind_level": 0.0, "gust_episodes": 0, "multipath_episodes": 0}
    if rng.random() < WIND_PROB:
        level = round(rng.uniform(0.3, 1.0), 2)
        cond.update(windy=True, wind_level=level, gust_episodes=_apply_wind(rows, rng, level))
    if rng.random() < MULTIPATH_PROB:
        cond["multipath_episodes"] = _apply_multipath(rows, rng)
    return cond
