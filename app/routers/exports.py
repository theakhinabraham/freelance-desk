"""CSV downloads: /export/payments.csv and /export/time_logs.csv."""

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app import db
from app.auth import current_user
from app.services import exports as svc

router = APIRouter(prefix="/export")


def _csv_response(text: str, name: str) -> Response:
    filename = f"freelancedesk-{name}-{db.now_ist().date().isoformat()}.csv"
    # utf-8-sig adds a byte-order mark, so Excel opens names with
    # non-English characters correctly instead of showing garbled text.
    return Response(
        content=text.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/payments.csv")
def payments_csv(user=Depends(current_user), conn: sqlite3.Connection = Depends(db.get_db)):
    return _csv_response(svc.payments_csv(conn, user["id"]), "payments")


@router.get("/time_logs.csv")
def time_logs_csv(user=Depends(current_user), conn: sqlite3.Connection = Depends(db.get_db)):
    return _csv_response(svc.time_logs_csv(conn, user["id"]), "time-logs")
