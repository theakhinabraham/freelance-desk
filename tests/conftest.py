"""Shared fixtures. Every test gets a brand-new in-memory database."""

import pytest

from app import db


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


# --- small helpers to build rows with as little noise as possible ---------

def make_user(conn, email="a@example.com"):
    return conn.execute(
        "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
        ("Test User", email, "x"),
    ).lastrowid


def make_client(conn, user_id, name="Client"):
    return conn.execute(
        "INSERT INTO clients (user_id, name) VALUES (?, ?)", (user_id, name)
    ).lastrowid


def make_project(conn, client_id, billing_type="hourly", rate=100_000, fixed=None):
    return conn.execute(
        """
        INSERT INTO projects (client_id, title, billing_type, hourly_rate_paise, fixed_price_paise)
        VALUES (?, ?, ?, ?, ?)
        """,
        (client_id, "Project", billing_type, rate, fixed),
    ).lastrowid


def start_timer(conn, project_id, started_at="2026-11-19 10:00:00"):
    return conn.execute(
        "INSERT INTO time_logs (project_id, started_at) VALUES (?, ?)",
        (project_id, started_at),
    ).lastrowid


# --- web-level fixtures -----------------------------------------------------

@pytest.fixture
def web_db(tmp_path, monkeypatch):
    """Point the app at a temp-file database and make password hashing fast."""
    from app import auth

    path = tmp_path / "test.db"
    monkeypatch.setenv("FREELANCEDESK_DB", str(path))
    # Fewer PBKDF2 rounds in tests only. The real value is stored inside
    # each hash, so verify_password still works.
    monkeypatch.setattr(auth, "ITERATIONS", 1_000)
    return str(path)


@pytest.fixture
def make_browser(web_db):
    """Factory for TestClients. Each one has its own cookie jar, i.e. its own browser."""
    from fastapi.testclient import TestClient

    from app.main import app

    clients = []

    def _make(email=None, name="Test User", password="password123"):
        browser = TestClient(app, follow_redirects=False)
        browser.__enter__()  # runs startup (creates tables)
        clients.append(browser)
        if email:
            response = browser.post("/register", data={
                "name": name, "email": email,
                "password": password, "confirm_password": password,
            })
            assert response.status_code == 303, response.text
        return browser

    yield _make
    for browser in clients:
        browser.__exit__(None, None, None)


def db_rows(web_db, sql, params=()):
    """Read the test database directly, to check what the web layer did."""
    connection = db.connect(web_db)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def create_client_via_web(browser, name="Acme"):
    response = browser.post("/clients/new", data={"name": name})
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def create_project_via_web(browser, client_id, **overrides):
    form = {"client_id": client_id, "title": "Website", "billing_type": "hourly",
            "hourly_rate": "1500", "status": "active"}
    form.update(overrides)
    response = browser.post("/projects/new", data=form)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])
