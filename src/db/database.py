"""
SQLite access layer.

- DB path: env DRONE_DB_PATH, default data/drone.db (git-ignored).
- Foreign keys are OFF by default in SQLite, per connection. connect() turns
  them on every time; forgetting this lets bad references in silently.
"""
import logging
import os
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


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    log.debug("schema ready (%s)", SCHEMA_PATH.name)


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, schema ensured."""
    conn = connect()
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
