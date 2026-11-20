"""CSV export of payments and time logs (feature 7).

Both queries are scoped to one user through clients.user_id, like every
other query in the app. Money leaves the database as integer paise and is
turned into a plain rupee number ("1500.00", no ₹ sign or commas) only here,
so spreadsheets read the column as a number.
"""

import csv
import io
import sqlite3

from app.services.payments import METHOD_LABELS


def payment_rows(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Every payment the user has received, newest first."""
    return conn.execute(
        """
        SELECT pay.paid_on, i.invoice_number, c.name AS client_name,
               p.title AS project_title, pay.amount_paise, pay.method, pay.reference
        FROM payments pay
        JOIN invoices i ON i.id = pay.invoice_id
        JOIN projects p ON p.id = i.project_id
        JOIN clients  c ON c.id = p.client_id
        WHERE c.user_id = ?
        ORDER BY pay.paid_on DESC, pay.id DESC
        """,
        (user_id,),
    ).fetchall()


def time_log_rows(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Every finished time log, newest first. A running timer has no
    duration yet, so it is left out until it is stopped."""
    return conn.execute(
        """
        SELECT t.started_at, t.ended_at, t.duration_minutes,
               c.name AS client_name, p.title AS project_title, t.description,
               i.invoice_number                     -- NULL = not billed yet
        FROM time_logs t
        JOIN projects p ON p.id = t.project_id
        JOIN clients  c ON c.id = p.client_id
        LEFT JOIN invoices i ON i.id = t.invoice_id   -- LEFT: keep unbilled logs
        WHERE c.user_id = ?
          AND t.ended_at IS NOT NULL
        ORDER BY t.started_at DESC, t.id DESC
        """,
        (user_id,),
    ).fetchall()


def paise_to_rupees(paise: int) -> str:
    """150050 -> '1500.50'. Integer arithmetic only, never a float."""
    rupees, rem = divmod(paise, 100)
    return f"{rupees}.{rem:02d}"


def safe_cell(value) -> str:
    """Text for one CSV cell.

    Excel and LibreOffice treat a cell starting with = + - or @ as a formula,
    so a client named '=HYPERLINK(...)' could run when the file is opened
    ("CSV injection"). A leading apostrophe makes the cell plain text.
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def to_csv(header: list[str], rows: list[list]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        writer.writerow([safe_cell(v) for v in row])
    return out.getvalue()


def payments_csv(conn: sqlite3.Connection, user_id: int) -> str:
    header = ["paid_on", "invoice_number", "client", "project", "amount_inr", "method", "reference"]
    rows = [
        [r["paid_on"], r["invoice_number"], r["client_name"], r["project_title"],
         paise_to_rupees(r["amount_paise"]), METHOD_LABELS.get(r["method"], r["method"]),
         r["reference"]]
        for r in payment_rows(conn, user_id)
    ]
    return to_csv(header, rows)


def time_logs_csv(conn: sqlite3.Connection, user_id: int) -> str:
    header = ["started_at", "ended_at", "duration_minutes", "hours", "client", "project",
              "description", "invoice_number"]
    rows = [
        [r["started_at"], r["ended_at"], r["duration_minutes"],
         # hours with 2 decimals from integer minutes: 135 -> '2.25'
         f"{r['duration_minutes'] // 60}.{(r['duration_minutes'] % 60) * 100 // 60:02d}",
         r["client_name"], r["project_title"], r["description"], r["invoice_number"]]
        for r in time_log_rows(conn, user_id)
    ]
    return to_csv(header, rows)
