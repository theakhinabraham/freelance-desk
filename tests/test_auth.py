"""Register, login, logout, and protection of private pages."""

from app.auth import hash_password, verify_password
from tests.conftest import db_rows


def test_password_hash_is_salted_and_verifiable():
    h1, h2 = hash_password("secret123"), hash_password("secret123")
    assert h1 != h2                      # different random salts
    assert "secret123" not in h1
    assert verify_password("secret123", h1)
    assert not verify_password("wrong", h1)
    assert not verify_password("secret123", "garbage")


def test_private_pages_redirect_to_login(make_browser):
    browser = make_browser()
    for url in ("/clients", "/projects", "/clients/new", "/projects/1"):
        response = browser.get(url)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_register_logs_in_and_stores_hash(make_browser, web_db):
    browser = make_browser("asha@example.com", name="Asha")
    assert browser.get("/clients").status_code == 200
    row = db_rows(web_db, "SELECT name, password_hash FROM users")[0]
    assert row["name"] == "Asha"
    assert row["password_hash"].startswith("pbkdf2_sha256$")


def test_register_validation_errors(make_browser):
    browser = make_browser()
    response = browser.post("/register", data={
        "name": "", "email": "not-an-email", "password": "short", "confirm_password": "short"})
    assert response.status_code == 400
    assert "Name is required" in response.text
    assert "valid email" in response.text
    assert "at least 8 characters" in response.text


def test_duplicate_email_rejected_case_insensitive(make_browser):
    make_browser("asha@example.com")
    response = make_browser().post("/register", data={
        "name": "Other", "email": "ASHA@example.com",
        "password": "password123", "confirm_password": "password123"})
    assert response.status_code == 400
    assert "already exists" in response.text


def test_login_and_logout(make_browser):
    make_browser("asha@example.com", password="password123")
    browser = make_browser()
    response = browser.post("/login", data={"email": "Asha@Example.com", "password": "password123"})
    assert response.status_code == 303
    assert browser.get("/clients").status_code == 200

    assert browser.post("/logout").status_code == 303
    assert browser.get("/clients").status_code == 303  # back to the login redirect


def test_bad_login_same_message_for_unknown_email_and_wrong_password(make_browser):
    make_browser("asha@example.com", password="password123")
    browser = make_browser()
    wrong_pw = browser.post("/login", data={"email": "asha@example.com", "password": "nope"})
    unknown = browser.post("/login", data={"email": "who@example.com", "password": "nope"})
    for response in (wrong_pw, unknown):
        assert response.status_code == 400
        assert "Invalid email or password" in response.text
    assert browser.get("/clients").status_code == 303


def test_tampered_session_cookie_is_rejected(make_browser):
    browser = make_browser("asha@example.com")
    browser.cookies.set("session", browser.cookies.get("session")[:-4] + "AAAA")
    assert browser.get("/clients").status_code == 303
