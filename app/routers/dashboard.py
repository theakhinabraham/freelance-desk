"""Dashboard (home page after login) and the Reports page."""

import sqlite3

from fastapi import APIRouter, Depends, Request

from app import db
from app.auth import current_user
from app.forms import parse_rupees
from app.reports import load_queries
from app.services.dashboard import DEFAULT_RATE_THRESHOLD_PAISE, dashboard_data, reports_data
from app.templating import render

router = APIRouter()


@router.get("/dashboard")
def dashboard(request: Request, user=Depends(current_user),
              conn: sqlite3.Connection = Depends(db.get_db)):
    data = dashboard_data(conn, user["id"], db.now_ist().date())
    return render(request, "dashboard.html", user=user, **data)


@router.get("/reports")
def reports(request: Request, threshold: str | None = None, user=Depends(current_user),
            conn: sqlite3.Connection = Depends(db.get_db)):
    try:
        threshold_paise = parse_rupees(threshold) or DEFAULT_RATE_THRESHOLD_PAISE
    except ValueError:
        threshold_paise = DEFAULT_RATE_THRESHOLD_PAISE
    return render(request, "reports.html", user=user, threshold_paise=threshold_paise,
                  reports=reports_data(conn, user["id"], db.now_ist().date(), threshold_paise),
                  queries=load_queries())
