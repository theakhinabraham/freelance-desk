"""Effective hourly rate, dashboard totals and the SQL reports in db/reports.sql."""

from datetime import date

import pytest

from app.reports import load_queries, run
from app.services.dashboard import dashboard_data
from tests.conftest import (create_client_via_web, make_client, make_project, make_user)

TODAY = date(2026, 11, 19)


def add_log(conn, project, minutes, start="2026-11-02 10:00:00", end="2026-11-02 23:00:00", invoice=None):
    conn.execute("""INSERT INTO time_logs (project_id, started_at, ended_at, duration_minutes, invoice_id)
                    VALUES (?, ?, ?, ?, ?)""", (project, start, end, minutes, invoice))


_number = [0]


def add_invoice(conn, project, amount, status="sent", issue="2026-10-01", due="2026-10-15"):
    _number[0] += 1
    return conn.execute(
        """INSERT INTO invoices (project_id, invoice_number, issue_date, due_date, amount_paise, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (project, f"FD-2026-{_number[0]:04d}", issue, due, amount, status)).lastrowid


def add_payment(conn, invoice, amount, paid_on="2026-10-10"):
    conn.execute("INSERT INTO payments (invoice_id, amount_paise, paid_on, method) VALUES (?, ?, ?, 'upi')",
                 (invoice, amount, paid_on))


@pytest.fixture
def data(conn):
    """
    Client A
      p1 hourly Rs 1,500/h: 3 logs (60 + 60 + 60 = 180 min), 3 payments of Rs 1,000 = Rs 3,000
           -> effective rate Rs 3,000 / 3 h = Rs 1,000/h. (A naive JOIN would make 9 rows.)
      p2 fixed: 30 min logged, Rs 4,000 received                   -> Rs 8,000/h
    Client B
      p3 hourly: 0 hours logged, nothing paid                      -> no rate (NULL)
    Another user: a project with a big payment that must never show up.
    """
    user = make_user(conn)
    a, b = make_client(conn, user, "A"), make_client(conn, user, "B")
    p1 = make_project(conn, a, rate=150_000)
    p2 = make_project(conn, a, billing_type="fixed", rate=None, fixed=1_000_000)
    p3 = make_project(conn, b)
    for _ in range(3):
        add_log(conn, p1, 60)
    conn.execute("INSERT INTO time_logs (project_id, started_at) VALUES (?, '2026-11-19 09:00:00')",
                 (p1,))  # running timer: not counted
    inv1 = add_invoice(conn, p1, 300_000)
    for _ in range(3):
        add_payment(conn, inv1, 100_000)
    add_log(conn, p2, 30)
    add_payment(conn, add_invoice(conn, p2, 400_000), 400_000)

    other = make_user(conn, "other@example.com")
    op = make_project(conn, make_client(conn, other, "Other"))
    add_log(conn, op, 60)
    add_payment(conn, add_invoice(conn, op, 9_900_000), 9_900_000)
    return {"user": user, "p1": p1, "p2": p2, "p3": p3, "a": a, "b": b}


# --- effective hourly rate (the headline feature) -----------------------------------

def test_effective_rate_per_project(conn, data):
    rows = {r["project_id"]: r for r in run(conn, "effective_rate_by_project", data["user"])}
    assert set(rows) == {data["p1"], data["p2"], data["p3"]}           # other user excluded
    assert (rows[data["p1"]]["received_paise"], rows[data["p1"]]["minutes"]) == (300_000, 180)  # no fan-out
    assert rows[data["p1"]]["effective_rate_paise"] == 100_000          # Rs 1,000/h
    assert rows[data["p1"]]["quoted_rate_paise"] == 150_000
    assert rows[data["p2"]]["effective_rate_paise"] == 800_000          # Rs 4,000 / 0.5 h
    assert rows[data["p3"]]["effective_rate_paise"] is None             # 0 hours: no division by zero


def test_effective_rate_ranked_lowest_first_nulls_last(conn, data):
    order = [r["project_id"] for r in run(conn, "effective_rate_by_project", data["user"])]
    assert order == [data["p1"], data["p2"], data["p3"]]


def test_effective_rate_per_client(conn, data):
    rows = run(conn, "effective_rate_by_client", data["user"])
    assert [r["client_id"] for r in rows] == [data["a"], data["b"]]
    a = rows[0]
    # Client A: Rs 7,000 received over 210 minutes = Rs 2,000/h
    assert (a["received_paise"], a["minutes"], a["effective_rate_paise"]) == (700_000, 210, 200_000)
    assert rows[1]["effective_rate_paise"] is None


def test_effective_rate_rounds_to_nearest_paisa(conn):
    user = make_user(conn)
    project = make_project(conn, make_client(conn, user))
    add_log(conn, project, 7)
    add_payment(conn, add_invoice(conn, project, 1_000), 1_000)  # Rs 10 for 7 minutes
    rate = run(conn, "effective_rate_by_project", user)[0]["effective_rate_paise"]
    assert rate == 8_571  # 1000 * 60 / 7 = 8571.43 -> 8571 paise = Rs 85.71/h


# --- dashboard totals --------------------------------------------------------------------

def test_dashboard_totals(conn):
    user = make_user(conn)
    project = make_project(conn, make_client(conn, user))
    partial = add_invoice(conn, project, 100_000, status="sent", due="2026-12-01")
    add_payment(conn, partial, 30_000, paid_on="2026-11-05")      # this month
    late = add_invoice(conn, project, 50_000, status="sent", due="2026-11-10")  # past due
    add_payment(conn, late, 10_000, paid_on="2026-10-20")          # last month
    add_invoice(conn, project, 70_000, status="draft")             # not owed yet
    paid = add_invoice(conn, project, 20_000, status="paid")
    add_payment(conn, paid, 20_000, paid_on="2026-11-01")          # this month
    add_invoice(conn, project, 90_000, status="cancelled")

    data = dashboard_data(conn, user, TODAY)
    totals = data["totals"]
    assert totals["earned_this_month_paise"] == 30_000 + 20_000
    assert totals["outstanding_paise"] == 70_000 + 40_000      # both balances, partial counted net
    assert totals["overdue_paise"] == 40_000                    # refresh_overdue ran first
    assert conn.execute("SELECT status FROM invoices WHERE id = ?", (late,)).fetchone()[0] == "overdue"


def test_dashboard_for_new_user_is_all_zero(conn):
    user = make_user(conn)
    data = dashboard_data(conn, user, TODAY)
    assert tuple(data["totals"]) == (0, 0, 0)
    assert data["by_project"] == [] and data["charts"]["hours"]["labels"] == []
    assert len(data["income"]) == 12


# --- the other reports ------------------------------------------------------------

def test_monthly_income_has_12_months_including_empty_ones(conn, data):
    rows = run(conn, "monthly_income", data["user"], TODAY.isoformat())
    assert [r["month"] for r in rows][0] == "2025-12" and rows[-1]["month"] == "2026-11"
    assert len(rows) == 12
    by_month = {r["month"]: r["income_paise"] for r in rows}
    assert by_month["2026-10"] == 700_000                       # other user's Rs 99,000 excluded
    assert sum(by_month.values()) == 700_000


def test_monthly_income_month_boundaries(conn):
    user = make_user(conn)
    invoice = add_invoice(conn, make_project(conn, make_client(conn, user)), 300, issue="2026-01-01", due="2026-01-01")
    for day in ("2026-10-31", "2026-11-01", "2026-11-30"):
        add_payment(conn, invoice, 100, paid_on=day)
    by_month = {r["month"]: r["income_paise"]
                for r in run(conn, "monthly_income", user, "2026-11-30")}
    assert (by_month["2026-10"], by_month["2026-11"]) == (100, 200)


def test_overdue_invoices_days(conn):
    user = make_user(conn)
    project = make_project(conn, make_client(conn, user))
    inv = add_invoice(conn, project, 10_000, status="sent", due="2026-11-09")
    add_payment(conn, inv, 2_500)
    add_invoice(conn, project, 10_000, status="paid", due="2026-11-01")
    add_invoice(conn, project, 10_000, status="sent", due="2026-11-19")   # due today: not overdue
    rows = run(conn, "overdue_invoices", user, TODAY.isoformat())
    assert [(r["days_overdue"], r["balance_paise"]) for r in rows] == [(10, 7_500)]


def test_unbilled_hours_having(conn):
    user = make_user(conn)
    client = make_client(conn, user)
    hourly = make_project(conn, client, rate=120_000)
    billed_only = make_project(conn, client)
    fixed = make_project(conn, client, billing_type="fixed", rate=None, fixed=1000)
    add_log(conn, hourly, 90)
    add_log(conn, hourly, 45)
    inv = add_invoice(conn, billed_only, 100)
    add_log(conn, billed_only, 60, invoice=inv)
    add_log(conn, fixed, 60)
    rows = run(conn, "unbilled_hours", user)
    assert [(r["project_id"], r["entries"], r["minutes"], r["unbilled_value_paise"]) for r in rows] == \
        [(hourly, 2, 135, 270_000)]                 # 2.25 h x Rs 1,200


def test_top_clients_window_functions(conn, data):
    rows = run(conn, "top_clients", data["user"])
    assert [r["rank"] for r in rows] == [1]          # B has no revenue, other user excluded
    assert rows[0]["share_pct"] == 100.0

    c = make_client(conn, data["user"], "C")
    add_payment(conn, add_invoice(conn, make_project(conn, c), 300_000), 300_000)
    rows = run(conn, "top_clients", data["user"])
    assert [(r["rank"], r["client_name"], r["share_pct"], r["running_total_paise"]) for r in rows] == \
        [(1, "A", 70.0, 700_000), (2, "C", 30.0, 1_000_000)]


def test_projects_needing_attention(conn, data):
    conn.execute("UPDATE projects SET estimated_hours = 1 WHERE id = ?", (data["p2"],))  # 0.5 h logged
    rows = run(conn, "projects_needing_attention", data["user"], 150_000)  # threshold Rs 1,500/h
    assert [(r["project_id"], r["reason"]) for r in rows] == [(data["p1"], "Low effective rate")]

    conn.execute("UPDATE projects SET estimated_hours = 2 WHERE id = ?", (data["p1"],))  # 3 h logged
    rows = run(conn, "projects_needing_attention", data["user"], 150_000)
    assert rows[0]["reason"] == "Over estimate and low rate"
    rows = run(conn, "projects_needing_attention", data["user"], 50_000)
    assert [(r["project_id"], r["reason"]) for r in rows] == [(data["p1"], "Over estimate")]


def test_reports_file_has_all_named_queries():
    queries = load_queries()
    assert set(queries) == {
        "effective_rate_by_project", "effective_rate_by_client", "overdue_invoices",
        "monthly_income", "unbilled_hours", "top_clients", "projects_needing_attention",
        "dashboard_totals"}
    for name, q in queries.items():
        assert q["sql"].endswith(";") and q["comment"], name
        assert "?1" in q["sql"], f"{name} must be scoped to the user"


# --- web ---------------------------------------------------------------------------------

def test_dashboard_and_reports_pages(make_browser):
    browser = make_browser("a@example.com")
    assert browser.get("/").headers["location"] == "/dashboard"
    create_client_via_web(browser, "Acme")
    page = browser.get("/dashboard").text
    assert "Effective hourly rate = total payments received ÷ total hours logged" in page
    assert "Acme" in page and 'id="incomeChart"' in page
    page = browser.get("/reports?threshold=1200").text
    assert "Top clients by revenue" in page and 'value="1200.00"' in page
    assert browser.get("/reports?threshold=abc").status_code == 200  # bad input -> default


def test_dashboard_requires_login(make_browser):
    browser = make_browser()
    assert browser.get("/dashboard").status_code == 303
    assert browser.get("/reports").status_code == 303
