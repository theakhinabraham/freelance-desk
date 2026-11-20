"""Phase 1 checks: transaction helper, invoice numbering, formatting, seed data, health page."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db
from app.services.invoices import next_invoice_number
from app.templating import format_hours, format_inr
from db.seed import seed
from tests.conftest import make_user


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            make_user(conn)
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_next_invoice_number_increments_per_year(conn):
    assert next_invoice_number(conn, 2026) == "FD-2026-0001"
    user = make_user(conn)
    client = conn.execute("INSERT INTO clients (user_id, name) VALUES (?, 'C')", (user,)).lastrowid
    project = conn.execute(
        "INSERT INTO projects (client_id, title, billing_type, hourly_rate_paise) VALUES (?, 'P', 'hourly', 1)",
        (client,),
    ).lastrowid
    for number in ("FD-2026-0009", "FD-2025-0042"):
        conn.execute(
            """
            INSERT INTO invoices (project_id, invoice_number, issue_date, due_date, amount_paise)
            VALUES (?, ?, '2026-01-01', '2026-01-10', 100)
            """,
            (project, number),
        )
    assert next_invoice_number(conn, 2026) == "FD-2026-0010"
    assert next_invoice_number(conn, 2025) == "FD-2025-0043"
    assert next_invoice_number(conn, 2027) == "FD-2027-0001"


@pytest.mark.parametrize("paise, text", [
    (0, "₹0.00"), (5, "₹0.05"), (99_900, "₹999.00"), (100_000, "₹1,000.00"),
    (12_345_678, "₹1,23,456.78"), (1_000_000_000, "₹1,00,00,000.00"), (-150_050, "-₹1,500.50"),
    (None, "—"),
])
def test_format_inr_indian_grouping(paise, text):
    assert format_inr(paise) == text


def test_format_hours():
    assert format_hours(135) == "2h 15m"
    assert format_hours(None) == "0h 00m"


def test_seed_meets_spec(conn):
    seed(conn, today=date(2026, 11, 19))
    row = conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM clients)   AS clients,
               (SELECT COUNT(*) FROM projects)  AS projects,
               (SELECT COUNT(*) FROM time_logs) AS logs,
               (SELECT COUNT(*) FROM invoices)  AS invoices,
               (SELECT COUNT(*) FROM time_logs WHERE ended_at IS NULL) AS running
        """
    ).fetchone()
    assert (row["clients"], row["projects"]) == (5, 8)
    assert row["logs"] >= 60
    assert row["invoices"] >= 8
    assert row["running"] == 1

    # At least one partially paid invoice: some payment, but less than the amount.
    partial = conn.execute(
        """
        SELECT COUNT(*) FROM invoices i
        WHERE (SELECT SUM(amount_paise) FROM payments p WHERE p.invoice_id = i.id)
              < i.amount_paise
        """
    ).fetchone()[0]
    assert partial >= 1

    # No invoice is overpaid.
    overpaid = conn.execute(
        """
        SELECT COUNT(*) FROM invoices i
        WHERE (SELECT COALESCE(SUM(amount_paise), 0) FROM payments p WHERE p.invoice_id = i.id)
              > i.amount_paise
        """
    ).fetchone()[0]
    assert overpaid == 0


def test_health_page(tmp_path, monkeypatch):
    monkeypatch.setenv("FREELANCEDESK_DB", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as client:  # "with" runs the startup code (creates tables)
        response = client.get("/health")
    assert response.status_code == 200
    assert "System health" in response.text
    assert "time_logs" in response.text
