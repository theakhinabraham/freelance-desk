"""Client pages: list, create, view, edit, delete. Every route requires login."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from app import db
from app.auth import current_user, redirect
from app.forms import validate_client
from app.services import clients as svc
from app.services.projects import list_projects
from app.templating import flash, render

router = APIRouter(prefix="/clients")


def _get_or_404(conn, user, client_id):
    client = svc.get_client(conn, user["id"], client_id)
    if client is None:
        # The same 404 for "does not exist" and "belongs to another user",
        # so nobody can probe which ids exist.
        raise HTTPException(status_code=404, detail="Client not found")
    return client


@router.get("")
def list_page(request: Request, user=Depends(current_user),
              conn: sqlite3.Connection = Depends(db.get_db)):
    return render(request, "clients/list.html", user=user,
                  clients=svc.list_clients(conn, user["id"]))


@router.get("/new")
def new_form(request: Request, user=Depends(current_user)):
    return render(request, "clients/form.html", user=user, client=None, values={}, errors={})


@router.post("/new")
async def create(request: Request, user=Depends(current_user),
                 conn: sqlite3.Connection = Depends(db.get_db)):
    data, errors = validate_client(await request.form())
    if errors:
        return render(request, "clients/form.html", status_code=400, user=user,
                      client=None, values=data, errors=errors)
    client_id = svc.create_client(conn, user["id"], data)
    flash(request, f"Client '{data['name']}' added.")
    return redirect(f"/clients/{client_id}")


@router.get("/{client_id}")
def detail(client_id: int, request: Request, user=Depends(current_user),
           conn: sqlite3.Connection = Depends(db.get_db)):
    client = _get_or_404(conn, user, client_id)
    projects = list_projects(conn, user["id"], client_id=client_id)
    return render(request, "clients/detail.html", user=user, client=client, projects=projects)


@router.get("/{client_id}/edit")
def edit_form(client_id: int, request: Request, user=Depends(current_user),
              conn: sqlite3.Connection = Depends(db.get_db)):
    client = _get_or_404(conn, user, client_id)
    return render(request, "clients/form.html", user=user, client=client,
                  values=dict(client), errors={})


@router.post("/{client_id}/edit")
async def update(client_id: int, request: Request, user=Depends(current_user),
                 conn: sqlite3.Connection = Depends(db.get_db)):
    client = _get_or_404(conn, user, client_id)
    data, errors = validate_client(await request.form())
    if errors:
        return render(request, "clients/form.html", status_code=400, user=user,
                      client=client, values=data, errors=errors)
    svc.update_client(conn, user["id"], client_id, data)
    flash(request, "Client updated.")
    return redirect(f"/clients/{client_id}")


@router.post("/{client_id}/delete")
def delete(client_id: int, request: Request, user=Depends(current_user),
           conn: sqlite3.Connection = Depends(db.get_db)):
    client = _get_or_404(conn, user, client_id)
    error = svc.delete_client(conn, user["id"], client_id)
    if error:
        flash(request, f"Cannot delete '{client['name']}': {error}", "bad")
        return redirect(f"/clients/{client_id}")
    flash(request, f"Client '{client['name']}' deleted.")
    return redirect("/clients")
