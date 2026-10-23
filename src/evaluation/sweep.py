"""
Threshold sweep on the dev set (Phase 5, day 9): vary one rule threshold at a
time, keep the others at their defaults, and report caught / false alarms for
the anomaly type that rule covers. Reproduces the table behind the chosen
RuleConfig defaults.

Pick values from the middle of the range where every anomaly is caught and
no false alarm fires, not from its edge: the edge is tuned to this one data set.

Run:  python -m src.evaluation.sweep     (on the dev database only, never on test)
"""
import logging
from dataclasses import replace

from src.analytics.rule_detector import RuleConfig, run
from src.db.database import connect
from src.evaluation.evaluator import evaluate

SWEEPS = {
    "battery_drop_pct": ("battery", [0.5, 1, 2, 3, 4, 5, 6, 8]),
    "gps_min_s": ("gps", [1, 5, 10, 20, 30, 45, 60, 90]),
    "max_hdop": ("gps", [1.5, 2.0, 2.5, 3.5, 5.0]),
    "spike_score": ("sensor", [4, 6, 8, 10, 12, 14, 16, 24]),
    "min_vtol_return_pct": ("lipo", [45, 50, 52, 54, 56, 58]),
}


def sweep(conn, base: RuleConfig = RuleConfig()) -> dict[str, list[tuple[float, int, int, int]]]:
    """field -> [(value, caught, injected, false_alarms)]"""
    out = {}
    for field, (kind, values) in SWEEPS.items():
        out[field] = []
        for v in values:
            run(conn, replace(base, **{field: v}))
            r = evaluate(conn).by_type[kind]
            out[field].append((v, r.detected, r.injected, r.false_alarms))
    run(conn, base)  # leave the default alerts in place
    return out


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    conn = connect()
    try:
        results = sweep(conn)
    finally:
        conn.close()
    default = RuleConfig()
    for field, rows in results.items():
        cells = [f"{'*' if v == getattr(default, field) else ''}{v}: {c}/{n} caught, {fa} false"
                 for v, c, n, fa in rows]
        print(f"{field}\n  " + "\n  ".join(cells))
    print("\n* = current default")


if __name__ == "__main__":
    main()
