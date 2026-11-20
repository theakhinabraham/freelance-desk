"""Constraint, trigger and foreign-key tests: the database must reject bad data itself."""

import sqlite3

import pytest

from tests.conftest import make_client, make_project, make_user, start_timer


@pytest.fixture
def project(conn):
    user = make_user(conn)
    return make_project(conn, make_client(conn, user))


def make_invoice(conn, project_id, number="FD-2026-0001", issue="2026-11-01",
                 due="2026-11-15", amount=10_000, status="sent"):
    return conn.execute(
        """
        INSERT INTO invoices (project_id, invoice_number, issue_date, due_date, amount_paise, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, number, issue, due, amount, status),
    ).lastrowid


# --- foreign keys -----------------------------------------------------------

def test_foreign_keys_pragma_is_on(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_project_needs_existing_client(conn):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        make_project(conn, client_id=999)


def test_cannot_delete_client_with_projects(conn, project):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("DELETE FROM clients")


# --- CHECK constraints ------------------------------------------------------

def test_duplicate_email_rejected_case_insensitively(conn):
    make_user(conn, "asha@example.com")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        make_user(conn, "ASHA@example.com")


def test_invalid_billing_type_rejected(conn):
    client = make_client(conn, make_user(conn))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        make_project(conn, client, billing_type="weekly")


def test_hourly_project_requires_rate(conn):
    client = make_client(conn, make_user(conn))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        make_project(conn, client, billing_type="hourly", rate=None)


def test_fixed_project_requires_price(conn):
    client = make_client(conn, make_user(conn))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        make_project(conn, client, billing_type="fixed", rate=None, fixed=None)
    assert make_project(conn, client, billing_type="fixed", rate=None, fixed=50_000)


def test_invalid_project_status_rejected(conn, project):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("UPDATE projects SET status = 'done' WHERE id = ?", (project,))


def test_ended_at_must_be_after_started_at(conn, project):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            """
            INSERT INTO time_logs (project_id, started_at, ended_at, duration_minutes)
            VALUES (?, '2026-11-19 10:00:00', '2026-11-19 09:00:00', 0)
            """,
            (project,),
        )


def test_finished_log_needs_duration(conn, project):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            """
            INSERT INTO time_logs (project_id, started_at, ended_at)
            VALUES (?, '2026-11-19 10:00:00', '2026-11-19 11:00:00')
            """,
            (project,),
        )


def test_invoice_status_and_dates_checked(conn, project):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        make_invoice(conn, project, status="unpaid")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        make_invoice(conn, project, issue="2026-11-15", due="2026-11-01")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        make_invoice(conn, project, number="INV-1")


def test_invoice_number_unique(conn, project):
    make_invoice(conn, project)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        make_invoice(conn, project)


def test_payment_amount_and_method_checked(conn, project):
    invoice = make_invoice(conn, project)
    sql = "INSERT INTO payments (invoice_id, amount_paise, paid_on, method) VALUES (?, ?, ?, ?)"
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(sql, (invoice, 0, "2026-11-10", "upi"))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(sql, (invoice, 500, "2026-11-10", "cheque"))
    conn.execute(sql, (invoice, 500, "2026-11-10", "upi"))


# --- one running timer per user (trigger) ----------------------------------

def test_second_running_timer_for_same_user_blocked(conn):
    user = make_user(conn)
    client = make_client(conn, user)
    p1, p2 = make_project(conn, client), make_project(conn, client)
    start_timer(conn, p1)
    with pytest.raises(sqlite3.IntegrityError, match="already running"):
        start_timer(conn, p2)


def test_different_users_can_each_run_a_timer(conn):
    p1 = make_project(conn, make_client(conn, make_user(conn, "a@x.com")))
    p2 = make_project(conn, make_client(conn, make_user(conn, "b@x.com")))
    start_timer(conn, p1)
    start_timer(conn, p2)  # must not raise


def test_new_timer_allowed_after_stopping(conn, project):
    log = start_timer(conn, project)
    conn.execute(
        "UPDATE time_logs SET ended_at = '2026-11-19 11:00:00', duration_minutes = 60 WHERE id = ?",
        (log,),
    )
    start_timer(conn, project, "2026-11-19 12:00:00")  # must not raise


def test_reopening_a_stopped_log_blocked_while_another_runs(conn, project):
    old = conn.execute(
        """
        INSERT INTO time_logs (project_id, started_at, ended_at, duration_minutes)
        VALUES (?, '2026-11-18 10:00:00', '2026-11-18 11:00:00', 60)
        """,
        (project,),
    ).lastrowid
    start_timer(conn, project)
    with pytest.raises(sqlite3.IntegrityError, match="already running"):
        conn.execute(
            "UPDATE time_logs SET ended_at = NULL, duration_minutes = NULL WHERE id = ?", (old,)
        )
