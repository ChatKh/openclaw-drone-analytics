"""Tests for the results gate (Phase 8, day 22)."""
import json

from src.evaluation import gate


def report(rule=(1.0, 1.0), ml=(0.92, 0.86), ok=True):
    return {"ok": ok, "error": None if ok else "boom",
            "detectors": {"rule": {"precision": rule[0], "recall": rule[1]},
                          "ml": {"precision": ml[0], "recall": ml[1]}}}


def test_todays_results_pass():
    assert gate.check(report()) == []


def test_a_drop_in_recall_fails():
    problems = gate.check(report(ml=(0.92, 0.50)))
    assert problems == ["ml recall 50% is below the minimum 80%"]


def test_no_alerts_at_all_fails():
    # precision is None when a detector raised nothing: that must not pass quietly
    assert gate.check(report(rule=(None, 0.0)))


def test_a_failed_pipeline_fails():
    assert "did not finish" in gate.check(report(ok=False))[0]


def test_cli_exit_codes(tmp_path, capsys):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(report()))
    bad.write_text(json.dumps(report(rule=(0.5, 1.0))))
    assert gate.main(["--report", str(good)]) == 0
    assert gate.main(["--report", str(bad)]) == 1
    assert gate.main(["--report", str(tmp_path / "missing.json")]) == 2
    assert "FAIL rule precision 50%" in capsys.readouterr().out
