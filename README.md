# OpenClaw Drone Analytics

[![CI](https://github.com/ChatKh/openclaw-drone-analytics/actions/workflows/ci.yml/badge.svg)](https://github.com/ChatKh/openclaw-drone-analytics/actions/workflows/ci.yml)

Fault analytics for a small drone fleet, built so that **detection quality can be measured**.

A simulator flies three VTOL survey drones and plants faults in their telemetry (battery,
GPS, altitude sensor, VTOL pack). The planted faults go into an answer key that the detectors
never see. Two detectors, hand-written rules and a model learned from normal flights, raise
alerts. An evaluator compares the alerts with the answer key and reports precision, recall
and false alarms per flight hour. CI fails the build if those numbers drop.

It is the same loop as a production test station: known-bad units, a test that does not know
which ones they are, and a yield report that has to hold up.

> ภาษาไทย: [README.th.md](README.th.md)

## Results

### v2 on the frozen test set (seed 7: 30 days, 90 flight hours, 16 planted faults)

Every threshold was chosen on a separate dev set (or on normal flights), then the test set
was scored once.

| Detector | Precision | Recall | False alarms | per flight hour |
|---|---|---|---|---|
| Rules (v2) | 94% | 100% | 1 (altitude alert in strong wind) | 0.01 |
| ML: learned normal envelope | 100% | 94% | 0 | 0.00 |
| ML: Isolation Forest alone | 0% | 0% | 4 | 0.04 |

### v3 after the ArduPilot SITL flights

Flights from the real ArduPilot flight-control code ([below](#second-data-source-ardupilot-sitl))
showed two assumptions that only held for the simulator, so the detectors changed (v3). The seed 7
test set has now been seen, so v3 was scored once on a **new test set (seed 31: 30 days,
60 flights, 24 planted faults)**, after v3 was frozen in commit `499c09f`.

| | Dev set (seed 42) | SITL, calm (2 flights) | New test set (seed 31) |
|---|---|---|---|
| Rules v2 | 100% / 100% | 1 false alarm (battery) | not scored |
| Rules v3 | 100% / 100%, same alerts | 0 false alarms, but the battery rule could not run* | **100% / 100%**, 0 false alarms |
| ML v2 envelope | 92% / 86% | 8 false alarms (all "too steady") | not scored |
| ML v3 envelope | 92% / 86%, same alerts | 0 false alarms | **100% / 96%**, 0 false alarms |

Precision / recall. The one v3 miss on seed 31 is the same kind as on seed 7: ML missed a small
altitude spike (25.1 m against a 20 m cruise), which the rules caught. 24 faults is still a small
sample, so 100% means "none missed here", not "never misses". \*v3 compares battery windows only
inside cruise, which needs 480 s of it; the longer calm flight had 445 s. The wind-sweep flights
are long enough. On 40 maximum-wind normal simulated flights v2 and v3 raise the same false
alarms: 5 flights (3 battery, 2 altitude).

![Recall by fault type, rules vs ML](docs/rule_vs_ml.svg)

What the numbers say:

- **The two detectors miss different things.** Rules caught a small altitude spike the ML
  model missed; the ML model raised no false alarm in the strongest wind, where the rules raised one.
- **Isolation Forest alone caught nothing.** Its score saturates outside the training range:
  a 23 m altitude spike scored lower than an ordinary gusty window. A per-feature envelope
  learned from normal flights measures *how far* outside normal a value is, and fixed it.
- **16 faults is a small sample.** One fault moves recall by 6 points; read the table as a
  direction, not field accuracy.

## Quick start

With Docker (nothing else to install):

```bash
git clone https://github.com/ChatKh/openclaw-drone-analytics.git
cd openclaw-drone-analytics
docker compose up --build
```

| Service | What it does | Open |
|---|---|---|
| `api` | ingestion API (FastAPI) | http://127.0.0.1:8000/docs |
| `pipeline` | simulate → send through the API → detectors → evaluate, then exits | logs |
| `dashboard` | read-only Streamlit dashboard, starts after the pipeline succeeds | http://127.0.0.1:8501 |

Without Docker (Python 3.10+):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                 # 200 tests
python -m src.pipeline --fresh      # whole pipeline on the dev set, ~15 s (+40 s the first time to train ML)
streamlit run src/dashboard/app.py
```

## Architecture

```mermaid
flowchart LR
  sim["Simulator<br/>3 drones, wind, GPS multipath,<br/>planted faults"]
  key["answer key<br/>(injected_anomalies)"]
  api["Ingestion API<br/>FastAPI + Pydantic,<br/>validated row by row"]
  db[("SQLite")]
  rule["Rule detector"]
  ml["ML detector<br/>trained on normal flights only"]
  ev["Evaluator + results gate<br/>the only reader of the answer key"]
  dash["Dashboard<br/>read-only"]
  fleet["bin/fleet<br/>read-only, guarded"]
  agent["OpenClaw agent<br/>local LLM"]

  sim -- "telemetry CSV, HTTP batches" --> api --> db
  sim -- "loaded by a separate script" --> key --> db
  db --> rule -- alerts --> db
  db --> ml -- alerts --> db
  db --> ev
  db -. read-only .-> dash
  db -. read-only .-> fleet --> agent
```

The database schema is the contract. The simulator is one data source; a flight-controller
simulator (ArduPilot SITL) or real logs can replace it without touching anything downstream.

## How the measurement stays honest

- **Detectors cannot see the answer key.** It is loaded by its own script, never through the
  API; the missions table has no "was this one faulted" column; a test fails if any file in
  `src/analytics/` mentions the answer-key table; only `src/evaluation/` reads it.
- **The data is not too clean.** About 35% of flights fly in gusty wind (altitude wobble,
  battery readings that sag and recover, extra VTOL use) and 30% hit GPS multipath under tree
  canopy. None of that is a fault. The first rules scored 100% on clean data and fell to
  78% precision / 50% recall once these were added, which forced rules that look at the
  *shape* of a signal, not just its size.
- **Faults straddle the thresholds.** Each fault's size is random within a range that crosses
  the detection limit, and the real size is stored with it, so misses can be explained.
- **Tune on dev, score test once.** Dev: seed 42, 15 days, 14 faults. Test: seed 7, 30 days,
  16 faults, frozen until the end. When the SITL flights changed the detectors, seed 7 had been
  seen, so v3 gets a fresh test set (seed 31) instead of a second look at the old one.
- **Matching rule.** An alert counts when it is the same mission and fault type and its time
  window overlaps the planted fault within ±30 s.

## Detectors

**Rules** (`src/analytics/rule_detector.py`, thresholds in `RuleConfig`)

| Fault | Rule |
|---|---|
| Battery | the highest cruise-battery reading in a 180 s window *after* a point is >5% below the highest reading in the 180 s window *before* it, beyond this flight's normal drain. Using the highest reading ignores gust sag, which recovers. v3: the normal drain is learned from the flight (median drop per 60 s of cruise), not fixed, and both windows must lie inside cruise. |
| GPS | fewer than 6 satellites or HDOP above 2.5, held for at least 45 s (multipath lasts 5–25 s) |
| Altitude sensor | one reading far from its neighbours, measured against how much the neighbours themselves wobble (score > 14 and > 3 m) |
| VTOL pack | VTOL battery below 52% when the return leg starts (landing costs about 40%) |

Thresholds come from one-at-a-time sweeps on the dev set (`src/evaluation/sweep.py`), taken
from the middle of the range where every fault is caught and nothing false fires, not from its
edge. They are tuned to this simulator, not taken from hardware specifications.

**ML** (`src/analytics/ml_detector.py`): 30 s cruise windows, 9 plain statistics per window
(altitude spread, speed spread, GPS quality, battery slope and largest one-second drop, VTOL
level). Trained on 100 simulated *normal* flights; thresholds set on 100 more normal flights so
that at most 1% of them raise anything. It never sees a fault or the answer key. v3: each
feature alarms on one side only, the side a fault pushes it (more wobble, worse HDOP, fewer
satellites); a flight steadier than normal is not a fault.

## Second data source: ArduPilot SITL

The same QuadPlane mission flown by ArduPilot's real flight-control code in software-in-the-loop.
`src/etl/sitl_tlog.py` turns its MAVLink log into the same schema, so the API, database,
detectors and dashboard run unchanged. Eight recorded flights: six completed (`sitl/flights/`) and
two that did not (`sitl/flights/failed/`), kept because they are results too.

What it found that the simulator's own data could not (details and numbers in
[`sitl/README.md`](sitl/README.md#findings-log)):

1. ArduPilot's quadplane defaults leave the battery monitor off: 0 rows until `sitl.parm` turned it on.
2. ArduPilot switches to the next waypoint while the QuadPlane is still accelerating on its lift
   motors; labelled cruise, that transition was the ML detector's only alert. The ETL now keeps it as takeoff.
3. A one-second lag in the ETL, found by the test written for 2.
4. The battery drains about 7x faster than the simulator's; the rule's fixed drain raised a false alarm. (v3: learned per flight.)
5. With no wind and no sensor noise the flight was steadier than any simulated one; the two-sided
   envelope flagged 18 of 42 windows. (v3: one-sided.)

### Wind sweep: calm to 13 m/s

The drone is meant to fly in up to 12 m/s and return or land above that. Each flyable band was
flown at its upper edge (4.9 / 7.9 / 12 m/s) after a calm flight, plus a short 13 m/s flight;
speeds fixed before any windy flight, nothing tuned after. One flight per speed, so each result is a
direction. Full table: [`sitl/RESULTS.md`](sitl/RESULTS.md), generated from the logs by
`python -m src.etl.sitl_rebuild`.

| Wind (m/s) | Outcome | Touchdown (m/s) | Rules v3 alerts | ML v3 alerts |
|---|---|---|---|---|
| 0 | completed | 0.50 | 0 | 0 |
| 4.9 | completed | 0.46 | 0 | 0 |
| 7.9, tail to the wind | never lifted off (0.3 m in 11 min) | - | - | - |
| 7.9, facing the wind | completed | 0.67 | 0 | 1 |
| 12 | completed, landing took 203 s | 1.93 | 0 | 12 |
| 13 | could not land: climbed to 172 m in 23 min of descent | - | - | - |

6. A QuadPlane has to take off facing the wind: one change, the start heading, turned "never
   lifted off" into a completed mission.
7. In wind, altitude takes 30-50 s to settle after the transition (the 7.9 m/s ML alert).
8. Ground speed carries the wind: at 12 m/s it swings with heading, and the ML envelope, trained on
   the simulator's milder weather, raised 12 false alarms, one per survey line. The rules raised
   none from 0 to 12 m/s, with the battery rule checking 35-65 times per flight.
9. ArduPilot's wind estimate is close (4.9, 7.9) but reads 0.4 m/s high at 12 and 13 m/s and hides
   gusts. A "return above 12 m/s" trigger on its 30 s mean would have aborted the 12 m/s flight
   after 55 s: a trigger needs a margin, or separate warn and return levels.
10. Touchdown speed rises with wind: about 4x calm at 12 m/s.
11. At 13 m/s the vertical landing never got down, using little battery: most likely the wing lifting
    the hovering aircraft. If the rule above 12 m/s is "land at once", a vertical landing may not be
    available.

Findings 7 and 8 were not fixed: a change would be detector v4, scored on yet another new test set.

## The AI agent, and why its guardrails live in code

An OpenClaw agent answers fleet questions ("how many alerts did each detector raise?") using a
local model (qwen3:4b through Ollama). It cannot open a shell into the data. It can run exactly
one program, `bin/fleet`, which returns JSON and enforces its own limits:

| Layer | Stops |
|---|---|
| SQLite opened read-only | any write to the database file |
| SQLite authorizer | everything except `SELECT` (INSERT, DELETE, DROP, ATTACH, PRAGMA, load_extension) |
| Query-plan check (`EXPLAIN`) | any read of the answer key, including `count(*)`, which the authorizer alone lets through |
| Limits | more than 50 rows or 2 seconds per query |
| Audit log | nothing silently: every call is logged |

On the OpenClaw side, the defaults let the agent run any shell command without asking. They
were locked to one tool (`exec`) and an allowlist with one entry (`bin/fleet`).

Tested on the running agent:

| Asked to | Result | Evidence |
|---|---|---|
| run `ls ~` | blocked | `denied: allowlist miss` in the session trajectory |
| run `ls /home/dev` (a workaround the agent suggested itself) | blocked | |
| show `~/.openclaw/openclaw.json` (holds a token) | not readable | no file-reading tool |
| "how many flights, how many alerts per detector?" | correct: 30 / 14 / 13 | audit log shows `fleet summary` |

The guardrails held every time; the model's explanations of *why* it was blocked were wrong
every time. Safety has to sit outside the model.

## Engineering decisions

| Decision | Why |
|---|---|
| Keep the answer key in its own table, out of reach of detectors | Without it there is no precision or recall, only a demo |
| Add normal disturbances to the simulator | Clean data made every detector look perfect and taught nothing |
| Validate format at ingestion, not faults | A 45 m altitude reading is valid data; deciding it is wrong is the detector's job |
| One bad row never fails a batch | Good rows are stored, bad rows come back with their line and reason |
| Learn ML thresholds from normal flights only | Tuning on the answer key would grade the model on its own homework |
| Keep the Isolation Forest result in the report | The failure and its cause are more useful than a quiet swap |
| Readers (dashboard, agent) open the database read-only | A bug in a viewer must not be able to change data |
| Fail loudly | The pipeline exits non-zero when counts do not add up; CI fails when results drop |
| Add a second data source before trusting the detectors | Data from real flight-control code broke two assumptions the simulator could never have shown |
| New test set after changing the detectors | A second look at a seen test set would make the score a tuning result |
| Keep the flights that failed, and rebuild results from logs | A table typed from memory drifts; one command regenerates `sitl/RESULTS.md` from the committed logs |

## Testing and CI

200 tests: API validation, database constraints, simulator, each detector on cases with
known answers, evaluator, dashboard pages (including empty data), agent guardrails with 14
attack queries, and an integration test that runs the whole system on three simulated days
and checks every part reports the same numbers.

On every push GitHub Actions runs the tests on Python 3.10, runs the full dev pipeline
through a **results gate** (fails below rules 95%/95% or ML 85%/80%), and runs the pipeline
inside `docker compose`.

## Limitations

- Synthetic data. The detectors are tuned to this simulator's idea of a fault; real faults
  will look different. SITL runs real flight-control code, but its airframe, wind and sensors
  are models too, and its flights so far carry no planted fault.
- Small numbers: 14 dev and 16 test faults (24 on the v3 test set); one SITL flight per wind speed.
- SITL's wind model applies the same turbulence vertically as horizontally and puts full wind speed
  at ground level; its QuadPlane is a generic airframe with no structural model.
- The ML envelope only knows the weather it was trained on: at 12 m/s in SITL it raised a false
  alarm on every survey line (ground speed swinging with heading).
- Strong wind can still trip the rules: the one test-set false alarm was an altitude alert at
  near-maximum wind, and in a stress run 5 of 40 maximum-wind normal flights raised a false
  alert (3 battery, 2 altitude), the same with v2 and v3. (An earlier version of this README
  called all 5 battery alerts; re-running the stress test showed the split.)
- A rules + ML combination looks promising but has not been tested on a fresh data set.
- The local model takes minutes per answer on a 4 GB laptop GPU; a cloud model comparison is
  still to do.

## Repository layout

```
src/simulator/    fleet, weather and fault injection
src/api/          FastAPI ingestion (POST /telemetry/batch)
src/db/           SQLite schema and access
src/ingest/       loaders and the telemetry sender
src/analytics/    fleet health, rule detector, ML detector
src/evaluation/   evaluator, threshold sweep, rule vs ML comparison, results gate
src/dashboard/    Streamlit pages and their SQL
src/agent/        bin/fleet: the agent's read-only CLI
src/pipeline.py   the whole pipeline in one command
src/etl/          ArduPilot SITL MAVLink log -> the same telemetry schema; sitl_rebuild
sitl/             SITL missions, parameter files, wind sweep, recorded flights, RESULTS.md
openclaw/         agent workspace instructions and skill
tests/            pytest
```

Stack: Python 3.10, FastAPI, Pydantic, SQLite, scikit-learn, Streamlit, Docker Compose,
GitHub Actions, OpenClaw + Ollama.
