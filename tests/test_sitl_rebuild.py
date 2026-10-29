"""sitl/flights.json and the one-command rebuild (python -m src.etl.sitl_rebuild)."""
import json
import re
import shutil
from pathlib import Path

import pytest

from src.analytics import ml_detector as M
from src.api.models import MISSION_ID_PATTERN
from src.db.database import connect
from src.etl import sitl_rebuild as R
from tests.test_sitl_etl import ROOT, write_tlog

SITL = ROOT / "sitl"
FLIGHTS = R.load_manifest(SITL / "flights.json")


def parm(path: Path) -> dict:
    return dict(line.split()[:2] for line in path.read_text().splitlines() if line.strip() and not line.startswith("#"))


# ---------- the manifest ----------

def test_every_recorded_log_is_in_the_manifest_and_every_entry_exists():
    on_disk = {p.relative_to(SITL).as_posix() for p in (SITL / "flights").rglob("*.tlog")}
    assert on_disk == {f["file"] for f in FLIGHTS}


def test_entries_are_complete_and_consistent():
    ids = [f["mission_id"] for f in FLIGHTS]
    assert len(set(ids)) == len(ids) and all(re.match(MISSION_ID_PATTERN, i) for i in ids)
    for f in FLIGHTS:
        assert f["status"] in R.STATUSES
        assert (f["status"] == "flown") == ("/failed/" not in "/" + f["file"])   # failed logs live in failed/
        assert (SITL / f["phases"]).exists() and (SITL / f["params"]).exists()
        assert f["start_heading_deg"] in (353, 180)
        wind = float(parm(SITL / f["params"]).get("SIM_WIND_SPD", 0))
        assert wind == f["wind_mps"]                                             # the table says what was flown


def test_results_file_lists_every_flight():
    text = (SITL / "RESULTS.md").read_text()
    assert all(f"`{Path(f['file']).name}`" in text for f in FLIGHTS)
    assert "Do not edit by hand" in text


# ---------- the rebuild ----------

@pytest.fixture(scope="module")
def small_bundle():
    return M.train(n_flights=5, seed=9000, n_calib=5, calib_seed=9100)


@pytest.fixture()
def sandbox(tmp_path):
    """A sitl/ folder with one completed flight and one that never transitioned (like no_liftoff)."""
    sitl = tmp_path / "sitl"
    (sitl / "flights" / "failed").mkdir(parents=True)
    shutil.copy(SITL / "mission_phases.json", sitl)
    shutil.copy(SITL / "sitl.parm", sitl)
    write_tlog(sitl / "flights" / "ok.tlog")
    write_tlog(sitl / "flights" / "failed" / "stuck.tlog", transition_at=None)
    common = {"phases": "mission_phases.json", "params": "sitl.parm", "wind_mps": 0, "start_heading_deg": 353}
    (sitl / "flights.json").write_text(json.dumps({"flights": [
        {"file": "flights/ok.tlog", "mission_id": "D0001_S_AM", "status": "flown", **common},
        {"file": "flights/failed/stuck.tlog", "mission_id": "D0002_S_AM", "status": "no_liftoff",
         "note": "test", **common},
    ]}))
    return sitl


def test_rebuild_loads_completed_flights_only_and_writes_results(sandbox, tmp_path, small_bundle):
    db = tmp_path / "sitl.db"
    results = R.rebuild(sandbox / "flights.json", db, tmp_path / "out", bundle=small_bundle)
    conn = connect(str(db))
    try:
        assert [r[0] for r in conn.execute("SELECT mission_id FROM missions")] == ["D0001_S_AM"]
        stored = conn.execute("SELECT COUNT(*) FROM telemetry_logs").fetchone()[0]
    finally:
        conn.close()
    ok, stuck = results
    assert stored == ok.rows and ok.battery_checks is not None and stuck.battery_checks is None
    assert (tmp_path / "out" / "ok" / "flight_summary.json").exists()
    md = R.results_markdown(results)
    assert "| `ok.tlog` |" in md and "| `stuck.tlog` |" in md and "no liftoff" in md
    assert stuck.summary["wind_estimate"] is None                  # never flew on the wing: no estimate shown


def test_rebuild_twice_gives_the_same_database_and_results(sandbox, tmp_path, small_bundle):
    db = tmp_path / "sitl.db"
    first = R.results_markdown(R.rebuild(sandbox / "flights.json", db, tmp_path / "out", bundle=small_bundle))
    second = R.results_markdown(R.rebuild(sandbox / "flights.json", db, tmp_path / "out", bundle=small_bundle))
    conn = connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == 1   # not doubled
    finally:
        conn.close()
    assert first == second
