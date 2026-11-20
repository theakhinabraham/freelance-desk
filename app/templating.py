"""Jinja2 setup and display-only formatting. Money becomes text only here."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


def format_inr(paise: int | None) -> str:
    """12345678 paise -> '₹1,23,456.78' (Indian digit grouping: 3, then 2s)."""
    if paise is None:
        return "—"
    sign = "-" if paise < 0 else ""
    rupees, rem = divmod(abs(int(paise)), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return f"{sign}₹{digits}.{rem:02d}"


def format_hours(minutes: int | None) -> str:
    """135 -> '2h 15m'."""
    if not minutes:
        return "0h 00m"
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h {mins:02d}m"


def rupees_input(paise: int | None) -> str:
    """Value for an <input> when editing: 150000 -> '1500.00' (no ₹, no commas)."""
    if not isinstance(paise, int):  # None, or a field missing on a blank form
        return ""
    rupees, rem = divmod(paise, 100)
    return f"{rupees}.{rem:02d}"


STATUS_LABELS = {
    "active": "Active", "completed": "Completed", "on_hold": "On hold", "cancelled": "Cancelled",
    "draft": "Draft", "sent": "Sent", "paid": "Paid", "overdue": "Overdue",
}


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["inr"] = format_inr
templates.env.filters["hours"] = format_hours
templates.env.filters["rupees_input"] = rupees_input
templates.env.filters["status_label"] = lambda s: STATUS_LABELS.get(s, s)


# --- flash messages: one-time notices shown on the next page ---------------

def flash(request, message: str, category: str = "ok") -> None:
    """Store a message in the session; it is shown (and removed) on the next render."""
    flashes = request.session.get("_flashes", [])
    flashes.append({"message": message, "category": category})
    request.session["_flashes"] = flashes


def render(request, template_name: str, status_code: int = 200, **context):
    """Render a template, adding the pending flash messages."""
    context["flashes"] = request.session.pop("_flashes", [])
    return templates.TemplateResponse(request, template_name, context, status_code=status_code)
