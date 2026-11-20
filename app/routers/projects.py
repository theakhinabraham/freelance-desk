"""Project pages: list (with filters), create, view, edit, delete. Every route requires login."""

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from app import db
from app.auth import current_user, redirect
from app.forms import PROJECT_STATUSES, validate_project
from app.services import projects as svc
from app.services import time_logs
from app.services.invoices import list_invoices, refresh_overdue
from app.services.clients import list_clients
from app.templating import flash, render

router = APIRouter(prefix="/projects")


def _get_or_404(conn, user, project_id):
    project = svc.get_project(conn, user["id"], project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _form(request, user, conn, project, values, errors, status_code=200):
    return render(request, "projects/form.html", status_code=status_code, user=user,
                  project=project, values=values, errors=errors,
                  clients=list_clients(conn, user["id"]), statuses=PROJECT_STATUSES)


@router.get("")
def list_page(request: Request, status: str | None = None, user=Depends(current_user),
              conn: sqlite3.Connection = Depends(db.get_db)):
    if status not in PROJECT_STATUSES:
        status = None  # unknown filter value -> show all
    return render(request, "projects/list.html", user=user, status=status,
                  statuses=PROJECT_STATUSES, projects=svc.list_projects(conn, user["id"], status))


@router.get("/new")
def new_form(request: Request, client_id: int | None = None, user=Depends(current_user),
             conn: sqlite3.Connection = Depends(db.get_db)):
    values = {"client_id": client_id, "billing_type": "hourly", "status": "active"}
    return _form(request, user, conn, None, values, {})


@router.post("/new")
async def create(request: Request, user=Depends(current_user),
                 conn: sqlite3.Connection = Depends(db.get_db)):
    form = await request.form()
    data, errors = validate_project(form)
    if not errors:
        project_id = svc.create_project(conn, user["id"], data)
        if project_id is not None:
            flash(request, f"Project '{data['title']}' created.")
            return redirect(f"/projects/{project_id}")
        errors["client_id"] = "Choose one of your clients."
    # Re-show exactly what the user typed (dict(form)) next to the errors.
    return _form(request, user, conn, None, {**data, **dict(form)}, errors, status_code=400)


@router.get("/{project_id}")
def detail(project_id: int, request: Request, user=Depends(current_user),
           conn: sqlite3.Connection = Depends(db.get_db)):
    project = _get_or_404(conn, user, project_id)
    summary = svc.project_summary(conn, user["id"], project_id)
    running = time_logs.running_timer(conn, user["id"])
    elapsed = None
    if running:
        started = datetime.fromisoformat(running["started_at"])
        elapsed = time_logs.minutes_between(started, db.now_ist())
    logs = time_logs.list_logs(conn, user["id"], project_id=project_id, limit=200)
    refresh_overdue(conn, user["id"], db.now_ist().date().isoformat())
    invoices = list_invoices(conn, user["id"], project_id=project_id)
    return render(request, "projects/detail.html", user=user, project=project, summary=summary,
                  running=running, elapsed=elapsed, logs=logs, return_to_project=project_id,
                  invoices=invoices, hide_project=True)


@router.get("/{project_id}/edit")
def edit_form(project_id: int, request: Request, user=Depends(current_user),
              conn: sqlite3.Connection = Depends(db.get_db)):
    project = _get_or_404(conn, user, project_id)
    return _form(request, user, conn, project, dict(project), {})


@router.post("/{project_id}/edit")
async def update(project_id: int, request: Request, user=Depends(current_user),
                 conn: sqlite3.Connection = Depends(db.get_db)):
    project = _get_or_404(conn, user, project_id)
    form = await request.form()
    data, errors = validate_project(form)
    if not errors:
        if svc.update_project(conn, user["id"], project_id, data):
            flash(request, "Project updated.")
            return redirect(f"/projects/{project_id}")
        errors["client_id"] = "Choose one of your clients."
    return _form(request, user, conn, project, {**data, **dict(form)}, errors, status_code=400)


@router.post("/{project_id}/delete")
def delete(project_id: int, request: Request, user=Depends(current_user),
           conn: sqlite3.Connection = Depends(db.get_db)):
    project = _get_or_404(conn, user, project_id)
    error = svc.delete_project(conn, user["id"], project_id)
    if error:
        flash(request, f"Cannot delete '{project['title']}': {error}", "bad")
        return redirect(f"/projects/{project_id}")
    flash(request, f"Project '{project['title']}' deleted.")
    return redirect(f"/clients/{project['client_id']}")
