# ArduPilot SITL: the second data source

A QuadPlane flown by the real ArduPilot flight-control code in software-in-the-loop (SITL).
Its MAVLink telemetry log (`.tlog`) goes through `src/etl/sitl_tlog.py` into the same
schema, API and database as the simulator's data. Nothing downstream changes.

| File | What |
|---|---|
| `survey.waypoints` | the mission: VTOL takeoff to 40 m, a short two-leg survey, VTOL landing at home (~4 min) |
| `mission_phases.json` | which flight phase each leg is (by the waypoint being flown toward) |
| `sitl.parm` | parameters on top of ArduPilot's quadplane defaults: the battery monitor on |
| `flights/*.tlog` | recorded flights, small enough to keep in git; CI runs the ETL on them |

## Fly once

ArduPilot installed in `~/ardupilot` (`Tools/environment_install/install-prereqs-ubuntu.sh -y`, then
`./waf configure --board sitl && ./waf plane`).

```bash
mkdir -p ~/sitl-runs/flight1 && cd ~/sitl-runs/flight1
~/ardupilot/Tools/autotest/sim_vehicle.py -v ArduPlane -f quadplane --console --map -w \
    --add-param-file=/home/dev/openclaw-drone-analytics/sitl/sitl.parm
```

`-w` resets every parameter to the defaults, then `sitl.parm` is applied on top, so each flight
starts from the same settings. Without it the battery monitor is off and the ETL gets no rows
(`battery_remaining` = -1 throughout; the ETL says so). Check with `param show BATT_MONITOR` (4).

Wait until the console says the EKF is using GPS (about a minute), then type in the MAVProxy prompt:

```
wp load /home/dev/openclaw-drone-analytics/sitl/survey.waypoints
mode AUTO
arm throttle
```

The QuadPlane takes off vertically, transitions to forward flight, flies the survey, transitions
back and lands. When it has disarmed after landing, stop SITL with Ctrl+C. MAVProxy wrote
`mav.tlog` in the folder you started from.

## Into the pipeline

Use a separate database, so the SITL flight does not change the simulator's dev-set results:

```bash
cd ~/openclaw-drone-analytics
cp ~/sitl-runs/flight1/mav.tlog sitl/flights/survey_short.tlog
python -m src.etl.sitl_tlog --tlog sitl/flights/survey_short.tlog --mission-id D0001_S_AM \
    --out data/sitl --load --db data/sitl.db
DRONE_DB_PATH=data/sitl.db python -m src.analytics.rule_detector
DRONE_DB_PATH=data/sitl.db python -m src.analytics.ml_detector detect
```

This flight has no planted fault, so every alert is a false alarm: a direct measure of how well
thresholds tuned on the simulator carry over to the real flight-control code.

## Mapping decisions

| Schema field | From MAVLink | Note |
|---|---|---|
| `battery_vtol_pct` | none | SITL simulates one pack: NULL (schema v2); the VTOL-reserve rule skips it |
| `battery_cruise_pct` | `SYS_STATUS.battery_remaining` | |
| `altitude_m` | `GLOBAL_POSITION_INT.relative_alt` | metres above home; below home clamped to 0 |
| `ground_speed_mps` | `VFR_HUD.groundspeed` | |
| `satellite_count`, `hdop` | `GPS_RAW_INT` | no fix -> hdop 99.99 |
| `flight_mode` | `MISSION_CURRENT.seq` | via `mission_phases.json` |
| timestamp | ground-station receive time | UTC, one row per armed second; gaps up to 5 s filled |
