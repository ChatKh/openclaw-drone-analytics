"""
Tests for the SITL ETL (tlog -> schema rows -> API).

A synthetic .tlog is written with pymavlink, so the extract step runs on the real file
format without ArduPilot installed. A real SITL flight in sitl/flights/ is used too when present.
"""
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.analytics import rule_detector
from src.db.database import connect
from src.etl import sitl_tlog as E

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_793_000_000          # some second in 2026
ARMED, DISARMED = 128 | 1, 1
SEQ_BY_SECOND = [(5, 1), (21, 2), (41, 3), (60, 4), (75, 5), (91, 6), (111, 7)]  # (from second, seq)


def seq_at(s: int) -> int:
    return [q for start, q in SEQ_BY_SECOND if s >= start][-1]


def write_tlog(path: Path) -> None:
    """130 s: disarmed 0-4 s, armed 5-125 s, disarmed after. Gaps at 60-62 s (short) and 100-109 s (long)."""
    from pymavlink.dialects.v20 import ardupilotmega as mav

    vehicle, gcs = mav.MAVLink(None, srcSystem=1, srcComponent=1), mav.MAVLink(None, srcSystem=255, srcComponent=190)
    out = bytearray()

    def put(t: float, link, msg) -> None:
        out.extend(struct.pack(">Q", int(t * 1e6)) + msg.pack(link))

    for s in range(0, 131):
        if 60 <= s <= 62 or 100 <= s <= 109:
            continue
        t = T0 + s
        armed = 5 <= s <= 125
        put(t + 0.00, vehicle, vehicle.heartbeat_encode(1, 3, ARMED if armed else DISARMED, 0, 4))
        put(t + 0.01, gcs, gcs.heartbeat_encode(6, 8, 0, 0, 0))   # MAVProxy: must be ignored
        put(t + 0.10, vehicle, vehicle.global_position_int_encode(
            s * 1000, int((-35.363262 + s * 1e-5) * 1e7), int(149.165237 * 1e7), 584_000,
            -300 if s < 8 else 40_000, 0, 0, 0, 0))                # below home on the ground
        put(t + 0.20, vehicle, vehicle.vfr_hud_encode(20.0, 21.5 if armed else 0.0, 0, 50, 624.0, 0.0))
        eph = 65535 if s in (30, 31) else 80                       # no fix for two seconds
        put(t + 0.30, vehicle, vehicle.gps_raw_int_encode(s * 1_000_000, 3, 0, 0, 0, eph, 120, 0, 0, 10))
        put(t + 0.40, vehicle, vehicle.sys_status_encode(0, 0, 0, 0, 12_600, 1000, 100 - s // 10,
                                                         0, 0, 0, 0, 0, 0))
        if armed:
            put(t + 0.50, vehicle, vehicle.mission_current_encode(seq_at(s)))
    path.write_bytes(bytes(out))


@pytest.fixture(scope="module")
def tlog(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("sitl") / "synthetic.tlog"
    write_tlog(path)
    return path


@pytest.fixture(scope="module")
def rows(tlog):
    return E.transform(E.extract(tlog), E.load_phases(ROOT / "sitl" / "mission_phases.json"), "D0001_S_AM")


def test_extract_reads_the_tlog_and_ignores_the_ground_station(tlog):
    msgs = list(E.extract(tlog))
    kinds = {m["msg"] for m in msgs}
    assert kinds == set(E.WANTED)
    assert all(m["msg"] != "HEARTBEAT" or m["autopilot"] == 3 for m in msgs)  # no GCS heartbeat


def test_one_row_per_second_only_while_armed(rows):
    secs = [int(datetime.strptime(r["timestamp"], E.TS).replace(tzinfo=timezone.utc).timestamp()) - T0
            for r in rows]
    assert secs == sorted(set(secs))                 # unique, increasing
    assert secs[0] >= 5 and secs[-1] <= 126
    assert {60, 61, 62} <= set(secs)                 # short gap filled with the last values
    assert not set(range(100, 110)) & set(secs)      # long gap stays a gap


def test_phases_follow_the_waypoint_being_flown_to(rows):
    order = []
    for r in rows:
        if not order or order[-1] != r["flight_mode"]:
            order.append(r["flight_mode"])
    assert order == ["takeoff", "transit_out", "survey", "transit_back", "landing"]


def test_values_fit_the_contract(rows):
    assert all(r["battery_vtol_pct"] is None for r in rows)       # one pack in SITL
    assert min(r["altitude_m"] for r in rows) == 0.0               # below home clamped
    assert max(r["altitude_m"] for r in rows) == 40.0
    no_fix = [r for r in rows if r["hdop"] == E.NO_FIX_HDOP]
    assert len(no_fix) == 2 and all(r["hdop"] == 0.8 for r in rows if r not in no_fix)
    assert all(r["drone_id"] == "S" and r["mission_id"] == "D0001_S_AM" for r in rows)


def test_csv_and_load_through_the_api(rows, tmp_path):
    out, db = tmp_path / "sitl", tmp_path / "x.db"
    E.write_dataset(rows, out, "D0001_S_AM")
    assert (out / "telemetry_logs.csv").read_text().splitlines()[1].split(",")[4] == ""  # empty VTOL cell
    totals = E.load(out, db)
    assert (totals.sent, totals.accepted, totals.rejected) == (len(rows), len(rows), 0)
    again = E.load(out, db)
    assert (again.accepted, again.duplicates) == (0, len(rows))
    conn = connect(str(db))
    try:
        assert conn.execute("SELECT role, actual_duration_min FROM missions").fetchone()[0] == "AM"
        rule_detector.run(conn)                                    # detectors run on SITL rows
    finally:
        conn.close()


def test_cli_end_to_end(tlog, tmp_path):
    code = E.main(["--tlog", str(tlog), "--mission-id", "D0002_S_PM", "--out", str(tmp_path / "o"),
                   "--phases", str(ROOT / "sitl" / "mission_phases.json"), "--load", "--db", str(tmp_path / "d.db")])
    assert code == 0
    assert E.main(["--tlog", str(tmp_path / "missing.tlog"), "--mission-id", "D0001_S_AM"]) == 2


REAL_FLIGHTS = sorted((ROOT / "sitl" / "flights").glob("*.tlog")) if (ROOT / "sitl" / "flights").exists() else []


@pytest.mark.skipif(not REAL_FLIGHTS, reason="no recorded SITL flight in sitl/flights/")
@pytest.mark.parametrize("flight", REAL_FLIGHTS, ids=lambda p: p.name)
def test_recorded_sitl_flight_loads_cleanly(flight, tmp_path):
    rows = E.transform(E.extract(flight), E.load_phases(ROOT / "sitl" / "mission_phases.json"), "D0001_S_AM")
    assert len(rows) > 60
    assert {"takeoff", "survey", "landing"} <= {r["flight_mode"] for r in rows}
    E.write_dataset(rows, tmp_path, "D0001_S_AM")
    assert E.load(tmp_path, tmp_path / "x.db").rejected == 0
