"""
SQLite access layer.

- DB path: env DRONE_DB_PATH, default data/drone.db (git-ignored).
- Foreign keys are OFF by default in SQLite, per connection. connect() turns
  them on every time; forgetting this lets bad references in silently.
"""
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterator

log = logging.getLogger("db")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = "data/drone.db"


def db_path() -> str:
    return os.environ.get("DRONE_DB_PATH", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI may run the dependency and the endpoint
    # on different worker threads. Each request still gets its own connection.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA_VERSION = 2


def init_db(conn: sqlite3.Connection) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    _migrate(conn, schema)
    conn.commit()
    log.debug("schema ready (%s)", SCHEMA_PATH.name)


def _migrate(conn: sqlite3.Connection, schema: str) -> None:
    """v1 -> v2: battery_vtol_pct becomes nullable.

    SQLite cannot drop NOT NULL in place, so the table is rebuilt from the v2
    statement in schema.sql and every row is copied, in one transaction.
    A read-only connection (dashboard, agent) leaves an old database as it is:
    v1 rows are all valid v2 rows, so reading works either way.
    """
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(telemetry_logs)")}
    if not cols.get("battery_vtol_pct") or cols["battery_vtol_pct"][3] == 0:  # [3] = notnull
        return
    create = re.search(r"CREATE TABLE IF NOT EXISTS telemetry_logs \(.*?\n\);", schema, re.S).group(0)
    index = re.search(r"CREATE INDEX IF NOT EXISTS idx_telemetry_mission[^;]*;", schema).group(0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP INDEX IF EXISTS idx_telemetry_mission")
        conn.execute("ALTER TABLE telemetry_logs RENAME TO telemetry_logs_v1")
        conn.execute(create)
        moved = conn.execute("INSERT INTO telemetry_logs SELECT * FROM telemetry_logs_v1").rowcount
        conn.execute("DROP TABLE telemetry_logs_v1")
        conn.execute(index)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
        log.warning("migrated telemetry_logs to schema v%d (%d rows kept)", SCHEMA_VERSION, moved)
    except sqlite3.OperationalError as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if "readonly" in str(e):
            log.info("read-only connection: v1 table left as it is")
            return
        raise


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, schema ensured."""
    conn = connect()
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
