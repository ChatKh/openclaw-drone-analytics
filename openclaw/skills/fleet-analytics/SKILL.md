---
name: fleet-analytics
description: Answer questions about the drone fleet (flights, batteries, GPS, alerts, energy) from the read-only fleet database.
---

# Fleet analytics

Use this skill when the user asks about the drones, their flights, battery use,
GPS quality, alerts from the detectors, or energy per flight phase.

## The only command you may run

Use the `exec` tool to run exactly this program, with one of the subcommands below:

```bash
/home/dev/openclaw-drone-analytics/bin/fleet <subcommand> [options]
```

Do not run any other shell command, do not pipe its output into other programs,
and do not edit files. Every answer must come from this program's JSON output.

## Subcommands (try these first)

| Question | Command |
|---|---|
| Overall picture: flights, hours, alert counts | `fleet summary` |
| Health of each drone | `fleet drones` |
| List flights (optionally one drone, or only those with alerts) | `fleet missions --drone A --with-alerts` |
| One flight in detail with its alerts | `fleet mission D0004_A_AM` |
| Alerts (filter by `--drone`, `--type battery/gps/sensor/lipo`, `--detector rule/ml`, `--mission`) | `fleet alerts --type gps` |
| Alerts where the two detectors disagree | `fleet alerts --disagree` |
| Battery used per flight phase | `fleet energy --drone B` |
| Tables and columns, before writing SQL | `fleet schema` |
| Anything else: one read-only SELECT | `fleet sql "SELECT drone_id, count(*) FROM alerts GROUP BY 1"` |

## Rules for `fleet sql`

- One `SELECT` per call. Writing, `PRAGMA`, `ATTACH` and several statements are refused.
- At most 50 rows come back (`"truncated": true` means there were more). Aggregate
  in SQL (`count`, `avg`, `min`, `max`, `GROUP BY`) instead of fetching raw telemetry.
- Queries longer than 2 seconds are stopped. Add a `WHERE mission_id = ...`.
- Telemetry has one row per second per drone; a flight mission is 90 minutes (5,400 rows).

## Reading the output

- `"ok": false` means a guardrail or a mistake stopped the call. Read `error`, fix the
  command, and try again. Never try to get around a refusal.
- The table `injected_anomalies` (the evaluation answer key) is hidden on purpose.
  You answer the way an operator would, from telemetry and alerts only.
- Detectors: `rule` = hand-written rules, `ml` = model learned from normal flights.
  `other_detector_agrees: false` marks alerts only one detector raised.
- Batteries: `battery_vtol_pct` powers takeoff and landing (hover), `battery_cruise_pct`
  powers forward flight. Percentages are of a full pack.

## How to answer

- Give the number and where it came from (the command you ran).
- Say when a result was truncated or when the data cannot answer the question.
- Reply in the language the user wrote in.
