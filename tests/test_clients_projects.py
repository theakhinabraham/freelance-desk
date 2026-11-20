"""Client and project CRUD, delete rules, money parsing and validation."""

import pytest

from app.forms import parse_rupees
from tests.conftest import create_client_via_web, create_project_via_web, db_rows


@pytest.mark.parametrize("text, paise", [
    ("1500", 150_000), ("1,500.50", 150_050), ("1500.5", 150_050), ("0.10", 10),
    ("₹2,00,000", 20_000_000), ("", None), ("  ", None),
])
def test_parse_rupees(text, paise):
    assert parse_rupees(text) == paise


@pytest.mark.parametrize("text", ["abc", "1.234", "-50", "0", "0.00", "1e3", "12.", "NaN"])
def test_parse_rupees_rejects(text):
    with pytest.raises(ValueError):
        parse_rupees(text)


def test_client_crud(make_browser, web_db):
    browser = make_browser("a@example.com")
    client_id = create_client_via_web(browser, "Acme")
    assert "Acme" in browser.get("/clients").text

    response = browser.post(f"/clients/{client_id}/edit",
                            data={"name": "Acme Corp", "email": "x@acme.example", "phone": "", "notes": "VIP"})
    assert response.status_code == 303
    row = db_rows(web_db, "SELECT name, email, phone, notes FROM clients")[0]
    assert tuple(row) == ("Acme Corp", "x@acme.example", None, "VIP")  # empty phone -> NULL

    assert browser.post(f"/clients/{client_id}/delete").status_code == 303
    assert db_rows(web_db, "SELECT id FROM clients") == []


def test_client_name_required(make_browser):
    response = make_browser("a@example.com").post("/clients/new", data={"name": "  "})
    assert response.status_code == 400
    assert "Client name is required" in response.text


def test_deleting_client_with_projects_is_blocked(make_browser, web_db):
    browser = make_browser("a@example.com")
    client_id = create_client_via_web(browser, "Acme")
    create_project_via_web(browser, client_id)

    response = browser.post(f"/clients/{client_id}/delete")
    assert response.status_code == 303
    page = browser.get(response.headers["location"]).text
    assert "Cannot delete" in page and "1 project(s)" in page
    assert len(db_rows(web_db, "SELECT id FROM clients")) == 1


def test_project_money_stored_as_exact_paise(make_browser, web_db):
    browser = make_browser("a@example.com")
    client_id = create_client_via_web(browser)
    create_project_via_web(browser, client_id, hourly_rate="1,499.99", fixed_price="9999")
    row = db_rows(web_db, "SELECT hourly_rate_paise, fixed_price_paise FROM projects")[0]
    # The fixed price is discarded for an hourly project, so the row always passes the CHECK.
    assert (row["hourly_rate_paise"], row["fixed_price_paise"]) == (149_999, None)
    assert "₹1,499.99/h" in browser.get("/projects").text


def test_fixed_project_and_validation(make_browser, web_db):
    browser = make_browser("a@example.com")
    client_id = create_client_via_web(browser)

    response = browser.post("/projects/new", data={
        "client_id": client_id, "title": "Logo", "billing_type": "fixed", "hourly_rate": "500"})
    assert response.status_code == 400
    assert "Fixed-price projects need a price" in response.text

    response = browser.post("/projects/new", data={
        "client_id": client_id, "title": "Logo", "billing_type": "hourly", "hourly_rate": "abc",
        "start_date": "2026-11-10", "deadline": "2026-11-01"})
    assert response.status_code == 400
    assert "Enter an amount like" in response.text
    assert "Deadline cannot be before the start date" in response.text
    assert 'value="abc"' in response.text  # the typed value is shown again

    create_project_via_web(browser, client_id, title="Logo", billing_type="fixed",
                           fixed_price="25000", hourly_rate="")
    row = db_rows(web_db, "SELECT billing_type, hourly_rate_paise, fixed_price_paise FROM projects")[0]
    assert tuple(row) == ("fixed", None, 2_500_000)


def test_project_edit_and_status_filter(make_browser, web_db):
    browser = make_browser("a@example.com")
    client_id = create_client_via_web(browser)
    project_id = create_project_via_web(browser, client_id, title="Alpha")
    create_project_via_web(browser, client_id, title="Beta")

    response = browser.post(f"/projects/{project_id}/edit", data={
        "client_id": client_id, "title": "Alpha", "billing_type": "hourly",
        "hourly_rate": "2000", "status": "completed", "estimated_hours": "40"})
    assert response.status_code == 303

    browser.get("/projects")  # consume pending flash messages
    completed = browser.get("/projects?status=completed").text
    assert ">Alpha</a>" in completed and ">Beta</a>" not in completed
    assert ">Beta</a>" in browser.get("/projects?status=active").text
    all_projects = browser.get("/projects?status=bogus").text  # unknown filter = all
    assert ">Alpha</a>" in all_projects and ">Beta</a>" in all_projects


def test_delete_project_cascades_logs_but_blocks_on_invoices(make_browser, web_db):
    browser = make_browser("a@example.com")
    client_id = create_client_via_web(browser)
    p1 = create_project_via_web(browser, client_id, title="One")
    p2 = create_project_via_web(browser, client_id, title="Two")

    from app import db
    conn = db.connect(web_db)
    conn.execute("""INSERT INTO time_logs (project_id, started_at, ended_at, duration_minutes)
                    VALUES (?, '2026-11-01 10:00:00', '2026-11-01 11:00:00', 60)""", (p1,))
    conn.execute("""INSERT INTO invoices (project_id, invoice_number, issue_date, due_date, amount_paise)
                    VALUES (?, 'FD-2026-0001', '2026-11-01', '2026-11-15', 1000)""", (p2,))
    conn.close()

    assert browser.post(f"/projects/{p1}/delete").status_code == 303
    assert db_rows(web_db, "SELECT id FROM time_logs") == []  # ON DELETE CASCADE

    response = browser.post(f"/projects/{p2}/delete")
    assert "has 1 invoice(s)" in browser.get(response.headers["location"]).text
    assert len(db_rows(web_db, "SELECT id FROM projects")) == 1
