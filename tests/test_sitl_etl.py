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


TRANSITION_AT = 25  # waypoint 2 is current from 21 s; forward flight from 25 s


def write_tlog(path: Path, battery_monitor: bool = True, transition_at: int | None = TRANSITION_AT) -> None:
    """130 s: disarmed 0-4 s, armed 5-125 s, disarmed after. Gaps at 60-62 s (short) and 100-109 s (long).

    battery_monitor=False: SYS_STATUS as ArduPilot sends it with BATT_MONITOR 0 (remaining -1, 0 V).
    transition_at: second of the vehicle's "Transition done" (None: no such message)."""
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
        volts, remaining = (12_600, 100 - s // 10) if battery_monitor else (0, -1)
        put(t + 0.40, vehicle, vehicle.sys_status_encode(0, 0, 0, 0, volts, 1000, remaining,
                                                         0, 0, 0, 0, 0, 0))
        if armed:
            put(t + 0.50, vehicle, vehicle.mission_current_encode(seq_at(s)))
        if s == 22:
            put(t + 0.60, gcs, gcs.statustext_encode(6, b"Transition done"))      # not the vehicle
        if s == transition_at:
            put(t + 0.60, vehicle, vehicle.statustext_encode(6, b"Transition done"))
        if s == 80:
            put(t + 0.70, vehicle, vehicle.statustext_encode(2, b"EKF3 IMU0 yaw inconsistent"))
        if s == 124:
            put(t + 0.70, vehicle, vehicle.statustext_encode(6, b"SIM Hit ground at 0.480000 m/s"))
        if s == 128:
            put(t + 0.70, vehicle, vehicle.statustext_encode(2, b"PreArm: In landing sequence"))
        put(t + 0.80, vehicle, vehicle.wind_encode(180.0, 8.0 if s < 60 else 14.0, 0.0))   # estimate jumps at 60 s
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


def second_of(row) -> int:
    return int(datetime.strptime(row["timestamp"], E.TS).replace(tzinfo=timezone.utc).timestamp()) - T0


def test_transition_counts_as_takeoff_until_the_vehicle_says_done(rows):
    first_cruise = next(r for r in rows if r["flight_mode"] == "transit_out")
    assert second_of(first_cruise) == TRANSITION_AT          # not 21 (waypoint 2), not 22 (GCS text)
    assert all(r["flight_mode"] == "takeoff" for r in rows if second_of(r) < TRANSITION_AT)


def test_without_transition_messages_phases_follow_the_waypoints(tmp_path, caplog):
    path = tmp_path / "no_transition.tlog"
    write_tlog(path, transition_at=None)
    # the GCS text at 22 s is dropped by extract, so this log has no transition message at all
    rows = E.transform(E.extract(path), E.load_phases(ROOT / "sitl" / "mission_phases.json"), "D0001_S_AM")
    assert second_of(next(r for r in rows if r["flight_mode"] == "transit_out")) == 21
    assert "no 'Transition done'" in caplog.text


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


def test_battery_monitor_off_gives_no_rows_and_says_why(tmp_path, caplog):
    # The first real SITL flight (2026-10-25): quadplane defaults leave BATT_MONITOR off.
    path = tmp_path / "no_battery.tlog"
    write_tlog(path, battery_monitor=False)
    msgs = list(E.extract(path))
    assert E.transform(msgs, E.load_phases(ROOT / "sitl" / "mission_phases.json"), "D0001_S_AM") == []
    assert any("BATT_MONITOR" in r for r in E.diagnose(msgs))
    code = E.main(["--tlog", str(path), "--mission-id", "D0001_S_AM", "--out", str(tmp_path / "o"),
                   "--phases", str(ROOT / "sitl" / "mission_phases.json")])
    assert code == 1 and "BATT_MONITOR" in caplog.text


def test_a_good_log_has_nothing_to_diagnose(tlog):
    assert E.diagnose(list(E.extract(tlog))) == []


def test_parameter_file_turns_the_battery_monitor_on():
    params = dict(line.split()[:2] for line in (ROOT / "sitl" / "sitl.parm").read_text().splitlines()
                  if line.strip() and not line.startswith("#"))
    assert params["BATT_MONITOR"] == "4" and int(params["BATT_CAPACITY"]) > 0


def test_flight_summary_reports_the_landing(tlog, rows):
    s = E.flight_summary(list(E.extract(tlog)), rows)
    assert s["touchdown_mps"] == [0.48]
    assert s["transition_done_s"] == pytest.approx(TRANSITION_AT - 5, abs=1)  # armed from 5 s
    assert s["warnings"] == ["EKF3 IMU0 yaw inconsistent"]             # PreArm after landing left out
    assert s["armed_s"] == len(rows) and s["battery_pct"]["end"] < s["battery_pct"]["start"]
    assert s["landed_from_takeoff_m"] == pytest.approx(120 * 1e-5 * 111_320, rel=0.02)  # drifted north


def test_wind_estimate_and_when_the_return_trigger_would_fire(tlog, rows):
    w = E.flight_summary(list(E.extract(tlog)), rows)["wind_estimate"]
    # from forward flight (25.6 s): 8 m/s until 59 s, 14 m/s from 63 s (no messages 60-62 s).
    # The 30 s mean first passes 12 m/s when 21 of its 30 seconds read 14: at 83 s.
    assert w["max_mps"] == 14.0 and w["median_mps"] == 14.0
    assert w["30s_mean_over_limit_after_s"] == pytest.approx(83 - TRANSITION_AT - 0.6, abs=0.01)
    assert w["seconds_over_limit"] == (99 - 63 + 1) + (126 - 110 + 1)
    assert w["limit_mps"] == E.WIND_LIMIT_MPS == 12.0


def test_no_wind_messages_no_wind_estimate():
    assert E.wind_estimate([], 0, 100) is None


def test_no_forward_flight_no_wind_estimate(tmp_path):
    # the 7.9 m/s tail-to-wind flight never left the ground; ArduPilot's estimate there reads 0
    path = tmp_path / "no_transition.tlog"
    write_tlog(path, transition_at=None)
    msgs = list(E.extract(path))
    rows = E.transform(msgs, E.load_phases(ROOT / "sitl" / "mission_phases.json"), "D0001_S_AM")
    assert E.flight_summary(msgs, rows)["wind_estimate"] is None


def test_cli_writes_the_flight_summary(tlog, tmp_path):
    assert E.main(["--tlog", str(tlog), "--mission-id", "D0001_S_AM", "--out", str(tmp_path),
                   "--phases", str(ROOT / "sitl" / "mission_phases.json")]) == 0
    import json
    assert json.loads((tmp_path / "flight_summary.json").read_text())["touchdown_mps"] == [0.48]


# The drone's bands: 0-<5, 5-<8, 8-12 m/s each tested at its upper edge after a calm flight; above 12 it
# must return or land, so 13 m/s is a short flight that checks only the wind estimate.
WIND = {"00": 0, "4.9": 4.9, "7.9": 7.9, "12": 12, "13": 13}
WIND_MISSION = {"00": "survey_wind", "4.9": "survey_wind", "7.9": "survey_wind", "12": "survey_wind", "13": "survey"}


def read_parm(path):
    return dict(line.split()[:2] for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("#"))


@pytest.mark.parametrize("name,speed", WIND.items())
def test_wind_files(name, speed):
    p = read_parm(ROOT / "sitl" / "wind" / f"wind_{name}.parm")
    assert float(p["SIM_WIND_SPD"]) == speed
    assert float(p["SIM_WIND_TURB"]) == pytest.approx(0.3 * speed)      # same gustiness for every speed
    assert p["SIM_WIND_DIR"] == "180" and p["SIM_WIND_T"] == "1"        # same direction, full speed at 40 m
    assert int(p["BATT_CAPACITY"]) == 10000                             # same pack for every speed
    assert f"sitl/{WIND_MISSION[name]}.waypoints" in (ROOT / "sitl" / "wind" / f"wind_{name}.parm").read_text()


def test_wind_folder_holds_exactly_the_planned_flights():
    assert sorted(p.stem for p in (ROOT / "sitl" / "wind").glob("*.parm")) == sorted(f"wind_{w}" for w in WIND)


def test_wind_mission_is_long_enough_for_the_battery_rule():
    # cruise must exceed 2 x (180 + 60) = 480 s; the SITL QuadPlane cruised at ~25.7 m/s
    import math
    items = [line.split("\t") for line in (ROOT / "sitl" / "survey_wind.waypoints").read_text().splitlines()[1:]]
    home = (float(items[0][8]), float(items[0][9]))
    pts = [home] + [(float(i[8]), float(i[9])) for i in items[2:-1]] + [home]
    metres = sum(math.hypot((b[0] - a[0]) * 111_320, (b[1] - a[1]) * 111_320 * math.cos(math.radians(a[0])))
                 for a, b in zip(pts, pts[1:]))
    assert metres / 25.7 > 600


MISSIONS = {  # mission file -> its phase map
    "survey.waypoints": "mission_phases.json",
    "survey_long.waypoints": "survey_long.phases.json",
    "survey_wind.waypoints": "survey_wind.phases.json",
}
PHASE_ORDER = ["takeoff", "transit_out", "survey", "transit_back", "landing"]


@pytest.mark.parametrize("mission", MISSIONS)
def test_mission_and_phase_map_agree(mission):
    lines = (ROOT / "sitl" / mission).read_text().splitlines()
    assert lines[0] == "QGC WPL 110"
    items = [line.split("\t") for line in lines[1:]]
    assert [int(i[0]) for i in items] == list(range(len(items)))
    assert items[1][3] == "84" and items[-1][3] == "85"          # VTOL takeoff first, VTOL land last
    phases = E.load_phases(ROOT / "sitl" / MISSIONS[mission])
    order = []
    for seq in range(1, len(items)):
        p = E.phase_for(seq, phases)
        if not order or order[-1] != p:
            order.append(p)
    assert order == PHASE_ORDER


def test_values_belong_to_their_own_second(rows):
    # each row carries the values received during its second, not the second before (fixed 2026-10-26)
    by_sec = {second_of(r): r for r in rows}
    assert by_sec[20]["battery_cruise_pct"] == 98.0 and by_sec[19]["battery_cruise_pct"] == 99.0
    assert by_sec[5]["flight_mode"] == "takeoff"                     # the first armed second has a row


import json

FLOWN = [f for f in json.loads((ROOT / "sitl" / "flights.json").read_text())["flights"] if f["status"] == "flown"]


@pytest.mark.parametrize("flight", FLOWN, ids=lambda f: Path(f["file"]).name)
def test_recorded_sitl_flight_loads_cleanly(flight, tmp_path):
    """Every completed flight in sitl/flights.json: full phase order, battery reported, loads cleanly."""
    msgs = list(E.extract(ROOT / "sitl" / flight["file"]))
    assert any(E.is_transition_done(m) for m in msgs)              # QuadPlane: phases use the transition
    rows = E.transform(msgs, E.load_phases(ROOT / "sitl" / flight["phases"]), flight["mission_id"])
    assert len(rows) > 60
    order = []
    for r in rows:
        if not order or order[-1] != r["flight_mode"]:
            order.append(r["flight_mode"])
    assert order == PHASE_ORDER
    assert rows[-1]["battery_cruise_pct"] < rows[0]["battery_cruise_pct"]   # battery monitor was on
    assert E.flight_summary(msgs, rows)["touchdown_mps"]                     # it landed
    E.write_dataset(rows, tmp_path, flight["mission_id"])
    assert E.load(tmp_path, tmp_path / "x.db").rejected == 0
