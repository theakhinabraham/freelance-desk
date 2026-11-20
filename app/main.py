"""FreelanceDesk FastAPI application.  Run with:  uvicorn app.main:app --reload"""

import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import auth, db
from app.routers import clients, dashboard, exports, invoices, projects, time
from app.templating import STATIC_DIR, render, templates

# The key that signs session cookies. Set FREELANCEDESK_SECRET in production;
# anyone who knows the key could forge a login cookie.
SECRET_KEY = os.environ.get("FREELANCEDESK_SECRET", "dev-only-secret-change-me")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the tables on startup if they do not exist yet.
    conn = db.connect()
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="FreelanceDesk", lifespan=lifespan)
# same_site="lax": the browser does not send the cookie on POSTs from other
# websites, which blocks basic cross-site request forgery (CSRF).
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax",
                   max_age=7 * 24 * 3600)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(clients.router)
app.include_router(projects.router)
app.include_router(time.router)
app.include_router(invoices.router)
app.include_router(dashboard.router)
app.include_router(exports.router)


@app.exception_handler(auth.LoginRequired)
async def login_required_handler(request: Request, exc: auth.LoginRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    """Show errors such as 404 as an HTML page instead of JSON."""
    return render(request, "error.html", status_code=exc.status_code,
                  code=exc.status_code, message=exc.detail)


@app.get("/")
def index(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/health")
def health(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """Diagnostic page: confirms the database is reachable and correctly configured.

    Shows row counts only, never data. Table names cannot be ? parameters,
    so the counts are one fixed UNION ALL query, not a loop that builds SQL.
    """
    counts = conn.execute(
        """
        SELECT 'users'     AS table_name, COUNT(*) AS row_count FROM users
        UNION ALL SELECT 'clients',   COUNT(*) FROM clients
        UNION ALL SELECT 'projects',  COUNT(*) FROM projects
        UNION ALL SELECT 'time_logs', COUNT(*) FROM time_logs
        UNION ALL SELECT 'invoices',  COUNT(*) FROM invoices
        UNION ALL SELECT 'payments',  COUNT(*) FROM payments
        """
    ).fetchall()
    foreign_keys_on = conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "counts": counts,
            "foreign_keys_on": foreign_keys_on,
            "sqlite_version": sqlite3.sqlite_version,
            "db_path": db.get_db_path(),
        },
    )
