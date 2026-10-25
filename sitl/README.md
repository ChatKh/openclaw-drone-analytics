# ArduPilot SITL: the second data source

A QuadPlane flown by the real ArduPilot flight-control code in software-in-the-loop (SITL).
Its MAVLink telemetry log (`.tlog`) goes through `src/etl/sitl_tlog.py` into the same
schema, API and database as the simulator's data. Nothing downstream changes.

| File | What |
|---|---|
| `survey.waypoints` + `mission_phases.json` | short mission: VTOL takeoff to 40 m, one survey loop, VTOL landing (~2.5 min armed) |
| `survey_long.waypoints` + `survey_long.phases.json` | long mission: 8 survey lines of 1000 m, 200 m apart (~12 km, ~11 min armed). Long enough for the battery rule, which needs 2 x (180 + 60) s of data |
| `sitl.parm` | parameters on top of ArduPilot's quadplane defaults: the battery monitor on |
| `flights/*.tlog` | recorded flights, kept in git; the tests run the ETL on them |

| Flight | Mission | Mission id | Phase map |
|---|---|---|---|
| `flights/survey_short.tlog` | `survey.waypoints` | `D0001_S_AM` | `mission_phases.json` |
| `flights/survey_long.tlog` | `survey_long.waypoints` | `D0002_S_AM` | `survey_long.phases.json` |

## Fly

ArduPilot installed in `~/ardupilot` (`Tools/environment_install/install-prereqs-ubuntu.sh -y`, then
`./waf configure --board sitl && ./waf plane`). Run SITL in a terminal **without** the project's
`.venv`: `sim_vehicle.py` uses the first `python3` on PATH, and the venv has none of its packages.

```bash
mkdir -p ~/sitl-runs/flight3 && cd ~/sitl-runs/flight3
~/ardupilot/Tools/autotest/sim_vehicle.py -v ArduPlane -f quadplane --console --map -w \
    --add-param-file=/home/dev/openclaw-drone-analytics/sitl/sitl.parm
```

`-w` resets every parameter to the defaults, then `sitl.parm` is applied on top, so each flight
starts from the same settings. Without it the battery monitor is off and the ETL gets no rows
(`battery_remaining` = -1 throughout; the ETL says so). MAVProxy printing
`Flight battery 100 percent` shows the monitor is on.

When the console says `pre-arm good`, type in the MAVProxy prompt (the terminal, not the console window):

```
wp load /home/dev/openclaw-drone-analytics/sitl/survey_long.waypoints
wp list
mode AUTO
arm throttle
```

`wp list` must show every item (20 for the long mission). The QuadPlane takes off vertically,
transitions to forward flight, flies the survey, transitions back and lands. After
`Throttle disarmed`, stop SITL with Ctrl+C. MAVProxy wrote `mav.tlog` in the folder you started from.

## Into the pipeline

A separate database, so SITL flights do not change the simulator's dev-set results:

```bash
cd ~/openclaw-drone-analytics
cp ~/sitl-runs/flight3/mav.tlog sitl/flights/survey_long.tlog
python -m src.etl.sitl_tlog --tlog sitl/flights/survey_long.tlog --mission-id D0002_S_AM \
    --phases sitl/survey_long.phases.json --out data/sitl_long --load --db data/sitl.db
DRONE_DB_PATH=data/sitl.db python -m src.analytics.rule_detector
DRONE_DB_PATH=data/sitl.db python -m src.analytics.ml_detector detect
```

These flights have no planted fault, so every alert is a false alarm: a direct measure of how well
thresholds tuned on the simulator carry over to the real flight-control code.

## Mapping decisions

| Schema field | From MAVLink | Note |
|---|---|---|
| `battery_vtol_pct` | none | SITL simulates one pack: NULL (schema v2); the VTOL-reserve rule skips it |
| `battery_cruise_pct` | `SYS_STATUS.battery_remaining` | whole percent; needs `BATT_MONITOR` on (`sitl.parm`) |
| `altitude_m` | `GLOBAL_POSITION_INT.relative_alt` | metres above home; below home clamped to 0 |
| `ground_speed_mps` | `VFR_HUD.groundspeed` | |
| `satellite_count`, `hdop` | `GPS_RAW_INT` | no fix -> hdop 99.99 |
| `flight_mode` | `MISSION_CURRENT.seq` + `STATUSTEXT` | phase of the waypoint flown toward, but `takeoff` until the vehicle sends "Transition done" |
| timestamp | ground-station receive time | UTC, one row per armed second with the values at the end of that second; gaps up to 5 s filled |

## Findings log

What real flight-control code showed that the simulator's own data could not. Each one
changed the code or the setup, and has a test.

1. **Battery monitor off by default** (first flight, 2026-10-25). ArduPilot's quadplane defaults
   leave `BATT_MONITOR` at 0: `battery_remaining` was -1 in all 2,870 `SYS_STATUS` messages and
   the ETL produced 0 rows. Fix: `sitl.parm`; the ETL now names the missing input.
2. **The transition is not cruise** (second flight). ArduPilot moves to the next waypoint as
   soon as the VTOL climb reaches 40 m, while the QuadPlane is still accelerating on its lift
   motors. Labelled `transit_out`, that window was the ML detector's only alert: `speed_std` 6.5
   against at most 4.2 in normal simulated flights. Our phases say VTOL power is used only in
   takeoff and landing, so the ETL now keeps `takeoff` until "Transition done". Both results are
   reported: before and after the relabel.
3. **One-second lag** (found by the test written for 2). A row was built when the next second's
   first message arrived, before that second's messages were applied, so every row carried the
   previous second's values. Fixed; `test_values_belong_to_their_own_second`.
4. **Battery drains about 7x faster than the simulator assumes**: cruise slope ~0.10 %/s with
   ArduPilot's default 3300 mAh, against `CRUISE_DRAIN_PER_S` = 0.0136 %/s in the battery rule.
   The short flight is too short for the battery rule to run at all (159 s of data, it needs 480 s);
   the long mission tests it with the pack unchanged.
