"""Invoices: numbering, pre-filled amounts, creation, status changes, and the overdue refresh.

Ownership chain: invoices -> projects -> clients.user_id.

Status lifecycle:
    draft --send--> sent --(past due_date)--> overdue
    sent / overdue --(payments cover amount)--> paid      (see services/payments.py)
    draft / sent / overdue --cancel (no payments)--> cancelled
"""

import sqlite3

from app.db import transaction


def next_invoice_number(conn: sqlite3.Connection, year: int) -> str:
    """Return the next number for ``year``, e.g. FD-2026-0008.

    MUST be called inside db.transaction(): BEGIN IMMEDIATE holds the write
    lock, so no other request can insert an invoice between this SELECT and
    the caller's INSERT. The UNIQUE constraint on invoice_number is a
    second line of defence.
    """
    prefix = f"FD-{year}-"
    # 'FD-2026-0007' -> substr(..., 9) = '0007' -> CAST -> 7.
    # Cast to INTEGER before MAX so that the numbers compare as numbers.
    row = conn.execute(
        """
        SELECT MAX(CAST(substr(invoice_number, 9) AS INTEGER))
        FROM invoices
        WHERE invoice_number LIKE ?
        """,
        (prefix + "%",),
    ).fetchone()
    last = row[0] or 0
    return f"{prefix}{last + 1:04d}"


def hourly_amount(minutes: int, rate_paise: int) -> int:
    """minutes x rate per hour, in integer paise, rounded to the nearest paisa."""
    return (minutes * rate_paise + 30) // 60


def refresh_overdue(conn: sqlite3.Connection, user_id: int, today: str) -> int:
    """Mark this user's 'sent' invoices whose due date has passed as 'overdue'.

    There is no background job, so this runs before invoice pages and the
    dashboard are shown. It is idempotent: running it twice changes nothing
    the second time. WHERE status = 'sent' AND due_date < ? matches the
    composite index idx_invoices_status_due exactly.
    Returns the number of invoices changed.
    """
    cur = conn.execute(
        """
        UPDATE invoices
        SET status = 'overdue'
        WHERE status = 'sent'
          AND due_date < ?
          AND project_id IN (SELECT p.id
                             FROM projects p
                             JOIN clients c ON c.id = p.client_id
                             WHERE c.user_id = ?)
        """,
        (today, user_id),
    )
    return cur.rowcount


def list_invoices(conn: sqlite3.Connection, user_id: int, status: str | None = None,
                  project_id: int | None = None) -> list[sqlite3.Row]:
    """Invoices with paid amount and balance.

    The payments are added up in a derived table (the subquery "pay") BEFORE
    the join, so each invoice joins to at most one row and cannot be counted
    twice. LEFT JOIN keeps invoices that have no payments yet; COALESCE turns
    their NULL total into 0.
    """
    return conn.execute(
        """
        SELECT i.id, i.invoice_number, i.issue_date, i.due_date, i.amount_paise, i.status,
               p.id AS project_id, p.title AS project_title, p.billing_type,
               p.hourly_rate_paise,
               c.id AS client_id, c.name AS client_name, c.email AS client_email,
               c.phone AS client_phone,
               COALESCE(pay.paid_paise, 0)                  AS paid_paise,
               i.amount_paise - COALESCE(pay.paid_paise, 0) AS balance_paise
        FROM invoices i
        JOIN projects p ON p.id = i.project_id
        JOIN clients  c ON c.id = p.client_id
        LEFT JOIN (SELECT invoice_id, SUM(amount_paise) AS paid_paise
                   FROM payments
                   GROUP BY invoice_id) pay ON pay.invoice_id = i.id
        WHERE c.user_id = ?
          AND (? IS NULL OR i.status = ?)
          AND (? IS NULL OR i.project_id = ?)
        ORDER BY i.issue_date DESC, i.id DESC
        """,
        (user_id, status, status, project_id, project_id),
    ).fetchall()


def get_invoice(conn: sqlite3.Connection, user_id: int, invoice_id: int) -> sqlite3.Row | None:
    """One invoice (same columns as list_invoices), or None if it is not the user's."""
    return conn.execute(
        """
        SELECT i.id, i.invoice_number, i.issue_date, i.due_date, i.amount_paise, i.status,
               p.id AS project_id, p.title AS project_title, p.billing_type,
               p.hourly_rate_paise,
               c.id AS client_id, c.name AS client_name, c.email AS client_email,
               c.phone AS client_phone,
               COALESCE(pay.paid_paise, 0)                  AS paid_paise,
               i.amount_paise - COALESCE(pay.paid_paise, 0) AS balance_paise
        FROM invoices i
        JOIN projects p ON p.id = i.project_id
        JOIN clients  c ON c.id = p.client_id
        LEFT JOIN (SELECT invoice_id, SUM(amount_paise) AS paid_paise
                   FROM payments
                   GROUP BY invoice_id) pay ON pay.invoice_id = i.id
        WHERE i.id = ? AND c.user_id = ?
        """,
        (invoice_id, user_id),
    ).fetchone()


def billed_logs(conn: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    """Time logs included in an invoice (call only after get_invoice checked ownership)."""
    return conn.execute(
        """
        SELECT started_at, ended_at, duration_minutes, description
        FROM time_logs
        WHERE invoice_id = ?
        ORDER BY started_at
        """,
        (invoice_id,),
    ).fetchall()


def invoice_payments(conn: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    """Payments on an invoice (call only after get_invoice checked ownership)."""
    return conn.execute(
        """
        SELECT id, amount_paise, paid_on, method, reference
        FROM payments
        WHERE invoice_id = ?
        ORDER BY paid_on, id
        """,
        (invoice_id,),
    ).fetchall()


def invoice_prefill(conn: sqlite3.Connection, project: sqlite3.Row, cutoff: str) -> dict:
    """The suggested amount for a new invoice on ``project``.

    hourly: unbilled, finished hours (ended at or before ``cutoff``) x rate.
    fixed:  the fixed price minus what earlier non-cancelled invoices already billed.
    """
    if project["billing_type"] == "hourly":
        row = conn.execute(
            """
            SELECT COUNT(*) AS log_count, COALESCE(SUM(duration_minutes), 0) AS minutes
            FROM time_logs
            WHERE project_id = ?
              AND invoice_id IS NULL         -- not billed yet
              AND ended_at IS NOT NULL       -- skip a running timer
              AND ended_at <= ?
            """,
            (project["id"], cutoff),
        ).fetchone()
        return {
            "log_count": row["log_count"],
            "minutes": row["minutes"],
            "amount_paise": hourly_amount(row["minutes"], project["hourly_rate_paise"]),
        }

    already = conn.execute(
        """
        SELECT COALESCE(SUM(amount_paise), 0)
        FROM invoices
        WHERE project_id = ? AND status <> 'cancelled'
        """,
        (project["id"],),
    ).fetchone()[0]
    return {
        "already_invoiced_paise": already,
        "amount_paise": max(project["fixed_price_paise"] - already, 0),
    }


def create_invoice(conn: sqlite3.Connection, user_id: int, project_id: int,
                   data: dict, cutoff: str) -> tuple[int | None, str | None]:
    """Create an invoice and, for hourly projects, mark its time logs as billed.

    Returns (invoice_id, error). Everything happens in ONE transaction:
      1. check the project belongs to the user
      2. work out the next invoice number (safe because of BEGIN IMMEDIATE)
      3. INSERT the invoice
      4. UPDATE the unbilled logs (up to ``cutoff``) to point at it
    If any step fails, nothing is saved, so no hours are ever marked billed
    without an invoice.
    """
    with transaction(conn):
        project = conn.execute(
            """
            SELECT p.id, p.billing_type
            FROM projects p
            JOIN clients c ON c.id = p.client_id
            WHERE p.id = ? AND c.user_id = ?
            """,
            (project_id, user_id),
        ).fetchone()
        if project is None:
            return None, "Project not found."

        number = next_invoice_number(conn, int(data["issue_date"][:4]))
        cur = conn.execute(
            """
            INSERT INTO invoices (project_id, invoice_number, issue_date, due_date,
                                  amount_paise, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, number, data["issue_date"], data["due_date"],
             data["amount_paise"], data["status"]),
        )
        invoice_id = cur.lastrowid

        if project["billing_type"] == "hourly":
            # The same filter as invoice_prefill, so the hours on the invoice
            # are exactly the hours the user saw on the form.
            conn.execute(
                """
                UPDATE time_logs
                SET invoice_id = ?
                WHERE project_id = ?
                  AND invoice_id IS NULL
                  AND ended_at IS NOT NULL
                  AND ended_at <= ?
                """,
                (invoice_id, project_id, cutoff),
            )
    return invoice_id, None


def mark_sent(conn: sqlite3.Connection, user_id: int, invoice_id: int, today: str) -> str | None:
    """draft -> sent (or straight to overdue if the due date has already passed)."""
    with transaction(conn):
        invoice = get_invoice(conn, user_id, invoice_id)
        if invoice is None:
            return "Invoice not found."
        if invoice["status"] != "draft":
            return "Only draft invoices can be marked as sent."
        conn.execute(
            "UPDATE invoices SET status = 'sent' WHERE id = ? AND status = 'draft'", (invoice_id,)
        )
        refresh_overdue(conn, user_id, today)
    return None


def cancel_invoice(conn: sqlite3.Connection, user_id: int, invoice_id: int) -> str | None:
    """Cancel an unpaid invoice and release its hours (they become unbilled again)."""
    with transaction(conn):
        invoice = get_invoice(conn, user_id, invoice_id)
        if invoice is None:
            return "Invoice not found."
        if invoice["status"] in ("paid", "cancelled"):
            return f"A {invoice['status']} invoice cannot be cancelled."
        if invoice["paid_paise"] > 0:
            return "This invoice has payments recorded, so it cannot be cancelled."
        conn.execute("UPDATE invoices SET status = 'cancelled' WHERE id = ?", (invoice_id,))
        conn.execute("UPDATE time_logs SET invoice_id = NULL WHERE invoice_id = ?", (invoice_id,))
    return None
