"""
fleet: the only door an AI agent (OpenClaw) has into the drone database (Phase 7).

The agent never gets a shell into the data. It runs this one program, and every
answer comes back as JSON. The guardrails live HERE, in code we test, so they hold
no matter which LLM drives the agent or how its config is set:

  1. read-only file      the database is opened with mode=ro
  2. SELECT only         a SQLite authorizer denies every action except reading
                         (no INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA/...)
  3. no answer key       reading injected_anomalies is denied: the agent answers
                         like an operator would, without knowing the planted faults
  4. bounded work        at most MAX_ROWS rows and TIME_LIMIT_S seconds per query
  5. audit trail         every call is appended to logs/agent_queries.jsonl

Commands (all print JSON):
  fleet summary                              flights, hours, alerts per detector
  fleet drones                               health per drone
  fleet missions [--drone A] [--with-alerts] one row per flight
  fleet mission D0004_A_AM                   one flight: summary + its alerts
  fleet alerts [--drone] [--type] [--detector] [--mission] [--disagree]
  fleet energy [--drone A]                   battery used per flight phase
  fleet schema                               tables and columns the agent may query
  fleet sql "SELECT ..."                     any read-only query, within the limits

Run:  bin/fleet summary      (or python -m src.agent.fleet summary)
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.analytics.fleet_health import FLEET_HEALTH_SQL, MISSION_SUMMARY_SQL
from src.dashboard import queries as Q
from src.db.database import db_path

MAX_ROWS = 50   # small enough for a local model's context; aggregate in SQL instead
TIME_LIMIT_S = 2.0
HIDDEN_TABLES = {"injected_anomalies"}  # the answer key
AUDIT_LOG = Path(os.environ.get("FLEET_AUDIT_LOG", "logs/agent_queries.jsonl"))

EXIT_OK, EXIT_REFUSED, EXIT_NO_DATA, EXIT_BUG = 0, 1, 2, 3


class Refused(Exception):
    """The request broke a guardrail. The message says which, so the agent can adjust."""


# ---------- guarded connection ----------

class _Authorizer:
    """SQLite calls this for every action while preparing a statement. It remembers why it said no."""

    def __init__(self):
        self.denied: str | None = None

    def __call__(self, action, arg1, arg2, dbname, source):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):  # RECURSIVE is bounded by the time limit
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if arg1 in HIDDEN_TABLES:
                self.denied = "the answer key (injected_anomalies) is hidden from the agent"
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and arg2 != "load_extension":
            return sqlite3.SQLITE_OK
        self.denied = "only reading (one SELECT) is allowed"
        return sqlite3.SQLITE_DENY


class GuardedConnection(sqlite3.Connection):
    guard: _Authorizer


def connect_guarded(path: str | None = None) -> GuardedConnection:
    path = path or db_path()
    if not Path(path).exists():
        raise Q.NoDatabase(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, factory=GuardedConnection)
    conn.row_factory = sqlite3.Row
    conn.guard = _Authorizer()
    conn.set_authorizer(conn.guard)
    return conn


def _hidden_rootpages(conn) -> set[int]:
    """B-tree pages of the hidden tables and their indexes."""
    marks = ",".join("?" * len(HIDDEN_TABLES))
    return {r[0] for r in conn.execute(
        f"SELECT rootpage FROM sqlite_master WHERE tbl_name IN ({marks}) AND rootpage > 0", tuple(HIDDEN_TABLES))}


def _touches_hidden_table(conn, sql: str) -> bool:
    """The authorizer only sees COLUMN reads, so `SELECT count(*) FROM injected_anomalies` slips past it.
    EXPLAIN shows every table the statement will open (OpenRead <cursor> <rootpage>), columns or not."""
    hidden = _hidden_rootpages(conn)
    return any(r[1] in ("OpenRead", "OpenWrite", "ReopenIdx") and r[3] in hidden
               for r in conn.execute("EXPLAIN " + sql))


def _with_deadline(conn: sqlite3.Connection, seconds: float = TIME_LIMIT_S):
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)


def run_sql(conn: GuardedConnection, sql: str, max_rows: int = MAX_ROWS) -> dict:
    """One read-only statement. Anything a guardrail stops comes back as Refused with the reason."""
    conn.guard.denied = None
    _with_deadline(conn)
    try:
        if _touches_hidden_table(conn, sql):
            raise Refused("the answer key (injected_anomalies) is hidden from the agent")
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows + 1)
    except (sqlite3.ProgrammingError, sqlite3.Warning) as e:
        # "SELECT 1; DELETE ...": Python 3.10 raises Warning, 3.12+ ProgrammingError (a DatabaseError)
        raise Refused(f"one statement per call: {e}") from e
    except sqlite3.DatabaseError as e:
        if conn.guard.denied:
            raise Refused(conn.guard.denied) from e
        if "interrupted" in str(e):
            raise Refused(f"query took longer than {TIME_LIMIT_S} s; aggregate or add a WHERE") from e
        raise Refused(f"SQL error: {e}") from e
    finally:
        conn.set_progress_handler(None, 0)
    cols = [d[0] for d in cur.description] if cur.description else []
    return {"columns": cols, "rows": [dict(zip(cols, r)) for r in rows[:max_rows]],
            "truncated": len(rows) > max_rows}


# ---------- fixed commands (the agent's first choice) ----------

def _records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso"))


def cmd_summary(conn, args) -> dict:
    return {"summary": Q.overview(conn)}


def cmd_drones(conn, args) -> dict:
    return {"drones": _records(pd.read_sql_query(FLEET_HEALTH_SQL, conn))}


def cmd_missions(conn, args) -> dict:
    df = Q.mission_list(conn)
    if args.drone:
        df = df[df["drone_id"] == args.drone]
    if args.with_alerts:
        df = df[(df["rule_alerts"] > 0) | (df["ml_alerts"] > 0)]
    return {"missions": _records(df)}


def cmd_mission(conn, args) -> dict:
    summary = pd.read_sql_query(f"SELECT * FROM ({MISSION_SUMMARY_SQL}) WHERE mission_id = ?",
                                conn, params=(args.mission_id,))
    if summary.empty:
        raise Refused(f"no flight mission {args.mission_id!r}; try `fleet missions`")
    alerts = Q.mission_alerts(conn, args.mission_id)
    for c in ("start_time", "end_time"):
        alerts[c] = alerts[c].dt.strftime("%Y-%m-%d %H:%M:%S")
    return {"mission": _records(summary)[0], "alerts": _records(alerts)}


def cmd_alerts(conn, args) -> dict:
    df = Q.alerts_with_agreement(conn)
    for col, val in (("drone_id", args.drone), ("anomaly_type", args.type),
                     ("detector", args.detector), ("mission_id", args.mission)):
        if val:
            df = df[df[col] == val]
    if args.disagree:
        df = df[~df["other_detector_agrees"]]
    total = len(df)
    return {"alerts": _records(df.head(MAX_ROWS)), "total": total, "truncated": total > MAX_ROWS,
            "note": "other_detector_agrees = the other detector flagged the same type within 30 s"}


def cmd_energy(conn, args) -> dict:
    df = Q.energy_by_phase(conn)
    if args.drone:
        df = df[df["drone_id"] == args.drone]
    df = df.assign(flight_mode=df["flight_mode"].astype(str))
    return {"energy": _records(df), "note": "average % of each battery used per flight phase"}


def cmd_schema(conn, args) -> dict:
    tables = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if name in HIDDEN_TABLES:
            continue
        tables[name] = [d[0] for d in conn.execute(f'SELECT * FROM "{name}" LIMIT 0').description]
    return {"tables": tables, "limits": {"max_rows": MAX_ROWS, "time_limit_s": TIME_LIMIT_S},
            "timestamps": "TEXT 'YYYY-MM-DD HH:MM:SS', telemetry is 1 row per second"}


def cmd_sql(conn, args) -> dict:
    return run_sql(conn, args.query)


COMMANDS = {"summary": cmd_summary, "drones": cmd_drones, "missions": cmd_missions,
            "mission": cmd_mission, "alerts": cmd_alerts, "energy": cmd_energy,
            "schema": cmd_schema, "sql": cmd_sql}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fleet", description="Read-only drone fleet data for agents (JSON out)")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON for people")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("summary")
    sub.add_parser("drones")
    m = sub.add_parser("missions")
    m.add_argument("--drone")
    m.add_argument("--with-alerts", action="store_true")
    one = sub.add_parser("mission")
    one.add_argument("mission_id")
    a = sub.add_parser("alerts")
    a.add_argument("--drone")
    a.add_argument("--type", choices=["battery", "gps", "sensor", "lipo"])
    a.add_argument("--detector", choices=["rule", "ml"])
    a.add_argument("--mission")
    a.add_argument("--disagree", action="store_true", help="only alerts the other detector did not raise")
    e = sub.add_parser("energy")
    e.add_argument("--drone")
    sub.add_parser("schema")
    s = sub.add_parser("sql")
    s.add_argument("query")
    return ap


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # auditing must never break an answer


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "command": args.command,
             "args": {k: v for k, v in vars(args).items() if k not in ("command", "pretty") and v not in (None, False)}}
    try:
        conn = connect_guarded()
        try:
            out, code = {"ok": True, **COMMANDS[args.command](conn, args)}, EXIT_OK
        finally:
            conn.close()
    except Q.NoDatabase as e:
        out, code = {"ok": False, "error": f"no database at {e}; build it first (see README)"}, EXIT_NO_DATA
    except Refused as e:
        out, code = {"ok": False, "error": str(e)}, EXIT_REFUSED
    except Exception as e:  # noqa: BLE001  the agent must always get JSON back, never a traceback
        out, code = {"ok": False, "error": f"internal error ({type(e).__name__}); this is a bug in fleet"}, EXIT_BUG
    entry.update(ok=out["ok"], ms=round((time.monotonic() - started) * 1000),
                 error=out.get("error"), rows=len(out.get("rows", [])) if "rows" in out else None)
    _audit(entry)
    print(json.dumps(out, ensure_ascii=False, indent=1 if args.pretty else None, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
