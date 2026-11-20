"""Time tracking: timer start/stop, one-running-timer rule, manual entry, totals, isolation."""

from datetime import datetime

import pytest

from app.services import time_logs as svc
from tests.conftest import (create_client_via_web, create_project_via_web, db_rows,
                            make_client, make_project, make_user)

# Thursday 19 Nov 2026, 15:00. The week started on Monday 16 Nov.
NOW = datetime(2026, 11, 19, 15, 0, 0)


@pytest.fixture
def setup(conn):
    user = make_user(conn)
    client = make_client(conn, user)
    return user, make_project(conn, client), make_project(conn, client)


def manual(conn, user, project, start, end, description=None, now=NOW):
    return svc.add_manual_log(conn, user, {
        "project_id": project, "description": description,
        "started": datetime.fromisoformat(start), "ended": datetime.fromisoformat(end),
    }, now=now)


# --- timer -----------------------------------------------------------------------

def test_start_and_stop_records_duration(conn, setup):
    user, p1, _ = setup
    assert svc.start_timer(conn, user, p1, "Coding", now=datetime(2026, 11, 19, 10, 0, 0)) is None
    assert svc.running_timer(conn, user)["project_id"] == p1

    error, minutes = svc.stop_timer(conn, user, now=datetime(2026, 11, 19, 11, 30, 40))
    assert error is None and minutes == 91  # 1h 30m 40s rounds to 91 minutes
    row = conn.execute("SELECT started_at, ended_at, duration_minutes FROM time_logs").fetchone()
    assert tuple(row) == ("2026-11-19 10:00:00", "2026-11-19 11:30:40", 91)
    assert svc.running_timer(conn, user) is None


def test_second_timer_blocked_with_friendly_message(conn, setup):
    user, p1, p2 = setup
    svc.start_timer(conn, user, p1, None, now=NOW)
    error = svc.start_timer(conn, user, p2, None, now=NOW)
    assert "already running" in error
    assert conn.execute("SELECT COUNT(*) FROM time_logs").fetchone()[0] == 1


def test_stop_without_running_timer(conn, setup):
    user, _, _ = setup
    error, minutes = svc.stop_timer(conn, user, now=NOW)
    assert error == "No timer is running." and minutes is None


def test_stop_in_same_second_keeps_check_valid(conn, setup):
    user, p1, _ = setup
    svc.start_timer(conn, user, p1, None, now=NOW)
    error, minutes = svc.stop_timer(conn, user, now=NOW)  # same instant
    assert error is None and minutes == 0
    ended = conn.execute("SELECT ended_at FROM time_logs").fetchone()[0]
    assert ended == "2026-11-19 15:00:01"


def test_cannot_start_timer_on_other_users_project(conn, setup):
    _, p1, _ = setup
    other = make_user(conn, "b@example.com")
    assert svc.start_timer(conn, other, p1, None, now=NOW) == "Project not found."


def test_users_timers_are_independent(conn, setup):
    user, p1, _ = setup
    other = make_user(conn, "b@example.com")
    other_project = make_project(conn, make_client(conn, other))
    assert svc.start_timer(conn, user, p1, None, now=NOW) is None
    assert svc.start_timer(conn, other, other_project, None, now=NOW) is None
    svc.stop_timer(conn, other, now=datetime(2026, 11, 19, 16, 0))
    assert svc.running_timer(conn, user) is not None  # stopping B's timer left A's running


# --- manual entry ------------------------------------------------------------------

def test_manual_entry_ok(conn, setup):
    user, p1, _ = setup
    assert manual(conn, user, p1, "2026-11-18 09:30", "2026-11-18 12:15", "Design") is None
    row = conn.execute("SELECT duration_minutes, description FROM time_logs").fetchone()
    assert tuple(row) == (165, "Design")


@pytest.mark.parametrize("start, end, message", [
    ("2026-11-18 12:00", "2026-11-18 11:00", "after the start"),
    ("2026-11-18 12:00", "2026-11-18 12:00", "after the start"),
    ("2026-11-19 14:00", "2026-11-19 16:00", "future"),
])
def test_manual_entry_rejected(conn, setup, start, end, message):
    user, p1, _ = setup
    assert message in manual(conn, user, p1, start, end)


@pytest.mark.parametrize("start, end, overlaps", [
    ("2026-11-18 09:00", "2026-11-18 10:30", True),   # covers the start of the existing entry
    ("2026-11-18 10:30", "2026-11-18 10:45", True),   # inside it
    ("2026-11-18 09:00", "2026-11-18 12:00", True),   # contains it
    ("2026-11-18 11:00", "2026-11-18 12:00", False),  # starts exactly when it ends
    ("2026-11-18 08:00", "2026-11-18 10:00", False),  # ends exactly when it starts
])
def test_manual_entry_overlap(conn, setup, start, end, overlaps):
    user, p1, p2 = setup
    manual(conn, user, p1, "2026-11-18 10:00", "2026-11-18 11:00")
    error = manual(conn, user, p2, start, end)  # overlap is checked across ALL the user's projects
    assert (error is not None and "Overlaps" in error) == overlaps


def test_manual_entry_cannot_overlap_running_timer(conn, setup):
    user, p1, p2 = setup
    svc.start_timer(conn, user, p1, None, now=datetime(2026, 11, 19, 13, 0))
    assert "Overlaps" in manual(conn, user, p2, "2026-11-19 13:30", "2026-11-19 14:00")


def test_other_users_entries_do_not_count_as_overlap(conn, setup):
    user, p1, _ = setup
    other = make_user(conn, "b@example.com")
    other_project = make_project(conn, make_client(conn, other))
    manual(conn, other, other_project, "2026-11-18 10:00", "2026-11-18 11:00")
    assert manual(conn, user, p1, "2026-11-18 10:00", "2026-11-18 11:00") is None


# --- totals ----------------------------------------------------------------------

def test_hours_summary_today_and_week(conn, setup):
    user, p1, p2 = setup
    manual(conn, user, p1, "2026-11-15 10:00", "2026-11-15 12:00")  # Sunday: last week
    manual(conn, user, p1, "2026-11-16 09:00", "2026-11-16 10:00")  # Monday: 60
    manual(conn, user, p2, "2026-11-19 09:00", "2026-11-19 09:45")  # today: 45
    svc.start_timer(conn, user, p1, None, now=datetime(2026, 11, 19, 14, 30))  # 30 so far

    summary = svc.hours_summary(conn, user, now=NOW)
    assert summary["today_minutes"] == 45 + 30
    assert summary["week_minutes"] == 60 + 45 + 30

    by_project = {r["id"]: r["minutes"] for r in svc.week_by_project(conn, user, now=NOW)}
    assert by_project == {p1: 60, p2: 45}  # finished logs only


def test_hours_summary_excludes_other_users(conn, setup):
    user, p1, _ = setup
    other = make_user(conn, "b@example.com")
    manual(conn, other, make_project(conn, make_client(conn, other)),
           "2026-11-19 09:00", "2026-11-19 10:00")
    assert svc.hours_summary(conn, user, now=NOW)["week_minutes"] == 0


def test_week_starts_on_monday():
    assert svc.week_start(datetime(2026, 11, 19, 15, 0)) == datetime(2026, 11, 16)
    assert svc.week_start(datetime(2026, 11, 16, 0, 0)) == datetime(2026, 11, 16)
    assert svc.week_start(datetime(2026, 11, 22, 23, 59)) == datetime(2026, 11, 16)


# --- delete --------------------------------------------------------------------------

def test_delete_unbilled_but_not_billed_or_others(conn, setup):
    user, p1, _ = setup
    manual(conn, user, p1, "2026-11-17 10:00", "2026-11-17 11:00")
    manual(conn, user, p1, "2026-11-18 10:00", "2026-11-18 11:00")
    first, second = [r[0] for r in conn.execute("SELECT id FROM time_logs ORDER BY id")]
    invoice = conn.execute(
        """INSERT INTO invoices (project_id, invoice_number, issue_date, due_date, amount_paise)
           VALUES (?, 'FD-2026-0001', '2026-11-19', '2026-11-30', 100)""", (p1,)).lastrowid
    conn.execute("UPDATE time_logs SET invoice_id = ? WHERE id = ?", (invoice, first))

    assert "already billed" in svc.delete_log(conn, user, first)
    other = make_user(conn, "b@example.com")
    assert svc.delete_log(conn, other, second) == "Time entry not found."
    assert svc.delete_log(conn, user, second) is None
    assert [r[0] for r in conn.execute("SELECT id FROM time_logs")] == [first]


# --- web routes -------------------------------------------------------------------------

def test_timer_via_web(make_browser, web_db):
    browser = make_browser("a@example.com")
    client = create_client_via_web(browser)
    p1 = create_project_via_web(browser, client, title="Alpha")
    p2 = create_project_via_web(browser, client, title="Beta")

    assert browser.post("/time/start", data={"project_id": p1}).headers["location"] == "/time"
    page = browser.get("/time").text
    assert "Timer started" in page and "Timer running" in page

    # Starting a second one, from the project page, is refused.
    response = browser.post("/time/start", data={"project_id": p2, "return_to_project": p2})
    assert response.headers["location"] == f"/projects/{p2}"
    assert "already running on &#39;Alpha&#39;" in browser.get(f"/projects/{p2}").text

    browser.post("/time/stop", data={})
    assert "Timer stopped" in browser.get("/time").text
    assert db_rows(web_db, "SELECT COUNT(*) FROM time_logs WHERE ended_at IS NULL")[0][0] == 0


def test_manual_entry_via_web_shows_errors(make_browser, web_db):
    browser = make_browser("a@example.com")
    project = create_project_via_web(browser, create_client_via_web(browser))
    response = browser.post("/time/manual", data={
        "project_id": project, "date": "2026-11-18", "start_time": "11:00", "end_time": "10:00"})
    assert response.status_code == 400
    assert "End time must be after the start time" in response.text
    assert 'value="11:00"' in response.text  # typed value kept

    response = browser.post("/time/manual", data={
        "project_id": project, "date": "2026-11-18", "start_time": "10:00", "end_time": "11:30",
        "description": "Planning"})
    assert response.status_code == 303
    assert db_rows(web_db, "SELECT duration_minutes FROM time_logs")[0][0] == 90


def test_return_to_only_accepts_project_ids(make_browser):
    browser = make_browser("a@example.com")
    response = browser.post("/time/stop", data={"return_to_project": "//evil.example"})
    assert response.headers["location"] == "/time"


def test_form_attributes_render_as_html(make_browser):
    """Regression test: extra input attributes must not be HTML-escaped."""
    browser = make_browser("a@example.com")
    create_project_via_web(browser, create_client_via_web(browser))
    browser.get("/time")  # consume pending flash messages
    page = browser.get("/time").text
    assert 'max="' in page and "&#34;" not in page
    assert 'autocomplete="email"' in make_browser().get("/login").text
