# ArduPilot SITL: the second data source

A QuadPlane flown by the real ArduPilot flight-control code in software-in-the-loop (SITL).
Its MAVLink telemetry log (`.tlog`) goes through `src/etl/sitl_tlog.py` into the same
schema, API and database as the simulator's data. Nothing downstream changes.

| File | What |
|---|---|
| `survey.waypoints` + `mission_phases.json` | short mission: VTOL takeoff to 40 m, one survey loop, VTOL landing (~2.5 min armed) |
| `survey_long.waypoints` + `survey_long.phases.json` | long mission: 8 survey lines of 1000 m, 200 m apart (~12 km, ~9 min armed) |
| `survey_wind.waypoints` + `survey_wind.phases.json` | wind-sweep mission: 12 survey lines (~17.7 km). Cruise stays above the 480 s the battery rule needs |
| `sitl.parm` | parameters on top of ArduPilot's quadplane defaults: the battery monitor on |
| `wind/wind_*.parm` | wind sweep: calm, the top of each flyable band (4.9 / 7.9 / 12 m/s), and 13 m/s; layered after `sitl.parm` |
| `flights/*.tlog` | completed flights, kept in git; the tests run the ETL on each |
| `flights/failed/*.tlog` | flights that did not complete (no lift-off, could not land): kept, they are results too |
| `flights.json` | every flight: mission id, phase map, parameter file, wind, start heading, outcome |
| `RESULTS.md` | the results table, generated from the logs by `python -m src.etl.sitl_rebuild` |

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

Copy the log into `flights/` (or `flights/failed/` if the mission did not complete), add one entry
to `flights.json`, then:

```bash
python -m src.etl.sitl_rebuild
```

It deletes and rebuilds `data/sitl.db` from every flight in `flights.json` (completed ones only),
runs both detectors, writes `data/sitl/<flight>/` (CSV files and `flight_summary.json`) and
regenerates `RESULTS.md`. Running it again gives the same result, so a mistake in between, such as
an old command that deleted the database, is fixed by running it once more. A separate database
keeps SITL flights out of the simulator's dev-set results.

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
4. **Battery drains about 7x faster than the simulator assumes** (long flight: 99 % to 29 % in
   532 s). v2's battery rule had the simulator's drain fixed in the code (0.0136 %/s) and raised a
   false alarm (score 32.9). Its 'before' window also reached into the VTOL takeoff, which drains
   faster still. v3 learns the drain per flight (median drop per 60 s of cruise: 0.10 %/s here,
   0.0137 %/s on simulated flights) and compares only windows inside cruise.
   Caveat: that needs 480 s of cruise and this flight had 445 s, so under v3 the battery rule made
   no check at all here. Its 0 false alarms on these flights is not a pass; the wind sweep tests it.
5. **Steadier than any simulated flight** (long flight). No wind and almost no sensor noise:
   `speed_std` went down to 0.02 m/s against at least 0.154 in normal simulated flights. The
   two-sided envelope flagged 18 of 42 windows as 8 'sensor' alerts, all for being *below* normal.
   A guess that the 180-degree turns caused them was wrong: the turn windows were the least steady.
   v3: one-sided envelope (each feature alarms only on the side a fault pushes it).

| Flight | Rules v2 | Rules v3 | ML v2 | ML v3 |
|---|---|---|---|---|
| short (158 s armed) | 0 | 0 (battery rule not run) | 1, then 0 after finding 2 | 0 |
| long (532 s armed) | 1 battery | 0 (battery rule not run) | 8 sensor | 0 |

Findings from the wind sweep (all numbers in [`RESULTS.md`](RESULTS.md); one flight per speed, so
each is a direction, not a calibration). These were **not** fixed: the detectors stay v3, and any
change would be v4 with its own new test set.

6. **A QuadPlane must take off facing the wind.** At 7.9 m/s with SITL's default start heading
   (353 deg, wind from 180 deg, so from behind) it never got above 0.3 m in 11 minutes at full lift
   throttle: a "Potential VTOL Thrust Loss" emergency at 18.6 s, 64 % of the battery gone. With one
   change, the start heading set to face the wind, the same settings flew the whole mission. Why the
   tail wind holds it down (airflow over the wing from behind) is our reading, not measured.
7. **Altitude takes 30-50 s to settle after the transition in wind.** At 7.9 m/s the first three
   windows after "Transition done" wobbled more than any simulated flight (`alt_std` 2.9 m against
   2.18, `alt_max_dev` 7.6 m against 5.9): the ML detector's one false alarm on that flight.
8. **Ground speed carries the wind.** At 12 m/s ground speed swings with heading (tailwind and
   headwind legs between crosswind survey lines): `speed_std` up to 8.2 against 4.2 in normal
   simulated flights, in 31 of 78 windows, as 12 ML 'sensor' false alarms about 50 s apart, one per
   survey line. The simulator models wind as noise around a constant speed, never tied to heading.
   Airspeed (`VFR_HUD.airspeed`) would separate wind from faults; not in the schema yet.
9. **ArduPilot's wind estimate is accurate but smooth, and reads high in strong wind.** Median 4.9
   and 7.9 m/s for 4.9 and 7.9 set; 12.4 and 13.4 for 12 and 13, with gusts of 30 % barely visible
   (maximum at most 0.3 above the median). A 'return above 12 m/s' trigger on its 30 s mean would
   have fired 55 s into the 12 m/s flight, the top of a band the drone should still fly, and 29 s
   into the 13 m/s one. A trigger needs a margin for the estimate, or separate warn and return levels.
10. **Landing gets harder with wind.** Touchdown 0.50 / 0.46 / 0.67 / 1.93 m/s at 0 / 4.9 / 7.9 / 12
    m/s; the landing took 61 / 68 / 71 / 203 s; during the back-transition the aircraft climbed to
    45 / 55 / 62 / 59 m from a 40 m mission. At 12 and 13 m/s ArduPilot switched EKF lanes ("EKF3 lane
    switch"), a sign its estimator was working hard.
11. **At 13 m/s the vertical landing never got down.** 23 minutes of "Land descend", climbing slowly
    to 172 m, 18 % of the battery used in 25 minutes armed (the 7.9 m/s no-lift-off flight used 64 %
    in 11). Little battery and a climb point to the wing lifting the aircraft while it hovers into a
    13 m/s wind; that explanation is ours, not measured. If the policy above 12 m/s is to land at
    once, a vertical landing may not be available.

| Wind | Flight | Rules v3 alerts | ML v3 alerts | Battery-rule checks |
|---|---|---|---|---|
| 0 | completed | 0 | 0 | 35 |
| 4.9 | completed | 0 | 0 | 40 |
| 7.9 | completed (facing the wind) | 0 | 1 | 48 |
| 12 | completed | 0 | 12 | 65 |

The rules raised nothing from calm to 12 m/s with the battery rule actually checking; the ML
envelope knows only the weather it was trained on, and at the top of the flyable range that is
not enough.

### What the wind model does not cover

- SITL applies the same turbulence to the vertical axis as to the horizontal (`SIM_Aircraft::update_wind`),
  so 30 % gusts are 30 % vertical gusts too; real vertical gusts are usually weaker.
- `SIM_WIND_T 1` puts full wind speed at ground level; real wind is weaker near the ground.
- The airframe is SITL's generic QuadPlane, not the planned X8, and nothing models structural damage:
  touchdown speed is the closest measure of "landing without damage".

## Wind sweep

The drone's operating bands: 0 to below 5 m/s flies for sure, 5 to below 8 needs a skilled pilot,
8 to 12 may crash but is still flyable, above 12 it crashes for sure, so the policy is to return or
land at once. Each flyable band is tested at its **upper edge**, the worst case still inside it.
Above 12 m/s no survey should be flown, so that flight is short and asks only whether ArduPilot's
own wind estimate shows the limit was crossed, and how fast. ArduPlane has no built-in wind-speed
failsafe (searched its source); a return trigger would be ours to add, in the drone project.

| File | Wind | Band | Mission | Mission id |
|---|---|---|---|---|
| `wind/wind_00.parm` | 0 m/s | calm baseline (flown first) | `survey_wind` | `D0010_S_AM` |
| `wind/wind_4.9.parm` | 4.9 m/s | top of 0 to below 5 | `survey_wind` | `D0011_S_AM` |
| `wind/wind_7.9.parm` | 7.9 m/s | top of 5 to below 8 | `survey_wind` | `D0012_S_AM` |
| `wind/wind_12.parm` | 12 m/s | top of 8 to 12 | `survey_wind` | `D0013_S_AM` |
| `wind/wind_13.parm` | 13 m/s | above 12: wind-estimate check only | `survey` (short) | `D0014_S_AM` |

The speeds were changed from one-per-band values (4 / 7 / 10 / 13) to the band edges after the calm
flight and before any windy one: no windy result had been seen.

Every file: gusts (`SIM_WIND_TURB`, about the standard deviation) 30 % of the mean, wind from the
south, full speed at every height, a 10000 mAh pack so the long mission does not run the battery flat.
These are settings of the test, not of the detectors, and nothing is tuned after the sweep: the
results are reported as they come out. What SITL can and cannot say: it runs the real autopilot
against a simple wind model and an ideal airframe, so it shows how the detectors and the autopilot
behave, not whether the real drone survives the wind.

```bash
W=7.9   # 00, 4.9, 7.9, 12, 13
mkdir -p ~/sitl-runs/wind_$W && cd ~/sitl-runs/wind_$W
~/ardupilot/Tools/autotest/sim_vehicle.py -v ArduPlane -f quadplane --console --map -w \
    -l -35.363261,149.165230,584,180 \
    --add-param-file=/home/dev/openclaw-drone-analytics/sitl/sitl.parm \
    --add-param-file=/home/dev/openclaw-drone-analytics/sitl/wind/wind_$W.parm
```

`-l ...,180` starts the aircraft facing the wind (finding 6). The 0 and 4.9 m/s flights were flown
before that finding, at the default heading; both completed.

In MAVProxy: `param show SIM_WIND_SPD` (must match), `wp load .../sitl/survey_wind.waypoints`
(13 m/s: `survey.waypoints`), `wp list` (28 items; 8 for the short one), `mode AUTO`, `arm throttle`.
Wait for `Land complete` and `Throttle disarmed` before Ctrl+C: `Land descend started` is only the
start of the descent. Then copy `mav.tlog` into `flights/` as `survey_wind_$W.tlog`, add it to
`flights.json` and run `python -m src.etl.sitl_rebuild`.

The ETL also writes `flight_summary.json`: touchdown speed (SITL's "SIM Hit ground" message), how
far from take-off it landed, battery used, any warning ArduPilot raised, and ArduPilot's wind estimate
(`WIND` message) from forward flight on: median, maximum, seconds above 12 m/s, and when its 30 s
mean first passed 12 m/s, which is what a return trigger that ignores single gusts would have seen.
On the calm flights the estimate stayed at 0.1-0.2 m/s. Before forward flight there is no estimate
(ArduPilot reads 0 on the ground), so a flight that never transitioned shows none.
