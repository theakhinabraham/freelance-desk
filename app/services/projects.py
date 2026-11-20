"""Project queries.

projects has no user_id column (that would break 3NF), so ownership is
checked through the client:  projects.client_id -> clients.user_id.
"""

import sqlite3

from app.db import transaction

def list_projects(conn: sqlite3.Connection, user_id: int,
                  status: str | None = None, client_id: int | None = None) -> list[sqlite3.Row]:
    """Optional filters without building SQL strings:
    "(? IS NULL OR p.status = ?)" is TRUE for every row when the filter is
    None, and otherwise keeps only the matching rows. So the SQL text is the
    same for every request and only the parameters change.
    """
    return conn.execute(
        """
        SELECT p.id, p.client_id, c.name AS client_name, p.title, p.billing_type,
               p.hourly_rate_paise, p.fixed_price_paise, p.estimated_hours, p.status,
               p.start_date, p.deadline, p.created_at
        FROM projects p
        JOIN clients c ON c.id = p.client_id
        WHERE c.user_id = ?
          AND (? IS NULL OR p.status = ?)
          AND (? IS NULL OR p.client_id = ?)
        ORDER BY CASE p.status
                   WHEN 'active'    THEN 0
                   WHEN 'on_hold'   THEN 1
                   WHEN 'completed' THEN 2
                   ELSE 3
                 END,
                 p.created_at DESC, p.id DESC
        """,
        (user_id, status, status, client_id, client_id),
    ).fetchall()


def get_project(conn: sqlite3.Connection, user_id: int, project_id: int) -> sqlite3.Row | None:
    """Returns None if the project does not exist OR belongs to another user."""
    return conn.execute(
        """
        SELECT p.id, p.client_id, c.name AS client_name, p.title, p.billing_type,
               p.hourly_rate_paise, p.fixed_price_paise, p.estimated_hours, p.status,
               p.start_date, p.deadline, p.created_at
        FROM projects p
        JOIN clients c ON c.id = p.client_id
        WHERE p.id = ? AND c.user_id = ?
        """,
        (project_id, user_id),
    ).fetchone()


def _client_belongs_to_user(conn, user_id: int, client_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM clients WHERE id = ? AND user_id = ?", (client_id, user_id)
    ).fetchone()
    return row is not None


def create_project(conn: sqlite3.Connection, user_id: int, data: dict) -> int | None:
    """Returns the new id, or None if client_id is not one of the user's clients."""
    if not _client_belongs_to_user(conn, user_id, data["client_id"]):
        return None
    cur = conn.execute(
        """
        INSERT INTO projects (client_id, title, billing_type, hourly_rate_paise,
                              fixed_price_paise, estimated_hours, status, start_date, deadline)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (data["client_id"], data["title"], data["billing_type"], data["hourly_rate_paise"],
         data["fixed_price_paise"], data["estimated_hours"], data["status"],
         data["start_date"], data["deadline"]),
    )
    return cur.lastrowid


def update_project(conn: sqlite3.Connection, user_id: int, project_id: int, data: dict) -> bool:
    """Both the project AND the (possibly new) client must belong to the user."""
    if not _client_belongs_to_user(conn, user_id, data["client_id"]):
        return False
    # The subquery limits the UPDATE to projects owned by this user.
    cur = conn.execute(
        """
        UPDATE projects
        SET client_id = ?, title = ?, billing_type = ?, hourly_rate_paise = ?,
            fixed_price_paise = ?, estimated_hours = ?, status = ?, start_date = ?, deadline = ?
        WHERE id = ?
          AND client_id IN (SELECT id FROM clients WHERE user_id = ?)
        """,
        (data["client_id"], data["title"], data["billing_type"], data["hourly_rate_paise"],
         data["fixed_price_paise"], data["estimated_hours"], data["status"],
         data["start_date"], data["deadline"], project_id, user_id),
    )
    return cur.rowcount == 1


def delete_project(conn: sqlite3.Connection, user_id: int, project_id: int) -> str | None:
    """Delete a project and its time logs. Blocked if it has invoices.

    Returns an error message, or None on success. The time logs are removed
    by ON DELETE CASCADE. Invoices are protected by ON DELETE RESTRICT.
    """
    blocked_msg = ("it has {} invoice(s). Invoices are financial records, so mark the "
                   "project as cancelled instead.")
    try:
        with transaction(conn):
            invoice_count = conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
            if invoice_count:
                return blocked_msg.format(invoice_count)
            cur = conn.execute(
                """
                DELETE FROM projects
                WHERE id = ? AND client_id IN (SELECT id FROM clients WHERE user_id = ?)
                """,
                (project_id, user_id),
            )
            if cur.rowcount == 0:
                return "project not found."
    except sqlite3.IntegrityError:
        return blocked_msg.format("some")
    return None


def project_summary(conn: sqlite3.Connection, user_id: int, project_id: int) -> sqlite3.Row:
    """Totals shown on the project page: hours logged and invoices raised.

    Each figure is a scalar subquery, so the two SUMs cannot multiply each
    other's rows, which a double JOIN to time_logs and invoices would do.
    """
    return conn.execute(
        """
        SELECT
          (SELECT COALESCE(SUM(t.duration_minutes), 0)
             FROM time_logs t WHERE t.project_id = p.id)           AS logged_minutes,
          (SELECT COUNT(*) FROM invoices i WHERE i.project_id = p.id) AS invoice_count
        FROM projects p
        JOIN clients c ON c.id = p.client_id
        WHERE p.id = ? AND c.user_id = ?
        """,
        (project_id, user_id),
    ).fetchone()
