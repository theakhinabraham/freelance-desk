"""Client queries. EVERY query filters on user_id, so a user only ever sees their own clients."""

import sqlite3

from app.db import transaction


def list_clients(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    # LEFT JOIN keeps clients that have no projects yet (COUNT(p.id) = 0).
    # COUNT(p.id) counts only non-NULL values, i.e. real projects.
    # SUM(p.status = 'active'): a comparison is 1 or 0 in SQLite, so the sum
    # counts the active projects. COALESCE turns the SUM of no rows (NULL) into 0.
    return conn.execute(
        """
        SELECT c.id, c.name, c.email, c.phone,
               COUNT(p.id)                         AS project_count,
               COALESCE(SUM(p.status = 'active'), 0) AS active_count
        FROM clients c
        LEFT JOIN projects p ON p.client_id = c.id
        WHERE c.user_id = ?
        GROUP BY c.id
        ORDER BY c.name COLLATE NOCASE
        """,
        (user_id,),
    ).fetchall()


def get_client(conn: sqlite3.Connection, user_id: int, client_id: int) -> sqlite3.Row | None:
    """Returns None if the client does not exist OR belongs to someone else."""
    return conn.execute(
        """
        SELECT id, name, email, phone, notes, created_at
        FROM clients
        WHERE id = ? AND user_id = ?
        """,
        (client_id, user_id),
    ).fetchone()


def create_client(conn: sqlite3.Connection, user_id: int, data: dict) -> int:
    cur = conn.execute(
        "INSERT INTO clients (user_id, name, email, phone, notes) VALUES (?, ?, ?, ?, ?)",
        (user_id, data["name"], data["email"], data["phone"], data["notes"]),
    )
    return cur.lastrowid


def update_client(conn: sqlite3.Connection, user_id: int, client_id: int, data: dict) -> bool:
    """Returns False if nothing was updated (not found or not the user's client)."""
    cur = conn.execute(
        """
        UPDATE clients SET name = ?, email = ?, phone = ?, notes = ?
        WHERE id = ? AND user_id = ?
        """,
        (data["name"], data["email"], data["phone"], data["notes"], client_id, user_id),
    )
    return cur.rowcount == 1


def delete_client(conn: sqlite3.Connection, user_id: int, client_id: int) -> str | None:
    """Delete a client that has no projects. Returns an error message, or None on success.

    The count and the delete run in one transaction, so a project cannot be
    added in between. ON DELETE RESTRICT on projects.client_id is the safety net.
    """
    with_projects_msg = "it still has {} project(s). Delete them first or keep the client."
    try:
        with transaction(conn):
            project_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM projects p
                JOIN clients c ON c.id = p.client_id
                WHERE p.client_id = ? AND c.user_id = ?
                """,
                (client_id, user_id),
            ).fetchone()[0]
            if project_count:
                return with_projects_msg.format(project_count)
            cur = conn.execute(
                "DELETE FROM clients WHERE id = ? AND user_id = ?", (client_id, user_id)
            )
            if cur.rowcount == 0:
                return "client not found."
    except sqlite3.IntegrityError:
        return with_projects_msg.format("some")
    return None
