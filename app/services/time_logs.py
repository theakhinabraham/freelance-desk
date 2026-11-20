"""Time tracking: start/stop a timer, add past time by hand, and the hour totals.

Timestamps are 'YYYY-MM-DD HH:MM:SS' text in IST. Every function that needs
the current time takes ``now`` as a parameter, so tests can use a fixed clock.

Ownership: time_logs -> projects -> clients.user_id (same chain as projects).
"""

import sqlite3
from datetime import datetime, timedelta

from app.db import now_ist, to_timestamp, transaction


def minutes_between(start: datetime, end: datetime) -> int:
    """Whole minutes, rounded to the nearest minute (30 s or more rounds up)."""
    return int((end - start).total_seconds() + 30) // 60


def _own_project(conn, user_id: int, project_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT p.id, p.title
        FROM projects p
        JOIN clients c ON c.id = p.client_id
        WHERE p.id = ? AND c.user_id = ?
        """,
        (project_id, user_id),
    ).fetchone()


# --- the running timer -------------------------------------------------------

def running_timer(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """The user's running log (ended_at IS NULL), or None.

    Uses the partial index idx_time_logs_running, which holds only running rows.
    """
    return conn.execute(
        """
        SELECT t.id, t.project_id, p.title AS project_title, c.name AS client_name,
               t.started_at, t.description
        FROM time_logs t
        JOIN projects p ON p.id = t.project_id
        JOIN clients  c ON c.id = p.client_id
        WHERE t.ended_at IS NULL AND c.user_id = ?
        """,
        (user_id,),
    ).fetchone()


def start_timer(conn: sqlite3.Connection, user_id: int, project_id: int,
                description: str | None, now: datetime | None = None) -> str | None:
    """Start a timer. Returns an error message, or None on success.

    The check and the INSERT run in one transaction. The trigger
    trg_one_running_timer_insert is the database-level backstop: even if two
    requests raced, the second INSERT would be rejected.
    """
    now = now or now_ist()
    try:
        with transaction(conn):
            if _own_project(conn, user_id, project_id) is None:
                return "Project not found."
            running = running_timer(conn, user_id)
            if running:
                return f"A timer is already running on '{running['project_title']}'. Stop it first."
            conn.execute(
                "INSERT INTO time_logs (project_id, started_at, description) VALUES (?, ?, ?)",
                (project_id, to_timestamp(now), description),
            )
    except sqlite3.IntegrityError:  # raised by the trigger
        return "A timer is already running. Stop it first."
    return None


def stop_timer(conn: sqlite3.Connection, user_id: int,
               now: datetime | None = None) -> tuple[str | None, int | None]:
    """Stop the user's running timer. Returns (error message, minutes logged)."""
    now = now or now_ist()
    with transaction(conn):
        running = running_timer(conn, user_id)
        if running is None:
            return "No timer is running.", None
        started = datetime.fromisoformat(running["started_at"])
        # CHECK (ended_at > started_at): if stopped in the same second it
        # started, end it one second later so the row stays valid.
        ended = max(now, started + timedelta(seconds=1))
        minutes = minutes_between(started, ended)
        conn.execute(
            """
            UPDATE time_logs
            SET ended_at = ?, duration_minutes = ?
            WHERE id = ? AND ended_at IS NULL
            """,
            (to_timestamp(ended), minutes, running["id"]),
        )
    return None, minutes


# --- manual entry ----------------------------------------------------------------

def add_manual_log(conn: sqlite3.Connection, user_id: int, data: dict,
                   now: datetime | None = None) -> str | None:
    """Add a finished log for past work. Returns an error message, or None.

    data: project_id, started (datetime), ended (datetime), description.
    """
    now = now or now_ist()
    started, ended = data["started"], data["ended"]
    if ended <= started:
        return "End time must be after the start time."
    if ended > now:
        return "You cannot log time in the future."

    with transaction(conn):
        if _own_project(conn, user_id, data["project_id"]) is None:
            return "Project not found."
        # Two intervals [s1, e1) and [s2, e2) overlap exactly when
        #     s1 < e2  AND  s2 < e1.
        # A running timer has no end yet, so its end is taken as "now".
        clash = conn.execute(
            """
            SELECT p.title, t.started_at, t.ended_at
            FROM time_logs t
            JOIN projects p ON p.id = t.project_id
            JOIN clients  c ON c.id = p.client_id
            WHERE c.user_id = ?
              AND t.started_at < ?
              AND ? < COALESCE(t.ended_at, ?)
            LIMIT 1
            """,
            (user_id, to_timestamp(ended), to_timestamp(started), to_timestamp(now)),
        ).fetchone()
        if clash:
            until = clash["ended_at"][11:16] if clash["ended_at"] else "now"
            return (f"Overlaps an existing entry on '{clash['title']}' "
                    f"({clash['started_at'][:16]} to {until}).")
        conn.execute(
            """
            INSERT INTO time_logs (project_id, started_at, ended_at, duration_minutes, description)
            VALUES (?, ?, ?, ?, ?)
            """,
            (data["project_id"], to_timestamp(started), to_timestamp(ended),
             minutes_between(started, ended), data["description"]),
        )
    return None


def delete_log(conn: sqlite3.Connection, user_id: int, log_id: int) -> str | None:
    """Delete a mistaken entry. Logs already billed on an invoice are kept."""
    row = conn.execute(
        """
        SELECT t.invoice_id
        FROM time_logs t
        JOIN projects p ON p.id = t.project_id
        JOIN clients  c ON c.id = p.client_id
        WHERE t.id = ? AND c.user_id = ?
        """,
        (log_id, user_id),
    ).fetchone()
    if row is None:
        return "Time entry not found."
    if row["invoice_id"] is not None:
        return "This entry is already billed on an invoice and cannot be deleted."
    conn.execute("DELETE FROM time_logs WHERE id = ? AND invoice_id IS NULL", (log_id,))
    return None


# --- lists and totals --------------------------------------------------------

def list_logs(conn: sqlite3.Connection, user_id: int, project_id: int | None = None,
              limit: int = 50) -> list[sqlite3.Row]:
    """Most recent entries first. Optional project filter as in list_projects."""
    return conn.execute(
        """
        SELECT t.id, t.project_id, p.title AS project_title, c.name AS client_name,
               t.started_at, t.ended_at, t.duration_minutes, t.description,
               t.invoice_id, i.invoice_number
        FROM time_logs t
        JOIN projects p ON p.id = t.project_id
        JOIN clients  c ON c.id = p.client_id
        LEFT JOIN invoices i ON i.id = t.invoice_id   -- LEFT: most logs are unbilled
        WHERE c.user_id = ?
          AND (? IS NULL OR t.project_id = ?)
        ORDER BY t.started_at DESC
        LIMIT ?
        """,
        (user_id, project_id, project_id, limit),
    ).fetchall()


def week_start(day) -> datetime:
    """Monday 00:00 of the week containing ``day``."""
    monday = day - timedelta(days=day.weekday())
    return datetime(monday.year, monday.month, monday.day)


def hours_summary(conn: sqlite3.Connection, user_id: int,
                  now: datetime | None = None) -> sqlite3.Row:
    """Minutes logged today and this week (Monday to now), INCLUDING a running timer.

    The inner query works out each log's minutes. A running log has no
    duration yet, so its minutes so far are (now - started_at). julianday()
    returns days as a fraction, so multiply by 24 * 60 to get minutes.
    The outer query adds them up. CASE puts only today's rows into
    today_minutes.
    A log is counted on the day it STARTED.
    """
    now = now or now_ist()
    today = datetime(now.year, now.month, now.day)
    return conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN started_at >= ? THEN minutes END), 0) AS today_minutes,
               COALESCE(SUM(minutes), 0)                                  AS week_minutes
        FROM (
            SELECT t.started_at,
                   COALESCE(t.duration_minutes,
                            CAST((julianday(?) - julianday(t.started_at)) * 24 * 60 AS INTEGER)
                   ) AS minutes
            FROM time_logs t
            JOIN projects p ON p.id = t.project_id
            JOIN clients  c ON c.id = p.client_id
            WHERE c.user_id = ?
              AND t.started_at >= ?
        )
        """,
        (to_timestamp(today), to_timestamp(now), user_id, to_timestamp(week_start(now))),
    ).fetchone()


def week_by_project(conn: sqlite3.Connection, user_id: int,
                    now: datetime | None = None) -> list[sqlite3.Row]:
    """This week's minutes per project (GROUP BY), finished logs only."""
    now = now or now_ist()
    return conn.execute(
        """
        SELECT p.id, p.title, c.name AS client_name,
               SUM(t.duration_minutes) AS minutes
        FROM time_logs t
        JOIN projects p ON p.id = t.project_id
        JOIN clients  c ON c.id = p.client_id
        WHERE c.user_id = ?
          AND t.started_at >= ?
          AND t.ended_at IS NOT NULL
        GROUP BY p.id
        ORDER BY minutes DESC
        """,
        (user_id, to_timestamp(week_start(now))),
    ).fetchall()


def trackable_projects(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Projects shown in the timer and manual-entry dropdowns: active ones only."""
    return conn.execute(
        """
        SELECT p.id, p.title, c.name AS client_name
        FROM projects p
        JOIN clients c ON c.id = p.client_id
        WHERE c.user_id = ? AND p.status = 'active'
        ORDER BY c.name COLLATE NOCASE, p.title COLLATE NOCASE
        """,
        (user_id,),
    ).fetchall()
