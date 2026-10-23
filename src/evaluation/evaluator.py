"""
Evaluator (Phase 5, day 8): compare a detector's alerts with the answer key.

This is the only code allowed to read injected_anomalies.

Matching rule: an alert matches an injected anomaly when they are in the same
mission, have the same anomaly_type, and their time windows overlap once each
side is widened by TOLERANCE_S seconds.

  precision = alerts that match something / all alerts           (how often an alert is right)
  recall    = injected anomalies with >= 1 matching alert / all  (how many problems were caught)
  false alarms per flight hour = unmatched alerts / hours of flight telemetry
  latency   = seconds from the anomaly's start to the first matching alert

Run:  python -m src.evaluation.evaluator [--detector rule]
"""
import argparse
import json
import logging
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.db.database import connect, init_db

log = logging.getLogger("evaluator")

TOLERANCE_S = 30
TYPES = ["battery", "gps", "sensor", "lipo"]
TS = "%Y-%m-%d %H:%M:%S"


def _ts(s: str) -> datetime:
    return datetime.strptime(s, TS)


def _overlaps(a_start, a_end, b_start, b_end, tol_s: int) -> bool:
    tol = timedelta(seconds=tol_s)
    return _ts(a_start) - tol <= _ts(b_end) and _ts(b_start) - tol <= _ts(a_end)


@dataclass
class TypeResult:
    anomaly_type: str
    injected: int = 0
    detected: int = 0
    alerts: int = 0
    correct_alerts: int = 0
    latencies_s: list[float] = field(default_factory=list)

    @property
    def false_alarms(self) -> int:
        return self.alerts - self.correct_alerts

    @property
    def missed(self) -> int:
        return self.injected - self.detected

    @property
    def precision(self) -> float | None:
        return self.correct_alerts / self.alerts if self.alerts else None

    @property
    def recall(self) -> float | None:
        return self.detected / self.injected if self.injected else None


@dataclass
class Report:
    detector: str
    flight_hours: float
    by_type: dict[str, TypeResult]
    total: TypeResult
    missed: list[dict]         # injected anomalies nobody caught, with their magnitude
    false_alarms: list[dict]   # alerts that match nothing

    @property
    def false_alarms_per_hour(self) -> float | None:
        return self.total.false_alarms / self.flight_hours if self.flight_hours else None


def evaluate(conn: sqlite3.Connection, detector: str = "rule", tolerance_s: int = TOLERANCE_S) -> Report:
    init_db(conn)
    truth = [dict(r) for r in conn.execute("SELECT * FROM injected_anomalies ORDER BY anomaly_id")]
    alerts = [dict(r) for r in conn.execute(
        "SELECT * FROM alerts WHERE detector = ? ORDER BY start_time", (detector,))]
    flight_rows = conn.execute(
        "SELECT COUNT(*) FROM telemetry_logs t JOIN missions m ON m.mission_id = t.mission_id "
        "WHERE m.role IN ('AM', 'PM')").fetchone()[0]

    by_type = {t: TypeResult(t) for t in TYPES}
    total = TypeResult("all")

    def matches(a, g) -> bool:
        return (a["mission_id"] == g["mission_id"] and a["anomaly_type"] == g["anomaly_type"]
                and _overlaps(a["start_time"], a["end_time"], g["start_time"], g["end_time"], tolerance_s))

    missed, false_alarms = [], []
    for g in truth:
        r = by_type.setdefault(g["anomaly_type"], TypeResult(g["anomaly_type"]))
        r.injected += 1
        hits = [a for a in alerts if matches(a, g)]
        if hits:
            r.detected += 1
            first = min(_ts(a["start_time"]) for a in hits)
            r.latencies_s.append(max(0.0, (first - _ts(g["start_time"])).total_seconds()))
        else:
            missed.append({k: g[k] for k in ("anomaly_id", "mission_id", "anomaly_type", "start_time")}
                          | {"params": json.loads(g["params"])})

    for a in alerts:
        r = by_type.setdefault(a["anomaly_type"], TypeResult(a["anomaly_type"]))
        r.alerts += 1
        if any(matches(a, g) for g in truth):
            r.correct_alerts += 1
        else:
            false_alarms.append({k: a[k] for k in ("mission_id", "anomaly_type", "start_time", "end_time", "score")})

    for r in by_type.values():
        total.injected += r.injected
        total.detected += r.detected
        total.alerts += r.alerts
        total.correct_alerts += r.correct_alerts
        total.latencies_s += r.latencies_s

    report = Report(detector, round(flight_rows / 3600, 2), by_type, total, missed, false_alarms)
    log.info("detector=%s injected=%d alerts=%d precision=%s recall=%s",
             detector, total.injected, total.alerts, _pct(total.precision), _pct(total.recall))
    return report


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _latency(r: TypeResult) -> str:
    return f"{statistics.median(r.latencies_s):.0f}" if r.latencies_s else "-"


def format_report(rep: Report) -> str:
    head = f"{'type':<8} {'injected':>8} {'caught':>7} {'missed':>7} {'alerts':>7} {'false':>6} " \
           f"{'precision':>10} {'recall':>7} {'latency_s':>10}"
    lines = [f"Detector: {rep.detector}   flight hours: {rep.flight_hours}   tolerance: ±{TOLERANCE_S} s", "", head]
    for r in [*rep.by_type.values(), rep.total]:
        lines.append(f"{r.anomaly_type:<8} {r.injected:>8} {r.detected:>7} {r.missed:>7} {r.alerts:>7} "
                     f"{r.false_alarms:>6} {_pct(r.precision):>10} {_pct(r.recall):>7} {_latency(r):>10}")
    fa = rep.false_alarms_per_hour
    lines += ["", f"False alarms per flight hour: {'n/a' if fa is None else f'{fa:.2f}'}"]
    if rep.missed:
        lines += ["", "Missed (injected magnitude):"]
        lines += [f"  {m['mission_id']}  {m['anomaly_type']:<7} {m['params']}" for m in rep.missed]
    if rep.false_alarms:
        lines += ["", "False alarms:"]
        lines += [f"  {f['mission_id']}  {f['anomaly_type']:<7} {f['start_time']} -> {f['end_time'][11:]}  "
                  f"score={f['score']}" for f in rep.false_alarms]
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Compare a detector's alerts with the answer key")
    ap.add_argument("--detector", default="rule", choices=["rule", "ml"])
    args = ap.parse_args()
    conn = connect()
    try:
        print(format_report(evaluate(conn, args.detector)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
