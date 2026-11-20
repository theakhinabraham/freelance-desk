"""Invoices and payments: prefill, numbering, billing of logs, payment status transitions,
overdue refresh, cancel, PDF, and isolation."""

import pytest

from app.services import invoices as inv
from app.services.payments import record_payment
from app.services.pdf import build_invoice_pdf
from app.services.projects import get_project
from tests.conftest import (create_client_via_web, create_project_via_web, db_rows,
                            make_client, make_project, make_user)

TODAY = "2026-11-19"


def add_log(conn, project, start, end, minutes):
    return conn.execute(
        """INSERT INTO time_logs (project_id, started_at, ended_at, duration_minutes)
           VALUES (?, ?, ?, ?)""", (project, start, end, minutes)).lastrowid


def invoice_data(amount, issue="2026-11-10", due="2026-11-25", status="sent"):
    return {"issue_date": issue, "due_date": due, "amount_paise": amount, "status": status}


def pay(conn, user, invoice_id, amount, paid_on="2026-11-15"):
    return record_payment(conn, user, invoice_id, {
        "amount_paise": amount, "paid_on": paid_on, "method": "upi", "reference": None}, TODAY)


def status(conn, invoice_id):
    return conn.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()[0]


@pytest.fixture
def hourly(conn):
    """A user with an hourly project at Rs 1,500/h and 3h 30m of finished, unbilled time."""
    user = make_user(conn)
    project = make_project(conn, make_client(conn, user), rate=150_000)
    add_log(conn, project, "2026-11-02 10:00:00", "2026-11-02 12:00:00", 120)
    add_log(conn, project, "2026-11-03 10:00:00", "2026-11-03 11:30:00", 90)
    return user, project


# --- prefill and creation ----------------------------------------------------------

def test_hourly_prefill_is_unbilled_hours_times_rate(conn, hourly):
    user, project_id = hourly
    conn.execute("INSERT INTO time_logs (project_id, started_at) VALUES (?, '2026-11-19 09:00:00')",
                 (project_id,))  # a running timer must NOT be billed
    prefill = inv.invoice_prefill(conn, get_project(conn, user, project_id), "2026-11-19 10:00:00")
    assert prefill == {"log_count": 2, "minutes": 210, "amount_paise": 525_000}  # 3.5h x 1500


def test_hourly_amount_rounding():
    assert inv.hourly_amount(1, 100_000) == 1_667   # 1 min at Rs 1000/h = Rs 16.666... -> 16.67
    assert inv.hourly_amount(60, 99_999) == 99_999


def test_create_invoice_marks_logs_billed_up_to_cutoff(conn, hourly):
    user, project_id = hourly
    late = add_log(conn, project_id, "2026-11-19 09:00:00", "2026-11-19 10:00:00", 60)
    invoice_id, error = inv.create_invoice(conn, user, project_id, invoice_data(525_000),
                                           cutoff="2026-11-18 23:59:59")
    assert error is None
    billed = [r[0] for r in conn.execute("SELECT id FROM time_logs WHERE invoice_id = ?", (invoice_id,))]
    assert len(billed) == 2 and late not in billed  # logged after the form was opened

    prefill = inv.invoice_prefill(conn, get_project(conn, user, project_id), "2026-11-19 12:00:00")
    assert prefill["minutes"] == 60  # only the late log is still unbilled


def test_invoice_numbers_are_sequential_per_year(conn, hourly):
    user, project_id = hourly
    numbers = []
    for issue in ("2026-01-05", "2026-02-05", "2027-01-05"):
        invoice_id, _ = inv.create_invoice(conn, user, project_id,
                                           invoice_data(1000, issue=issue, due=issue), "2026-01-01 00:00:00")
        numbers.append(conn.execute("SELECT invoice_number FROM invoices WHERE id = ?",
                                    (invoice_id,)).fetchone()[0])
    assert numbers == ["FD-2026-0001", "FD-2026-0002", "FD-2027-0001"]


def test_fixed_prefill_is_remaining_price(conn):
    user = make_user(conn)
    project_id = make_project(conn, make_client(conn, user), billing_type="fixed",
                              rate=None, fixed=10_000_000)
    project = get_project(conn, user, project_id)
    assert inv.invoice_prefill(conn, project, "2026-11-19 00:00:00")["amount_paise"] == 10_000_000
    first, _ = inv.create_invoice(conn, user, project_id, invoice_data(4_000_000), "x")
    assert inv.invoice_prefill(conn, project, "x") == {
        "already_invoiced_paise": 4_000_000, "amount_paise": 6_000_000}
    inv.cancel_invoice(conn, user, first)  # cancelled invoices don't count
    assert inv.invoice_prefill(conn, project, "x")["amount_paise"] == 10_000_000


def test_create_invoice_rolls_back_completely_on_error(conn, hourly):
    user, project_id = hourly
    with pytest.raises(Exception):
        inv.create_invoice(conn, user, project_id, invoice_data(0), "2026-11-19 00:00:00")  # CHECK amount > 0
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM time_logs WHERE invoice_id IS NOT NULL").fetchone()[0] == 0


# --- payments: the partial -> paid transition ------------------------------------------

def test_partial_payment_then_full_payment(conn, hourly):
    user, project_id = hourly
    invoice_id, _ = inv.create_invoice(conn, user, project_id, invoice_data(100_000), "x")

    assert pay(conn, user, invoice_id, 40_000) is None
    row = inv.get_invoice(conn, user, invoice_id)
    assert (row["status"], row["paid_paise"], row["balance_paise"]) == ("sent", 40_000, 60_000)

    assert pay(conn, user, invoice_id, 60_000) is None
    row = inv.get_invoice(conn, user, invoice_id)
    assert (row["status"], row["paid_paise"], row["balance_paise"]) == ("paid", 100_000, 0)

    assert "paid" in pay(conn, user, invoice_id, 1)  # no more payments once paid


def test_overpayment_rejected(conn, hourly):
    user, project_id = hourly
    invoice_id, _ = inv.create_invoice(conn, user, project_id, invoice_data(100_000), "x")
    pay(conn, user, invoice_id, 70_000)
    assert "more than the balance" in pay(conn, user, invoice_id, 30_001)
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


@pytest.mark.parametrize("paid_on, message", [
    ("2026-11-20", "future"), ("2026-11-09", "before the invoice date")])
def test_payment_date_rules(conn, hourly, paid_on, message):
    user, project_id = hourly
    invoice_id, _ = inv.create_invoice(conn, user, project_id, invoice_data(100_000), "x")
    assert message in pay(conn, user, invoice_id, 100, paid_on=paid_on)


def test_no_payment_on_draft_or_cancelled(conn, hourly):
    user, project_id = hourly
    draft, _ = inv.create_invoice(conn, user, project_id, invoice_data(100_000, status="draft"), "x")
    assert "Mark the invoice as sent" in pay(conn, user, draft, 100)
    inv.cancel_invoice(conn, user, draft)
    assert "cancelled" in pay(conn, user, draft, 100)


# --- overdue -------------------------------------------------------------------------

def test_refresh_overdue(conn, hourly):
    user, project_id = hourly
    past_due, _ = inv.create_invoice(conn, user, project_id, invoice_data(1000, due="2026-11-18"), "x")
    due_today, _ = inv.create_invoice(conn, user, project_id, invoice_data(1000, due=TODAY), "x")
    draft, _ = inv.create_invoice(conn, user, project_id,
                                  invoice_data(1000, due="2026-11-12", status="draft"), "x")
    other = make_user(conn, "b@example.com")
    others, _ = inv.create_invoice(conn, other, make_project(conn, make_client(conn, other)),
                                   invoice_data(1000, due="2026-11-12"), "x")

    assert inv.refresh_overdue(conn, user, TODAY) == 1
    assert [status(conn, i) for i in (past_due, due_today, draft, others)] == \
        ["overdue", "sent", "draft", "sent"]  # other user's invoice is untouched
    assert inv.refresh_overdue(conn, user, TODAY) == 0  # idempotent


def test_overdue_partial_stays_overdue_then_paid(conn, hourly):
    user, project_id = hourly
    invoice_id, _ = inv.create_invoice(conn, user, project_id,
                                       invoice_data(1000, due="2026-11-12"), "x")
    inv.refresh_overdue(conn, user, TODAY)
    pay(conn, user, invoice_id, 400)
    assert status(conn, invoice_id) == "overdue"
    pay(conn, user, invoice_id, 600)
    assert status(conn, invoice_id) == "paid"


def test_mark_sent_after_due_date_goes_overdue(conn, hourly):
    user, project_id = hourly
    draft, _ = inv.create_invoice(conn, user, project_id,
                                  invoice_data(1000, due="2026-11-12", status="draft"), "x")
    assert inv.mark_sent(conn, user, draft, TODAY) is None
    assert status(conn, draft) == "overdue"
    assert "Only draft" in inv.mark_sent(conn, user, draft, TODAY)


# --- cancel --------------------------------------------------------------------------

def test_cancel_releases_hours_but_not_when_paid(conn, hourly):
    user, project_id = hourly
    invoice_id, _ = inv.create_invoice(conn, user, project_id, invoice_data(525_000), "2026-11-19 00:00:00")
    assert inv.cancel_invoice(conn, user, invoice_id) is None
    assert status(conn, invoice_id) == "cancelled"
    assert conn.execute("SELECT COUNT(*) FROM time_logs WHERE invoice_id IS NOT NULL").fetchone()[0] == 0

    second, _ = inv.create_invoice(conn, user, project_id, invoice_data(525_000), "2026-11-19 00:00:00")
    pay(conn, user, second, 100)
    assert "payments recorded" in inv.cancel_invoice(conn, user, second)


# --- PDF -----------------------------------------------------------------------------

def test_pdf_builds_with_unicode_text(conn, hourly):
    user, project_id = hourly
    conn.execute("UPDATE clients SET name = 'Café ₹ Ünïcode 日本 — “Ltd”'")
    invoice_id, _ = inv.create_invoice(conn, user, project_id, invoice_data(525_000), "2026-11-19 00:00:00")
    pay(conn, user, invoice_id, 25_000)
    invoice = inv.get_invoice(conn, user, invoice_id)
    user_row = conn.execute("SELECT id, name, email FROM users").fetchone()
    content = build_invoice_pdf(invoice, user_row, inv.billed_logs(conn, invoice_id),
                                inv.invoice_payments(conn, invoice_id))
    assert content.startswith(b"%PDF")


# --- web: full flow and isolation ----------------------------------------------------

def test_invoice_and_payment_flow_via_web(make_browser, web_db):
    browser = make_browser("a@example.com")
    project = create_project_via_web(browser, create_client_via_web(browser, "Acme"), hourly_rate="1000")
    from app import db
    conn = db.connect(web_db)
    add_log(conn, project, "2026-11-02 10:00:00", "2026-11-02 11:30:00", 90)
    conn.close()

    page = browser.get(f"/invoices/new?project_id={project}").text
    assert 'value="1500.00"' in page  # 1.5h x Rs 1000 pre-filled

    response = browser.post("/invoices/new", data={
        "project_id": project, "amount": "1500", "issue_date": "2026-11-10",
        "due_date": "2030-01-01", "action": "send", "cutoff": "2026-11-19 00:00:00"})
    assert response.status_code == 303
    invoice_url = response.headers["location"]

    browser.post(invoice_url + "/payments", data={"amount": "500", "paid_on": "2026-11-15", "method": "upi"})
    page = browser.get(invoice_url).text
    assert "Partial payment recorded" in page and "Partially paid" in page

    response = browser.post(invoice_url + "/payments",
                            data={"amount": "2000", "paid_on": "2026-11-15", "method": "upi"})
    assert response.status_code == 400 and "more than the balance" in response.text

    browser.post(invoice_url + "/payments", data={"amount": "1000", "paid_on": "2026-11-16", "method": "bank"})
    assert "fully paid" in browser.get(invoice_url).text
    assert db_rows(web_db, "SELECT status FROM invoices")[0][0] == "paid"

    response = browser.get(invoice_url + "/pdf")
    assert response.headers["content-type"] == "application/pdf"
    assert 'filename="FD-2026-0001.pdf"' in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


def test_invoices_are_isolated_between_users(make_browser, web_db):
    alice = make_browser("alice@example.com")
    project = create_project_via_web(alice, create_client_via_web(alice), billing_type="fixed",
                                     fixed_price="50000", hourly_rate="")
    response = alice.post("/invoices/new", data={
        "project_id": project, "amount": "50000", "issue_date": "2026-11-10",
        "due_date": "2026-11-25", "action": "send"})
    invoice_url = response.headers["location"]

    bob = make_browser("bob@example.com")
    for url in (invoice_url, invoice_url + "/pdf", f"/invoices/new?project_id={project}"):
        assert bob.get(url).status_code == 404, url
    assert bob.post(invoice_url + "/payments",
                    data={"amount": "1", "paid_on": "2026-11-15", "method": "upi"}).status_code == 404
    assert bob.post(invoice_url + "/cancel").status_code == 404
    assert bob.post("/invoices/new", data={"project_id": project, "amount": "1",
                                           "issue_date": "2026-11-10", "due_date": "2026-11-25"}).status_code == 404
    assert "FD-2026" not in bob.get("/invoices").text
    assert db_rows(web_db, "SELECT COUNT(*) FROM payments")[0][0] == 0
    assert db_rows(web_db, "SELECT COUNT(*) FROM invoices")[0][0] == 1
