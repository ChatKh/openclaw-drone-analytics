"""
Integration test (Phase 8, day 19): the whole system on one small run.

simulator -> CSV -> loaders -> API -> SQLite -> rule + ML detectors -> evaluator,
then the two readers (dashboard, agent CLI) on the same database. Unit tests check
each part; this checks that the parts agree with each other.
3 simulated days (32k rows, 3 planted faults) keep it under a minute.
"""
import json

import pytest
from streamlit.testing.v1 import AppTest

from src import pipeline
from src.agent import fleet
from src.db.database import connect

DAYS = 3


@pytest.fixture(scope="module")
def first_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("pipeline")
    cfg = pipeline.Config(days=DAYS, seed=42, data_dir=root / "data", db=root / "drone.db", fresh=True,
                          model_path=root / "iforest.joblib", retrain=True, ml_flights=20, ml_calib_flights=20)
    report = pipeline.run(cfg)
    return cfg, report


@pytest.fixture(scope="module")
def second_run(first_run):
    cfg, _ = first_run
    again = pipeline.Config(**{**cfg.__dict__, "fresh": False, "retrain": False})
    return pipeline.run(again)


@pytest.fixture
def on_pipeline_db(first_run, monkeypatch, tmp_path):
    cfg, _ = first_run
    monkeypatch.setenv("DRONE_DB_PATH", str(cfg.db))
    monkeypatch.setattr(fleet, "AUDIT_LOG", tmp_path / "audit.jsonl")
    return cfg


def scalar(cfg, sql):
    conn = connect(str(cfg.db))
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_every_row_goes_through_the_api_into_the_database(first_run):
    cfg, report = first_run
    c = report.counts
    assert report.ok
    assert c["telemetry_rows"] == c["sent"] == c["accepted"] == c["stored"] > 30_000
    assert c["rejected"] == 0 and c["duplicates"] == 0
    assert scalar(cfg, "SELECT COUNT(*) FROM injected_anomalies") == c["injected"] == 3


def test_detectors_write_alerts_and_get_scored(first_run):
    _, report = first_run
    rule, ml = report.detectors["rule"], report.detectors["ml"]
    assert rule["injected"] == ml["injected"] == 3
    assert rule["recall"] == 1.0 and rule["false_alarms"] == 0   # rule v2 on the first dev days
    assert ml["alerts"] > 0 and ml["recall"] > 0                  # small model: only check it works


def test_running_again_doubles_nothing(first_run, second_run):
    cfg, first = first_run
    c = second_run.counts
    assert (c["accepted"], c["duplicates"]) == (0, first.counts["telemetry_rows"])
    assert c["stored"] == first.counts["stored"]
    assert second_run.detectors["rule"]["alerts"] == first.detectors["rule"]["alerts"]
    assert scalar(cfg, "SELECT COUNT(*) FROM alerts WHERE detector = 'rule'") == first.detectors["rule"]["alerts"]


def test_report_file_is_valid_json(first_run, tmp_path):
    cfg, _ = first_run
    out = tmp_path / "report.json"
    code = pipeline.main(["--days", "1", "--data-dir", str(tmp_path / "d"), "--db", str(tmp_path / "x.db"),
                          "--fresh", "--report", str(out), "--model", str(tmp_path / "m.joblib"), "--ml-flights", "10"])
    body = json.loads(out.read_text())
    assert code == 0 and body["ok"] and body["counts"]["rejected"] == 0


def test_agent_cli_reads_the_same_numbers(on_pipeline_db, first_run, capsys):
    _, report = first_run
    assert fleet.main(["summary"]) == fleet.EXIT_OK
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert summary["flights"] == scalar(on_pipeline_db, "SELECT COUNT(*) FROM missions WHERE role != 'standby'")
    assert summary["rule_alerts"] == report.detectors["rule"]["alerts"]
    assert summary["ml_alerts"] == report.detectors["ml"]["alerts"]


def test_agent_still_cannot_see_the_answer_key(on_pipeline_db, capsys):
    assert fleet.main(["sql", "SELECT count(*) FROM injected_anomalies"]) == fleet.EXIT_REFUSED
    assert "answer key" in json.loads(capsys.readouterr().out)["error"]


def test_dashboard_shows_the_same_flights(on_pipeline_db, first_run):
    at = AppTest.from_string("from src.dashboard import views as V\nV.sidebar()\nV.page_overview()\n",
                             default_timeout=60).run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Flights"] == str(scalar(on_pipeline_db, "SELECT COUNT(*) FROM missions WHERE role != 'standby'"))
    assert metrics["Rule alerts"] == str(first_run[1].detectors["rule"]["alerts"])


def test_evaluation_page_matches_the_pipeline_report(on_pipeline_db, first_run):
    at = AppTest.from_string("from src.dashboard import views as V\nV.sidebar()\nV.page_evaluation()\n",
                             default_timeout=60).run()
    rule = first_run[1].detectors["rule"]
    assert any(m.label.startswith("rule") and m.value.startswith(f"{rule['precision']:.0%}") for m in at.metric)


def test_api_writing_to_another_database_fails_the_run(tmp_path, monkeypatch):
    # The classic Docker mistake: the API container and the pipeline use different DB files.
    # The API says "accepted", but the rows never reach the database the detectors read.
    from src.ingest.send_telemetry import SendTotals

    def accepted_elsewhere(cfg, rows):
        n = sum(1 for _ in rows)
        return SendTotals(sent=n, accepted=n, batches=1)

    monkeypatch.setattr(pipeline, "_send", accepted_elsewhere)
    cfg = pipeline.Config(days=1, data_dir=tmp_path / "d", db=tmp_path / "x.db", fresh=True,
                          model_path=tmp_path / "m.joblib")
    with pytest.raises(pipeline.CheckFailed, match="same database"):
        pipeline.run(cfg)
