"""Recording payments, and the automatic invoice status update.

An invoice can be paid in several parts. There is no 'partially_paid'
status: a partly paid invoice stays 'sent' (or 'overdue'), and the UI
shows "partially paid" whenever 0 < paid < amount.
"""

import sqlite3

from app.db import transaction
from app.services.invoices import get_invoice

PAYMENT_METHODS = ("upi", "bank", "cash", "other")
METHOD_LABELS = {"upi": "UPI", "bank": "Bank transfer", "cash": "Cash", "other": "Other"}


def record_payment(conn: sqlite3.Connection, user_id: int, invoice_id: int,
                   data: dict, today: str) -> str | None:
    """Insert a payment and update the invoice status, as ONE transaction.

    data: amount_paise, paid_on, method, reference.
    Returns an error message, or None on success.

    The balance is read and the payment written inside the same
    BEGIN IMMEDIATE transaction, so two payments submitted at the same time
    cannot together pay more than the amount due.
    """
    with transaction(conn):
        invoice = get_invoice(conn, user_id, invoice_id)  # includes paid_paise and balance_paise
        if invoice is None:
            return "Invoice not found."
        if invoice["status"] == "draft":
            return "Mark the invoice as sent before recording a payment."
        if invoice["status"] in ("paid", "cancelled"):
            return f"This invoice is {invoice['status']}; no more payments can be recorded."
        if data["amount_paise"] > invoice["balance_paise"]:
            return "Payment is more than the balance due."
        if data["paid_on"] > today:
            return "Payment date cannot be in the future."
        if data["paid_on"] < invoice["issue_date"]:
            return "Payment date cannot be before the invoice date."

        conn.execute(
            """
            INSERT INTO payments (invoice_id, amount_paise, paid_on, method, reference)
            VALUES (?, ?, ?, ?, ?)
            """,
            (invoice_id, data["amount_paise"], data["paid_on"], data["method"], data["reference"]),
        )

        # Status update: the invoice becomes 'paid' once the payments add up to
        # at least the amount. The subquery re-adds the payments, including the
        # one just inserted, so the database itself decides.
        conn.execute(
            """
            UPDATE invoices
            SET status = 'paid'
            WHERE id = ?
              AND (SELECT SUM(amount_paise) FROM payments WHERE invoice_id = ?) >= amount_paise
            """,
            (invoice_id, invoice_id),
        )
    return None
