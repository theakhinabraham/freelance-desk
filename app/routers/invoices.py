"""Invoice pages: list, create from a project, view, send, cancel, record payment, PDF."""

import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app import db
from app.auth import current_user, redirect
from app.forms import validate_invoice, validate_payment
from app.services import invoices as svc
from app.services.payments import METHOD_LABELS, PAYMENT_METHODS, record_payment
from app.services.pdf import build_invoice_pdf
from app.services.projects import get_project
from app.templating import flash, render

router = APIRouter(prefix="/invoices")

INVOICE_STATUSES = ("draft", "sent", "overdue", "paid", "cancelled")
DEFAULT_PAYMENT_TERMS_DAYS = 15


def _today() -> str:
    return db.now_ist().date().isoformat()


def _invoice_or_404(conn, user, invoice_id):
    invoice = svc.get_invoice(conn, user["id"], invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def _project_or_404(conn, user, project_id):
    project = get_project(conn, user["id"], project_id) if project_id else None
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _parse_cutoff(text: str | None) -> str:
    """The moment the form was opened. Hours logged after it are not billed.

    Falls back to "now" if missing or invalid, and is never later than now.
    """
    now = db.now_ist()
    try:
        cutoff = datetime.fromisoformat(text or "")
    except ValueError:
        cutoff = now
    return db.to_timestamp(min(cutoff, now))


@router.get("")
def list_page(request: Request, status: str | None = None, user=Depends(current_user),
              conn: sqlite3.Connection = Depends(db.get_db)):
    svc.refresh_overdue(conn, user["id"], _today())
    if status not in INVOICE_STATUSES:
        status = None
    return render(request, "invoices/list.html", user=user, status=status,
                  statuses=INVOICE_STATUSES, invoices=svc.list_invoices(conn, user["id"], status))


def _new_form(request, user, conn, project, values, errors, status_code=200):
    cutoff = values.get("cutoff") or db.to_timestamp(db.now_ist())
    prefill = svc.invoice_prefill(conn, project, cutoff)
    values.setdefault("amount_paise", prefill["amount_paise"] or None)
    return render(request, "invoices/new.html", status_code=status_code, user=user,
                  project=project, prefill=prefill, values={**values, "cutoff": cutoff},
                  errors=errors)


@router.get("/new")
def new_form(request: Request, project_id: int | None = None, user=Depends(current_user),
             conn: sqlite3.Connection = Depends(db.get_db)):
    project = _project_or_404(conn, user, project_id)
    today = db.now_ist().date()
    values = {
        "issue_date": today.isoformat(),
        "due_date": (today + timedelta(days=DEFAULT_PAYMENT_TERMS_DAYS)).isoformat(),
    }
    return _new_form(request, user, conn, project, values, {})


@router.post("/new")
async def create(request: Request, user=Depends(current_user),
                 conn: sqlite3.Connection = Depends(db.get_db)):
    form = await request.form()
    project_id = (form.get("project_id") or "").strip()
    project = _project_or_404(conn, user, int(project_id) if project_id.isdigit() else None)
    data, errors = validate_invoice(form)
    cutoff = _parse_cutoff(form.get("cutoff"))
    if not errors:
        invoice_id, error = svc.create_invoice(conn, user["id"], project["id"], data, cutoff)
        if error is None:
            svc.refresh_overdue(conn, user["id"], _today())
            flash(request, "Invoice created." if data["status"] == "draft"
                  else "Invoice created and marked as sent.")
            return redirect(f"/invoices/{invoice_id}")
        errors["form"] = error
    return _new_form(request, user, conn, project, {**dict(form), "cutoff": cutoff},
                     errors, status_code=400)


def _detail(request, user, conn, invoice_id, values=None, errors=None, status_code=200):
    svc.refresh_overdue(conn, user["id"], _today())
    invoice = _invoice_or_404(conn, user, invoice_id)
    defaults = {"amount_paise": invoice["balance_paise"], "paid_on": _today(), "method": "upi"}
    return render(
        request, "invoices/detail.html", status_code=status_code, user=user,
        invoice=invoice, logs=svc.billed_logs(conn, invoice_id),
        payments=svc.invoice_payments(conn, invoice_id),
        methods=PAYMENT_METHODS, method_labels=METHOD_LABELS, today=_today(),
        values=values or defaults, errors=errors or {},
    )


@router.get("/{invoice_id}")
def detail(invoice_id: int, request: Request, user=Depends(current_user),
           conn: sqlite3.Connection = Depends(db.get_db)):
    return _detail(request, user, conn, invoice_id)


@router.post("/{invoice_id}/send")
def send(invoice_id: int, request: Request, user=Depends(current_user),
         conn: sqlite3.Connection = Depends(db.get_db)):
    _invoice_or_404(conn, user, invoice_id)
    error = svc.mark_sent(conn, user["id"], invoice_id, _today())
    flash(request, error or "Invoice marked as sent.", "bad" if error else "ok")
    return redirect(f"/invoices/{invoice_id}")


@router.post("/{invoice_id}/cancel")
def cancel(invoice_id: int, request: Request, user=Depends(current_user),
           conn: sqlite3.Connection = Depends(db.get_db)):
    _invoice_or_404(conn, user, invoice_id)
    error = svc.cancel_invoice(conn, user["id"], invoice_id)
    flash(request, error or "Invoice cancelled. Its hours are unbilled again.",
          "bad" if error else "ok")
    return redirect(f"/invoices/{invoice_id}")


@router.post("/{invoice_id}/payments")
async def add_payment(invoice_id: int, request: Request, user=Depends(current_user),
                      conn: sqlite3.Connection = Depends(db.get_db)):
    _invoice_or_404(conn, user, invoice_id)
    form = await request.form()
    data, errors = validate_payment(form)
    if not errors:
        svc.refresh_overdue(conn, user["id"], _today())
        error = record_payment(conn, user["id"], invoice_id, data, _today())
        if error is None:
            invoice = svc.get_invoice(conn, user["id"], invoice_id)
            flash(request, "Payment recorded. Invoice is now fully paid."
                  if invoice["status"] == "paid" else "Partial payment recorded.")
            return redirect(f"/invoices/{invoice_id}")
        errors["form"] = error
    return _detail(request, user, conn, invoice_id, values=dict(form), errors=errors,
                   status_code=400)


@router.get("/{invoice_id}/pdf")
def pdf(invoice_id: int, user=Depends(current_user),
        conn: sqlite3.Connection = Depends(db.get_db)):
    svc.refresh_overdue(conn, user["id"], _today())
    invoice = _invoice_or_404(conn, user, invoice_id)
    content = build_invoice_pdf(invoice, user, svc.billed_logs(conn, invoice_id),
                                svc.invoice_payments(conn, invoice_id))
    # The CHECK constraint guarantees the number is FD-YYYY-NNNN, so it is
    # safe to use as a file name.
    filename = f"{invoice['invoice_number']}.pdf"
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
