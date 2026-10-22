"""Shared test setup: every test gets its own empty SQLite file."""
import pytest

from src.db.database import connect, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DRONE_DB_PATH", path)
    conn = connect(path)
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def seeded_db(temp_db):
    """Three drones and two missions, enough for the API tests."""
    temp_db.executemany(
        "INSERT INTO drones VALUES (?, 'VTOL-QuadPlane-X8', 'v1.0', 100, 'camera', 90, 'active')",
        [("A",), ("B",), ("C",)],
    )
    temp_db.executemany(
        "INSERT INTO missions VALUES (?, ?, ?, ?, ?, 90, 90, 'completed')",
        [
            ("D0001_A_AM", "A", "AM", "2026-06-01 08:00:00", "2026-06-01 09:29:59"),
            ("D0001_B_PM", "B", "PM", "2026-06-01 15:00:00", "2026-06-01 16:29:59"),
        ],
    )
    temp_db.commit()
    return temp_db
