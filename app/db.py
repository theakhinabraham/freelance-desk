"""Database helpers: open a connection, run the schema, and wrap work in a transaction.

All SQL in this project uses ``?`` placeholders. Values are never pasted into
SQL strings, which is what prevents SQL injection.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"
DEFAULT_DB_PATH = BASE_DIR / "freelancedesk.db"

# India does not use daylight saving, so a fixed +5:30 offset is exact.
IST = timezone(timedelta(hours=5, minutes=30))
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_db_path() -> str:
    """Database file path. Tests point FREELANCEDESK_DB at a temp file."""
    return os.environ.get("FREELANCEDESK_DB", str(DEFAULT_DB_PATH))


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced.

    * isolation_level=None puts sqlite3 in autocommit mode: each single
      statement commits on its own, and multi-statement work is grouped
      explicitly with transaction() below. Nothing is hidden.
    * check_same_thread=False: FastAPI may create the connection in one
      worker thread and use it in another. Each request gets its own
      connection, so it is never shared between two requests.
    """
    conn = sqlite3.connect(
        path or get_db_path(), isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row  # rows can be read as row["column"]
    # Foreign keys are OFF by default in SQLite and must be enabled
    # on every new connection.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables, indexes and triggers (safe to run more than once)."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Run several statements as one all-or-nothing unit.

    BEGIN IMMEDIATE takes the write lock at the start, so two requests
    cannot both read "the last invoice number is 7" and both create #8.
    Any exception rolls everything back.

        with transaction(conn):
            conn.execute("INSERT ...", (...))
            conn.execute("UPDATE ...", (...))
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_db():
    """FastAPI dependency: one connection per request, always closed."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def now_ist() -> datetime:
    """Current IST time as a naive datetime, without microseconds."""
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)


def to_timestamp(dt: datetime) -> str:
    """datetime -> 'YYYY-MM-DD HH:MM:SS' (the format stored in the database)."""
    return dt.strftime(TIMESTAMP_FORMAT)
