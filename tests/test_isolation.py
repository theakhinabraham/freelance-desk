"""User data isolation: user B must never see or change user A's data."""

from app.services.clients import list_clients
from app.services.projects import list_projects
from app import db
from tests.conftest import create_client_via_web, create_project_via_web, db_rows


def setup_two_users(make_browser):
    alice = make_browser("alice@example.com", name="Alice")
    bob = make_browser("bob@example.com", name="Bob")
    client_id = create_client_via_web(alice, "Alice Secret Client")
    project_id = create_project_via_web(alice, client_id, title="Alice Secret Project")
    return alice, bob, client_id, project_id


def test_lists_show_only_own_data(make_browser):
    alice, bob, _, _ = setup_two_users(make_browser)
    assert "Alice Secret Client" in alice.get("/clients").text
    assert "Alice Secret Client" not in bob.get("/clients").text
    assert "Alice Secret Project" not in bob.get("/projects").text


def test_other_users_records_are_404(make_browser):
    _, bob, client_id, project_id = setup_two_users(make_browser)
    for url in (f"/clients/{client_id}", f"/clients/{client_id}/edit",
                f"/projects/{project_id}", f"/projects/{project_id}/edit"):
        assert bob.get(url).status_code == 404, url


def test_cannot_edit_or_delete_other_users_records(make_browser, web_db):
    _, bob, client_id, project_id = setup_two_users(make_browser)
    assert bob.post(f"/clients/{client_id}/edit", data={"name": "Hacked"}).status_code == 404
    assert bob.post(f"/projects/{project_id}/delete").status_code == 404
    assert bob.post(f"/projects/{project_id}/edit", data={
        "client_id": client_id, "title": "Hacked", "billing_type": "hourly", "hourly_rate": "1"
    }).status_code == 404
    assert bob.post(f"/clients/{client_id}/delete").status_code == 404

    assert db_rows(web_db, "SELECT name FROM clients")[0]["name"] == "Alice Secret Client"
    assert db_rows(web_db, "SELECT title FROM projects")[0]["title"] == "Alice Secret Project"


def test_cannot_attach_project_to_other_users_client(make_browser, web_db):
    _, bob, client_id, _ = setup_two_users(make_browser)
    create_client_via_web(bob, "Bob Client")  # Bob has a client, but uses Alice's id
    response = bob.post("/projects/new", data={
        "client_id": client_id, "title": "Sneaky", "billing_type": "hourly", "hourly_rate": "100"})
    assert response.status_code == 400
    assert "Choose one of your clients" in response.text
    assert len(db_rows(web_db, "SELECT id FROM projects")) == 1


def test_cannot_move_own_project_to_other_users_client(make_browser, web_db):
    _, bob, alice_client, _ = setup_two_users(make_browser)
    bob_client = create_client_via_web(bob, "Bob Client")
    bob_project = create_project_via_web(bob, bob_client)
    response = bob.post(f"/projects/{bob_project}/edit", data={
        "client_id": alice_client, "title": "Moved", "billing_type": "hourly", "hourly_rate": "1"})
    assert response.status_code == 400
    row = db_rows(web_db, "SELECT client_id FROM projects WHERE id = ?", (bob_project,))[0]
    assert row["client_id"] == bob_client


def test_service_layer_scoping(make_browser, web_db):
    setup_two_users(make_browser)
    conn = db.connect(web_db)
    try:
        alice_id, bob_id = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
        assert len(list_clients(conn, alice_id)) == 1
        assert list_clients(conn, bob_id) == []
        assert list_projects(conn, bob_id) == []
    finally:
        conn.close()
