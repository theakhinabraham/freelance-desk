"""Time tracking pages: timer start/stop, manual entry, today/week totals, recent entries."""

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, Request

from app import db
from app.auth import current_user, redirect
from app.forms import validate_manual_log
from app.services import time_logs as svc
from app.templating import flash, render

router = APIRouter(prefix="/time")


def _back(form) -> str:
    """Where to go after a timer action: back to the project page, or to /time.

    Only a project id is accepted (never a free-form URL), so this cannot be
    abused to redirect users to another website.
    """
    project_id = (form.get("return_to_project") or "").strip()
    return f"/projects/{project_id}" if project_id.isdigit() else "/time"


def _page(request, user, conn, values=None, errors=None, status_code=200):
    now = db.now_ist()
    running = svc.running_timer(conn, user["id"])
    elapsed = None
    if running:
        elapsed = svc.minutes_between(datetime.fromisoformat(running["started_at"]), now)
    return render(
        request, "time/index.html", status_code=status_code, user=user,
        running=running, elapsed=elapsed,
        summary=svc.hours_summary(conn, user["id"], now),
        by_project=svc.week_by_project(conn, user["id"], now),
        projects=svc.trackable_projects(conn, user["id"]),
        logs=svc.list_logs(conn, user["id"]),
        today=now.date().isoformat(),
        values=values or {}, errors=errors or {},
    )


@router.get("")
def time_page(request: Request, user=Depends(current_user),
              conn: sqlite3.Connection = Depends(db.get_db)):
    return _page(request, user, conn)


@router.post("/start")
async def start(request: Request, user=Depends(current_user),
                conn: sqlite3.Connection = Depends(db.get_db)):
    form = await request.form()
    project_id = (form.get("project_id") or "").strip()
    if not project_id.isdigit():
        flash(request, "Choose a project to start the timer.", "bad")
        return redirect(_back(form))
    description = (form.get("description") or "").strip() or None
    error = svc.start_timer(conn, user["id"], int(project_id), description)
    flash(request, error or "Timer started.", "bad" if error else "ok")
    return redirect(_back(form))


@router.post("/stop")
async def stop(request: Request, user=Depends(current_user),
               conn: sqlite3.Connection = Depends(db.get_db)):
    form = await request.form()
    error, minutes = svc.stop_timer(conn, user["id"])
    if error:
        flash(request, error, "bad")
    else:
        hours, mins = divmod(minutes, 60)
        flash(request, f"Timer stopped: {hours}h {mins:02d}m logged.")
    return redirect(_back(form))


@router.post("/manual")
async def manual(request: Request, user=Depends(current_user),
                 conn: sqlite3.Connection = Depends(db.get_db)):
    form = await request.form()
    data, errors = validate_manual_log(form)
    if not errors:
        error = svc.add_manual_log(conn, user["id"], data)
        if error is None:
            flash(request, "Time entry added.")
            return redirect("/time")
        errors["form"] = error
    # Show the page again with the errors and the typed values.
    return _page(request, user, conn, values=dict(form), errors=errors, status_code=400)


@router.post("/{log_id}/delete")
async def delete(log_id: int, request: Request, user=Depends(current_user),
                 conn: sqlite3.Connection = Depends(db.get_db)):
    form = await request.form()
    error = svc.delete_log(conn, user["id"], log_id)
    flash(request, error or "Time entry deleted.", "bad" if error else "ok")
    return redirect(_back(form))
