"""Turn submitted HTML form text into clean values, collecting friendly errors.

Each validate_* function returns (data, errors):
  data   - a dict of cleaned values, ready to pass to the SQL layer
  errors - a dict of field name -> message (empty if the form is valid)
"""

import re
from datetime import date, datetime, time

from app.services.payments import PAYMENT_METHODS

PROJECT_STATUSES = ("active", "completed", "on_hold", "cancelled")
BILLING_TYPES = ("hourly", "fixed")
RUPEES_PATTERN = re.compile(r"[0-9]+(\.[0-9]{1,2})?")  # 1500 | 1500.5 | 1500.50


def clean(text) -> str:
    return (text or "").strip()


def parse_rupees(text) -> int | None:
    """'1,500.50' -> 150050 paise, using only integer arithmetic (no float anywhere).

    Accepts digits with an optional '.' and 1-2 decimals. Returns None for an
    empty field. Raises ValueError with a user-facing message.
    """
    text = clean(text).replace(",", "").replace("₹", "")
    if not text:
        return None
    if not RUPEES_PATTERN.fullmatch(text):
        raise ValueError("Enter an amount like 1500 or 1500.50.")
    rupees, _, decimals = text.partition(".")
    paise = int(rupees) * 100 + int(decimals.ljust(2, "0"))  # "1500.5" -> 150050
    if paise <= 0:
        raise ValueError("Amount must be greater than zero.")
    return paise


def parse_date(text) -> str | None:
    """'2026-11-19' -> '2026-11-19' after checking it is a real date. Empty -> None."""
    text = clean(text)
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError("Enter a valid date (YYYY-MM-DD).")


def parse_positive_int(text) -> int | None:
    text = clean(text)
    if not text:
        return None
    if not text.isdigit() or int(text) == 0:
        raise ValueError("Enter a whole number greater than zero.")
    return int(text)


def validate_registration(form) -> tuple[dict, dict]:
    data = {
        "name": clean(form.get("name")),
        "email": clean(form.get("email")).lower(),
        "password": form.get("password") or "",
    }
    errors = {}
    if not data["name"]:
        errors["name"] = "Name is required."
    if "@" not in data["email"] or "." not in data["email"].split("@")[-1]:
        errors["email"] = "Enter a valid email address."
    if len(data["password"]) < 8:
        errors["password"] = "Password must be at least 8 characters."
    elif data["password"] != (form.get("confirm_password") or ""):
        errors["confirm_password"] = "Passwords do not match."
    return data, errors


def validate_client(form) -> tuple[dict, dict]:
    data = {
        "name": clean(form.get("name")),
        "email": clean(form.get("email")) or None,
        "phone": clean(form.get("phone")) or None,
        "notes": clean(form.get("notes")) or None,
    }
    errors = {}
    if not data["name"]:
        errors["name"] = "Client name is required."
    if data["email"] and "@" not in data["email"]:
        errors["email"] = "Enter a valid email address."
    return data, errors


def validate_project(form) -> tuple[dict, dict]:
    errors = {}
    data = {
        "client_id": clean(form.get("client_id")),
        "title": clean(form.get("title")),
        "billing_type": clean(form.get("billing_type")),
        "status": clean(form.get("status")) or "active",
    }
    if not data["client_id"].isdigit():
        errors["client_id"] = "Choose a client."
    else:
        data["client_id"] = int(data["client_id"])
    if not data["title"]:
        errors["title"] = "Title is required."
    if data["billing_type"] not in BILLING_TYPES:
        errors["billing_type"] = "Choose hourly or fixed."
    if data["status"] not in PROJECT_STATUSES:
        errors["status"] = "Choose a valid status."

    # Each parser raises ValueError with a message for the user.
    for field, parser in [
        ("hourly_rate_paise", lambda: parse_rupees(form.get("hourly_rate"))),
        ("fixed_price_paise", lambda: parse_rupees(form.get("fixed_price"))),
        ("estimated_hours", lambda: parse_positive_int(form.get("estimated_hours"))),
        ("start_date", lambda: parse_date(form.get("start_date"))),
        ("deadline", lambda: parse_date(form.get("deadline"))),
    ]:
        try:
            data[field] = parser()
        except ValueError as exc:
            data[field] = None
            errors[field] = str(exc)

    # Keep only the price that matches the billing type, so the row always
    # satisfies the table's CHECK constraint.
    if data["billing_type"] == "hourly":
        data["fixed_price_paise"] = None
        if data["hourly_rate_paise"] is None and "hourly_rate_paise" not in errors:
            errors["hourly_rate_paise"] = "Hourly projects need an hourly rate."
    elif data["billing_type"] == "fixed":
        data["hourly_rate_paise"] = None
        if data["fixed_price_paise"] is None and "fixed_price_paise" not in errors:
            errors["fixed_price_paise"] = "Fixed-price projects need a price."

    if data["start_date"] and data["deadline"] and data["deadline"] < data["start_date"]:
        errors["deadline"] = "Deadline cannot be before the start date."
    return data, errors


def validate_manual_log(form) -> tuple[dict, dict]:
    """Manual time entry: one date plus a start and end time on that date."""
    errors = {}
    data = {"description": clean(form.get("description")) or None}
    project_id = clean(form.get("project_id"))
    if project_id.isdigit():
        data["project_id"] = int(project_id)
    else:
        errors["project_id"] = "Choose a project."

    try:
        day = parse_date(form.get("date"))
        if day is None:
            raise ValueError("Date is required.")
    except ValueError as exc:
        errors["date"] = str(exc)
        day = None

    times = {}
    for field in ("start_time", "end_time"):
        try:
            times[field] = time.fromisoformat(clean(form.get(field)))  # "09:30"
        except ValueError:
            errors[field] = "Enter a time like 09:30."

    if day and len(times) == 2:
        d = date.fromisoformat(day)
        data["started"] = datetime.combine(d, times["start_time"]).replace(second=0, microsecond=0)
        data["ended"] = datetime.combine(d, times["end_time"]).replace(second=0, microsecond=0)
        if data["ended"] <= data["started"]:
            errors["end_time"] = "End time must be after the start time."
    return data, errors


def validate_invoice(form) -> tuple[dict, dict]:
    errors = {}
    data = {"status": "sent" if form.get("action") == "send" else "draft"}
    for field in ("issue_date", "due_date"):
        try:
            data[field] = parse_date(form.get(field))
            if data[field] is None:
                errors[field] = "This date is required."
        except ValueError as exc:
            data[field] = None
            errors[field] = str(exc)
    if data["issue_date"] and data["due_date"] and data["due_date"] < data["issue_date"]:
        errors["due_date"] = "Due date cannot be before the issue date."
    try:
        data["amount_paise"] = parse_rupees(form.get("amount"))
        if data["amount_paise"] is None:
            errors["amount"] = "Amount is required."
    except ValueError as exc:
        data["amount_paise"] = None
        errors["amount"] = str(exc)
    return data, errors


def validate_payment(form) -> tuple[dict, dict]:
    errors = {}
    data = {
        "method": clean(form.get("method")),
        "reference": clean(form.get("reference")) or None,
    }
    if data["method"] not in PAYMENT_METHODS:
        errors["method"] = "Choose a payment method."
    try:
        data["amount_paise"] = parse_rupees(form.get("amount"))
        if data["amount_paise"] is None:
            errors["amount"] = "Amount is required."
    except ValueError as exc:
        data["amount_paise"] = None
        errors["amount"] = str(exc)
    try:
        data["paid_on"] = parse_date(form.get("paid_on"))
        if data["paid_on"] is None:
            errors["paid_on"] = "Payment date is required."
    except ValueError as exc:
        data["paid_on"] = None
        errors["paid_on"] = str(exc)
    return data, errors
