"""Build the invoice PDF with fpdf2.

fpdf2's built-in fonts (Helvetica) only cover Latin-1, so the rupee sign is
written as "Rs." and any other unsupported character becomes "?" instead of
crashing. Shipping a Unicode TTF font would avoid this, but it adds a binary
file to the project.
"""

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.services.payments import METHOD_LABELS
from app.templating import format_hours, format_inr

REPLACEMENTS = {"₹": "Rs. ", "—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "..."}


def latin1(text) -> str:
    text = "" if text is None else str(text)
    for char, replacement in REPLACEMENTS.items():
        text = text.replace(char, replacement)
    return text.encode("latin-1", "replace").decode("latin-1")


def money(paise: int) -> str:
    return latin1(format_inr(paise))  # "₹1,500.00" -> "Rs. 1,500.00"


def build_invoice_pdf(invoice, user, logs, payments) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def line(text, size=10, style="", align="L", h=5.5, w=0):
        pdf.set_font("Helvetica", style, size)
        pdf.cell(w, h, latin1(text), align=align, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- header ---
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(width / 2, 12, "INVOICE")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(width / 2, 12, latin1(invoice["invoice_number"]), align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # --- from / bill to ---
    top = pdf.get_y()
    line("FROM", 8, "B")
    line(user["name"], 11, "B")
    line(user["email"])
    left_bottom = pdf.get_y()

    pdf.set_xy(pdf.l_margin + width / 2, top)
    for text, size, style in [("BILL TO", 8, "B"), (invoice["client_name"], 11, "B"),
                              (invoice["client_email"] or "", 10, ""),
                              (invoice["client_phone"] or "", 10, "")]:
        pdf.set_font("Helvetica", style, size)
        pdf.cell(width / 2, 5.5, latin1(text), new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.set_y(max(left_bottom, pdf.get_y()) + 4)

    # --- dates ---
    status = invoice["status"].upper()
    for label, value in [("Issue date", invoice["issue_date"]), ("Due date", invoice["due_date"]),
                         ("Project", invoice["project_title"]), ("Status", status)]:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(30, 6, label)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, latin1(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # --- line item ---
    pdf.set_fill_color(235, 238, 242)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(width - 45, 8, "  Description", fill=True)
    pdf.cell(45, 8, "Amount  ", align="R", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    if invoice["billing_type"] == "hourly" and logs:
        minutes = sum(log["duration_minutes"] for log in logs)
        description = (f"{invoice['project_title']}: {format_hours(minutes)} "
                       f"at {format_inr(invoice['hourly_rate_paise'])}/hour")
    else:
        description = f"{invoice['project_title']}: professional services"
    pdf.cell(width - 45, 8, "  " + latin1(description), border="B")
    pdf.cell(45, 8, money(invoice["amount_paise"]) + "  ", align="R", border="B",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- totals ---
    pdf.ln(2)
    rows = [("Total", invoice["amount_paise"], "B")]
    if invoice["paid_paise"]:
        rows += [("Paid", -invoice["paid_paise"], ""), ("Balance due", invoice["balance_paise"], "B")]
    for label, value, style in rows:
        pdf.set_font("Helvetica", style, 11)
        pdf.cell(width - 45, 7, label, align="R")
        pdf.cell(45, 7, money(value) + "  ", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- payments received ---
    if payments:
        pdf.ln(4)
        line("Payments received", 11, "B", h=7)
        for p in payments:
            ref = f" (ref {p['reference']})" if p["reference"] else ""
            line(f"{p['paid_on']}   {METHOD_LABELS.get(p['method'], p['method'])}{ref}   "
                 f"{format_inr(p['amount_paise'])}", 9)

    # --- time log detail ---
    if logs:
        pdf.ln(4)
        line("Time log detail", 11, "B", h=7)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(28, 6, "Date")
        pdf.cell(width - 28 - 25, 6, "Description")
        pdf.cell(25, 6, "Hours", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        for log in logs:
            pdf.cell(28, 5.5, log["started_at"][:10])
            pdf.cell(width - 28 - 25, 5.5, latin1(log["description"] or "-"))
            pdf.cell(25, 5.5, format_hours(log["duration_minutes"]), align="R",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(8)
    line("Thank you for your business.", 9, "I")
    return bytes(pdf.output())
