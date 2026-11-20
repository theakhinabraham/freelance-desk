"""Dashboard figures. The SQL lives in db/reports.sql and is run by name."""

import sqlite3
from datetime import date

from app.reports import run
from app.services.invoices import refresh_overdue

# Report 6 flags projects earning less than this. Rs 1,000/h, in paise.
DEFAULT_RATE_THRESHOLD_PAISE = 1_000_00


def dashboard_data(conn: sqlite3.Connection, user_id: int, today: date) -> dict:
    refresh_overdue(conn, user_id, today.isoformat())  # so "overdue" is current
    month_start = today.replace(day=1).isoformat()
    by_client = run(conn, "effective_rate_by_client", user_id)
    income = run(conn, "monthly_income", user_id, today.isoformat())
    return {
        "totals": run(conn, "dashboard_totals", user_id, month_start)[0],
        "by_project": run(conn, "effective_rate_by_project", user_id),
        "by_client": by_client,
        "income": income,
        # Chart data. Values stay in paise and minutes; the page's script
        # converts them to rupees and hours only for display.
        "charts": {
            "income": {"labels": [r["month"] for r in income],
                       "paise": [r["income_paise"] for r in income]},
            "hours": {"labels": [r["client_name"] for r in by_client if r["minutes"]],
                      "minutes": [r["minutes"] for r in by_client if r["minutes"]]},
        },
    }


def reports_data(conn: sqlite3.Connection, user_id: int, today: date,
                 threshold_paise: int) -> list[dict]:
    """All the required reports, for the Reports page (results plus their SQL)."""
    t = today.isoformat()
    return [
        {"name": "effective_rate_by_project", "title": "1a. Effective hourly rate per project",
         "rows": run(conn, "effective_rate_by_project", user_id)},
        {"name": "effective_rate_by_client", "title": "1b. Effective hourly rate per client",
         "rows": run(conn, "effective_rate_by_client", user_id)},
        {"name": "overdue_invoices", "title": "2. Overdue invoices",
         "rows": run(conn, "overdue_invoices", user_id, t)},
        {"name": "monthly_income", "title": "3. Monthly income, last 12 months",
         "rows": run(conn, "monthly_income", user_id, t)},
        {"name": "unbilled_hours", "title": "4. Unbilled hours per project",
         "rows": run(conn, "unbilled_hours", user_id)},
        {"name": "top_clients", "title": "5. Top clients by revenue (window functions)",
         "rows": run(conn, "top_clients", user_id)},
        {"name": "projects_needing_attention",
         "title": "6. Projects over estimate or below the rate threshold",
         "rows": run(conn, "projects_needing_attention", user_id, threshold_paise)},
    ]
