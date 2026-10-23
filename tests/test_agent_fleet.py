"""Tests for the agent's fleet CLI (Phase 7): it answers, and it cannot be talked into harm."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from src.agent import fleet
from tests.test_dashboard import dashboard_db  # noqa: F401  (one flight, rule alerts, answer key)

ROOT = Path(__file__).resolve().parents[1]

ATTACKS = {
    "delete": "DELETE FROM alerts",
    "update": "UPDATE drones SET status = 'retired'",
    "insert": "INSERT INTO alerts (detector, drone_id, mission_id, anomaly_type, start_time, end_time) "
              "VALUES ('rule','A','D0001_A_AM','gps','x','x')",
    "drop": "DROP TABLE telemetry_logs",
    "create": "CREATE TABLE notes (x)",
    "attach": "ATTACH DATABASE 'other.db' AS other",
    "pragma": "PRAGMA journal_mode = DELETE",
    "two statements": "SELECT 1; DELETE FROM alerts",
    "extension": "SELECT load_extension('evil')",
}

ANSWER_KEY_PEEKS = {
    "columns": "SELECT * FROM injected_anomalies",
    "count only": "SELECT count(*) FROM injected_anomalies",
    "join count": "SELECT count(*) FROM alerts a JOIN injected_anomalies g USING (mission_id)",
    "exists": "SELECT 1 WHERE EXISTS (SELECT 1 FROM injected_anomalies)",
    "cte": "WITH k AS (SELECT mission_id FROM injected_anomalies) SELECT count(*) FROM k",
}


def call(capsys, *argv) -> tuple[int, dict]:
    code = fleet.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture(autouse=True)
def audit_log(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(fleet, "AUDIT_LOG", path)
    return path


def digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------- answers ----------

@pytest.mark.parametrize("argv", [
    ["summary"], ["drones"], ["missions"], ["missions", "--drone", "A", "--with-alerts"],
    ["mission", "D0001_A_AM"], ["alerts"], ["alerts", "--type", "sensor", "--detector", "rule"],
    ["energy", "--drone", "A"], ["schema"], ["sql", "SELECT drone_id, count(*) AS n FROM alerts GROUP BY 1"],
])
def test_every_command_answers_in_json(dashboard_db, capsys, argv):
    code, out = call(capsys, *argv)
    assert code == fleet.EXIT_OK and out["ok"], out


def test_summary_matches_the_data(dashboard_db, capsys):
    _, out = call(capsys, "summary")
    assert out["summary"]["flights"] == 1 and out["summary"]["rule_alerts"] == 1


def test_schema_does_not_list_the_answer_key(dashboard_db, capsys):
    _, out = call(capsys, "schema")
    assert "injected_anomalies" not in out["tables"] and "telemetry_logs" in out["tables"]


def test_unknown_mission_is_a_clear_refusal(dashboard_db, capsys):
    code, out = call(capsys, "mission", "D9999_Z_AM")
    assert code == fleet.EXIT_REFUSED and "fleet missions" in out["error"]


# ---------- guardrails ----------

@pytest.mark.parametrize("name", ATTACKS)
def test_writes_and_escapes_are_refused(dashboard_db, capsys, name):
    before = digest(dashboard_db)
    code, out = call(capsys, "sql", ATTACKS[name])
    assert code == fleet.EXIT_REFUSED and not out["ok"]
    assert digest(dashboard_db) == before


@pytest.mark.parametrize("name", ANSWER_KEY_PEEKS)
def test_answer_key_is_hidden_even_without_reading_a_column(dashboard_db, capsys, name):
    code, out = call(capsys, "sql", ANSWER_KEY_PEEKS[name])
    assert code == fleet.EXIT_REFUSED and "answer key" in out["error"]


def test_runaway_query_is_stopped(dashboard_db, capsys):
    code, out = call(capsys, "sql", "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                                    "SELECT max(x) FROM c")
    assert code == fleet.EXIT_REFUSED and "longer than" in out["error"]


def test_big_results_are_capped(dashboard_db, capsys):
    _, out = call(capsys, "sql", "SELECT * FROM telemetry_logs")
    assert len(out["rows"]) == fleet.MAX_ROWS and out["truncated"]


def test_semicolon_inside_a_string_is_not_two_statements(dashboard_db, capsys):
    code, out = call(capsys, "sql", "SELECT 'a;b' AS text")
    assert code == fleet.EXIT_OK and out["rows"] == [{"text": "a;b"}]


def test_unexpected_error_still_returns_json(dashboard_db, capsys, monkeypatch):
    def broken(conn, args):
        raise KeyError("boom")
    monkeypatch.setitem(fleet.COMMANDS, "summary", broken)
    code, out = call(capsys, "summary")
    assert code == fleet.EXIT_BUG and not out["ok"] and "KeyError" in out["error"]


def test_missing_database_is_reported_not_created(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "none.db"
    monkeypatch.setenv("DRONE_DB_PATH", str(missing))
    code, out = call(capsys, "summary")
    assert code == fleet.EXIT_NO_DATA and "no database" in out["error"]
    assert not missing.exists()


def test_every_call_is_audited(dashboard_db, capsys, audit_log):
    call(capsys, "summary")
    call(capsys, "sql", "DELETE FROM alerts")
    entries = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert [(e["command"], e["ok"]) for e in entries] == [("summary", True), ("sql", False)]
    assert entries[1]["args"]["query"] == "DELETE FROM alerts" and entries[1]["error"]


def test_wrapper_script_runs_from_any_folder(dashboard_db, tmp_path):
    # OpenClaw may run the command from its own workspace; bin/fleet must still find the project.
    out = subprocess.run([str(ROOT / "bin" / "fleet"), "summary"], cwd=tmp_path, capture_output=True,
                         text=True, env={"DRONE_DB_PATH": dashboard_db, "FLEET_AUDIT_LOG": str(tmp_path / "a.jsonl"),
                                         "PATH": "/usr/bin:/bin"})
    if out.returncode != 0 and ".venv/bin/python" in out.stderr:
        pytest.skip("project .venv not found next to bin/fleet")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["summary"]["flights"] == 1

