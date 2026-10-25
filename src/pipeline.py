"""
Whole pipeline in one command (Phase 8).

  simulate -> CSV -> reference + answer key -> telemetry THROUGH THE API -> SQLite
           -> rule detector -> ML detector -> evaluation -> report (JSON)

Every step is the same code the separate commands run; this only puts them in
order, times them, and fails loudly when a number does not add up.

Run:
  python -m src.pipeline --fresh                         # dev set, API in-process
  python -m src.pipeline --fresh --api-url http://api:8000   # through a running API (Docker)
  python -m src.pipeline --fresh --seed 7 --days 30 --data-dir data/test --db data/test.db

--api-url: the API server must write to the SAME database file as --db.
Exit code: 0 ok, 1 a check failed, 2 a step crashed.
"""
import argparse
import csv
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

import joblib

from src.analytics import ml_detector, rule_detector
from src.db.database import connect, init_db
from src.evaluation.evaluator import evaluate
from src.ingest.load_ground_truth import load_ground_truth
from src.ingest.load_reference import load_reference
from src.ingest.send_telemetry import read_rows, send_rows
from src.simulator.fleet import build_drones, generate_all

log = logging.getLogger("pipeline")


class CheckFailed(Exception):
    """The pipeline ran, but a number that must add up did not."""


@dataclass
class Config:
    days: int = 15
    seed: int = 42
    data_dir: Path = Path("data")
    db: Path = Path("data/drone.db")
    api_url: str | None = None          # None = run the API in-process (same code, no network)
    fresh: bool = False                 # delete the database first (repeats would be skipped as duplicates)
    model_path: Path = ml_detector.MODEL_PATH
    retrain: bool = False
    ml_flights: int = 100
    ml_calib_flights: int = 100
    batch_size: int = 5000


@dataclass
class Report:
    config: dict
    steps: dict[str, float] = field(default_factory=dict)       # seconds per step
    counts: dict[str, int] = field(default_factory=dict)
    detectors: dict[str, dict] = field(default_factory=dict)
    ok: bool = False
    error: str | None = None


def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _check(cond: bool, message: str) -> None:
    if not cond:
        raise CheckFailed(message)


@contextmanager
def _step(report: Report, name: str):
    log.info("▶ %s", name)
    t = time.perf_counter()
    yield
    report.steps[name] = round(time.perf_counter() - t, 2)
    log.info("✓ %s (%.1f s)", name, report.steps[name])


def _send(cfg: Config, rows):
    if cfg.api_url:
        import httpx
        with httpx.Client(base_url=cfg.api_url, timeout=120) as client:
            client.get("/health").raise_for_status()
            return send_rows(client, rows, cfg.batch_size)
    # In-process: the real FastAPI app and validation, without a server or a port.
    from fastapi.testclient import TestClient
    from src.api.main import app
    before = os.environ.get("DRONE_DB_PATH")
    os.environ["DRONE_DB_PATH"] = str(cfg.db)  # the app opens its DB per request from this
    try:
        with TestClient(app) as client:
            return send_rows(client, rows, cfg.batch_size)
    finally:
        if before is None:
            os.environ.pop("DRONE_DB_PATH", None)
        else:
            os.environ["DRONE_DB_PATH"] = before


def run(cfg: Config, report: Report | None = None) -> Report:
    """Run every step. Fills `report` as it goes, so a failure still shows how far it got."""
    report = report or Report(config={})
    report.config = {k: str(v) for k, v in asdict(cfg).items()}

    def step(name: str):
        return _step(report, name)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if cfg.fresh:
        for suffix in ("", "-wal", "-shm"):
            Path(str(cfg.db) + suffix).unlink(missing_ok=True)

    with step("simulate"):
        telemetry, injected, missions, results = generate_all(num_days=cfg.days, seed=cfg.seed)
        for name, rows in (("telemetry_logs", telemetry), ("injected_anomalies", injected),
                           ("missions", missions), ("drones", build_drones()),
                           ("conditions", results["conditions"])):
            _write_csv(cfg.data_dir / f"{name}.csv", rows)
        report.counts.update(telemetry_rows=len(telemetry), injected=len(injected), missions=len(missions))

    conn = connect(str(cfg.db))
    try:
        init_db(conn)
        with step("load reference + answer key"):
            load_reference(cfg.data_dir, conn)
            load_ground_truth(cfg.data_dir, conn)

        with step("send telemetry through the API"):
            totals = _send(cfg, read_rows(cfg.data_dir / "telemetry_logs.csv"))
        report.counts.update(sent=totals.sent, accepted=totals.accepted,
                             duplicates=totals.duplicates, rejected=totals.rejected)
        _check(totals.rejected == 0, f"{totals.rejected} rows rejected, e.g. {totals.examples[:1]}")
        _check(totals.sent == len(telemetry), f"sent {totals.sent} of {len(telemetry)} rows")
        stored = conn.execute("SELECT COUNT(*) FROM telemetry_logs").fetchone()[0]
        report.counts["stored"] = stored
        _check(stored >= len(telemetry), f"only {stored} of {len(telemetry)} rows in {cfg.db}; "
                                         "is the API writing to the same database file?")
        _check(conn.execute("PRAGMA foreign_key_check").fetchall() == [], "foreign key check failed")

        with step("rule detector"):
            rule_detector.run(conn)

        with step("ML detector"):
            if cfg.retrain or not cfg.model_path.exists():
                bundle = ml_detector.train(cfg.ml_flights, n_calib=cfg.ml_calib_flights)
                cfg.model_path.parent.mkdir(parents=True, exist_ok=True)
                joblib.dump(bundle, cfg.model_path)
            else:
                bundle = joblib.load(cfg.model_path)
            ml_detector.run(conn, bundle)

        with step("evaluate"):
            for det in ("rule", "ml"):
                rep = evaluate(conn, det)
                t = rep.total
                report.detectors[det] = {"injected": t.injected, "caught": t.detected, "alerts": t.alerts,
                                         "false_alarms": t.false_alarms, "precision": t.precision,
                                         "recall": t.recall}
        report.ok = True
    finally:
        conn.close()
    return report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "ingestion", "send_telemetry", "repository"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Run the whole pipeline")
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--db", type=Path, default=Path(os.environ.get("DRONE_DB_PATH", "data/drone.db")))
    ap.add_argument("--api-url", help="send telemetry to this running API instead of in-process")
    ap.add_argument("--fresh", action="store_true", help="delete the database first")
    ap.add_argument("--retrain", action="store_true", help="train the ML model even if one exists")
    ap.add_argument("--model", type=Path, default=ml_detector.MODEL_PATH, help="ML model file")
    ap.add_argument("--ml-flights", type=int, default=100,
                    help="normal flights to train on, and again to calibrate (only when training)")
    ap.add_argument("--report", type=Path, help="write the JSON report here (default: <data-dir>/pipeline_report.json)")
    args = ap.parse_args(argv)

    cfg = Config(days=args.days, seed=args.seed, data_dir=args.data_dir, db=args.db,
                 api_url=args.api_url, fresh=args.fresh, retrain=args.retrain, model_path=args.model,
                 ml_flights=args.ml_flights, ml_calib_flights=args.ml_flights)
    started = time.perf_counter()
    report = Report(config={})
    try:
        run(cfg, report)
        code = 0
    except CheckFailed as e:
        log.error("check failed: %s", e)
        report.error, code = f"check failed: {e}", 1
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline crashed")
        report.error, code = f"{type(e).__name__}: {e}", 2

    out = args.report or (args.data_dir / "pipeline_report.json")
    out.write_text(json.dumps(asdict(report), indent=1, default=str), encoding="utf-8")
    log.info("report: %s (%.0f s total)", out, time.perf_counter() - started)
    for det, r in report.detectors.items():
        p = "n/a" if r["precision"] is None else f"{r['precision']:.0%}"
        log.info("%-4s precision %s recall %.0f%% (%d alerts, %d false)",
                 det, p, (r["recall"] or 0) * 100, r["alerts"], r["false_alarms"])
    return code


if __name__ == "__main__":
    sys.exit(main())
