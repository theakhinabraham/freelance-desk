"""CSV export of payments and time logs."""

import csv
import io

from app import db
from app.services.exports import paise_to_rupees, payments_csv, safe_cell, time_logs_csv
from tests.conftest import make_client, make_project, make_user, start_timer


def parse(text):
    return list(csv.DictReader(io.StringIO(text)))


def add_finished_log(conn, project_id, start, end, minutes, description=None):
    conn.execute(
        """
        INSERT INTO time_logs (project_id, started_at, ended_at, duration_minutes, description)
        VALUES (?, ?, ?, ?, ?)
        """,
        (project_id, start, end, minutes, description),
    )


def add_paid_invoice(conn, project_id, number, amount, payments):
    invoice_id = conn.execute(
        """
        INSERT INTO invoices (project_id, invoice_number, issue_date, due_date, amount_paise, status)
        VALUES (?, ?, '2026-11-01', '2026-11-15', ?, 'sent')
        """,
        (project_id, number, amount),
    ).lastrowid
    for paid_on, part in payments:
        conn.execute(
            "INSERT INTO payments (invoice_id, amount_paise, paid_on, method) VALUES (?, ?, ?, 'upi')",
            (invoice_id, part, paid_on),
        )


def test_money_and_formula_helpers():
    assert paise_to_rupees(150050) == "1500.50"
    assert paise_to_rupees(5) == "0.05"
    assert safe_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert safe_cell("-5") == "'-5"
    assert safe_cell(None) == ""
    assert safe_cell("Website redesign") == "Website redesign"


def test_payments_csv_lists_partial_payments(conn):
    user = make_user(conn)
    project = make_project(conn, make_client(conn, user, "Acme"))
    add_paid_invoice(conn, project, "FD-2026-0001", 300_000,
                     [("2026-11-05", 100_000), ("2026-11-10", 150_050)])

    rows = parse(payments_csv(conn, user))
    assert [r["amount_inr"] for r in rows] == ["1500.50", "1000.00"]   # newest first
    assert rows[0]["invoice_number"] == "FD-2026-0001"
    assert rows[0]["client"] == "Acme"
    assert rows[0]["method"] == "UPI"


def test_time_logs_csv_skips_running_timer_and_shows_billing(conn):
    user = make_user(conn)
    project = make_project(conn, make_client(conn, user, "Acme"))
    add_finished_log(conn, project, "2026-11-18 10:00:00", "2026-11-18 12:15:00", 135, "=cmd")
    start_timer(conn, project, "2026-11-19 09:00:00")

    rows = parse(time_logs_csv(conn, user))
    assert len(rows) == 1                       # the running timer is not exported
    assert rows[0]["duration_minutes"] == "135"
    assert rows[0]["hours"] == "2.25"
    assert rows[0]["description"] == "'=cmd"    # formula neutralised
    assert rows[0]["invoice_number"] == ""      # not billed yet


def test_exports_are_scoped_to_the_user(conn):
    alice, bob = make_user(conn, "a@example.com"), make_user(conn, "b@example.com")
    project = make_project(conn, make_client(conn, alice))
    add_finished_log(conn, project, "2026-11-18 10:00:00", "2026-11-18 11:00:00", 60)
    add_paid_invoice(conn, project, "FD-2026-0001", 100_000, [("2026-11-05", 100_000)])

    assert len(parse(time_logs_csv(conn, alice))) == 1
    assert len(parse(payments_csv(conn, alice))) == 1
    assert parse(time_logs_csv(conn, bob)) == []
    assert parse(payments_csv(conn, bob)) == []


def test_export_download_via_web(make_browser, web_db):
    anonymous = make_browser()
    assert anonymous.get("/export/payments.csv").status_code == 303   # login required

    alice = make_browser("alice@example.com")
    bob = make_browser("bob@example.com")
    connection = db.connect(web_db)
    try:
        user_id = connection.execute(
            "SELECT id FROM users WHERE email = ?", ("alice@example.com",)).fetchone()[0]
        project = make_project(connection, make_client(connection, user_id, "Acme"))
        add_finished_log(connection, project, "2026-11-18 10:00:00", "2026-11-18 11:00:00", 60)
    finally:
        connection.close()

    response = alice.get("/export/time_logs.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = parse(response.content.decode("utf-8-sig"))
    assert [r["client"] for r in rows] == ["Acme"]

    # Bob gets only the header row.
    assert len(parse(bob.get("/export/time_logs.csv").content.decode("utf-8-sig"))) == 0
