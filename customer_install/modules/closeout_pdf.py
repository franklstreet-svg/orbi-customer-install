"""
modules/closeout_pdf — project closeout package PDF for a completed job.

When the GC marks a project complete (or runs "generate closeout for X"),
this assembles a single PDF the customer receives at job's end:

  · Cover: contractor + customer + project address + completion date
  · Project summary: contract value + signed CO total = final authorized
  · Signed change orders table (#, description, amount, signed date)
  · Invoices table (#, billed, paid, balance)
  · Daily-log highlights (recent 5)
  · Warranty statement (from business_info.policies.warranty)
  · Thank-you closeout note

Pattern follows modules/invoice_pdf — same ReportLab layout primitives.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                  Table, TableStyle, PageBreak)


def generate(project: dict, cos: list, invoices: list, logs: list,
              business: dict, out_dir: Path) -> Path:
    """Render the closeout PDF. Returns the Path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"closeout_{project['id']}.pdf"

    doc = SimpleDocTemplate(
        str(out), pagesize=letter,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Project Closeout — {project.get('address','')}",
    )

    styles = getSampleStyleSheet()
    cover_title = ParagraphStyle("CoverTitle", parent=styles["Normal"],
                                   fontSize=22, leading=28, spaceAfter=6,
                                   textColor=colors.HexColor("#1a2240"))
    cover_sub = ParagraphStyle("CoverSub", parent=styles["Normal"],
                                 fontSize=14, leading=18, spaceAfter=24,
                                 textColor=colors.HexColor("#555"))
    h2 = ParagraphStyle("H2", parent=styles["Normal"],
                         fontSize=14, leading=18, spaceBefore=14, spaceAfter=8,
                         textColor=colors.HexColor("#1a2240"),
                         fontName="Helvetica-Bold")
    body = ParagraphStyle("Body", parent=styles["Normal"],
                           fontSize=10, leading=14)
    small = ParagraphStyle("Small", parent=styles["Normal"],
                            fontSize=9, leading=12,
                            textColor=colors.HexColor("#666"))

    biz_name = business.get("name") or "Contractor"
    biz_addr = business.get("address") or {}
    if isinstance(biz_addr, dict):
        ap = ", ".join(filter(None, [
            (biz_addr.get("street") or "").strip(),
            (biz_addr.get("city") or "").strip(),
            f"{biz_addr.get('state') or ''} {biz_addr.get('zip') or ''}".strip(),
        ]))
    else:
        ap = str(biz_addr)
    biz_phone = (business.get("contact") or {}).get("phone") or ""
    biz_email = (business.get("contact") or {}).get("email") or ""
    license_no = business.get("license") or ""

    customer = project.get("customer_name") or "Client"
    project_addr = project.get("address") or ""
    project_label = project.get("label") or ""
    completed = project.get("actual_complete") or ""
    completed_str = _fmt_iso_date(completed) or "today"

    # ── Cover block ────────────────────────────────────────────────────
    elements = [
        Paragraph("PROJECT CLOSEOUT", cover_title),
        Paragraph(f"{project_addr}"
                   + (f" — {project_label}" if project_label else ""), cover_sub),
        Paragraph(f"<b>Customer:</b> {customer}", body),
        Paragraph(f"<b>Completed:</b> {completed_str}", body),
        Paragraph(f"<b>Contractor:</b> {biz_name}"
                   + (f", License {license_no}" if license_no else ""), body),
        Spacer(1, 0.1 * inch),
        Paragraph(f"{ap}", small),
        Paragraph(
            f"{biz_phone}" + ("  ·  " + biz_email if biz_phone and biz_email else biz_email),
            small),
    ]

    # ── Final money summary ───────────────────────────────────────────
    contract = float(project.get("contract_amount") or 0)
    signed_co_total = sum(float(c.get("amount", 0)) for c in cos
                            if c.get("status") == "signed")
    authorized = contract + signed_co_total
    billed = sum(float(i.get("amount_due", 0)) for i in invoices
                  if i.get("status") not in ("draft", "void"))
    paid = sum(float(i.get("amount_paid", 0)) for i in invoices)
    balance = billed - paid
    retainage = sum(float(i.get("retainage_held", 0)) for i in invoices
                     if i.get("status") in ("paid", "partial"))

    elements.append(Paragraph("Final Account Summary", h2))
    money_rows = [
        ["Original Contract", f"${contract:,.2f}"],
    ]
    if signed_co_total:
        money_rows.append([f"Signed Change Orders ({sum(1 for c in cos if c.get('status') == 'signed')})",
                            f"+${signed_co_total:,.2f}"])
        money_rows.append(["Authorized Total", f"${authorized:,.2f}"])
    money_rows.append(["Invoiced", f"${billed:,.2f}"])
    money_rows.append(["Paid", f"${paid:,.2f}"])
    if retainage:
        money_rows.append(["Retainage Released at Closeout", f"${retainage:,.2f}"])
    if balance > 0:
        money_rows.append(["Balance Remaining", f"${balance:,.2f}"])
    else:
        money_rows.append(["Account", "Paid in Full ✓"])
    money_table = Table(money_rows, colWidths=[3.5 * inch, 2.5 * inch])
    money_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1),
         [colors.white, colors.HexColor("#f4f6fb")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#1a2240")),
    ]))
    elements.append(money_table)

    # ── Signed change orders ──────────────────────────────────────────
    signed_cos = [c for c in cos if c.get("status") == "signed"]
    if signed_cos:
        elements.append(Paragraph("Signed Change Orders", h2))
        co_rows = [["#", "Description", "Amount", "Signed"]]
        for c in signed_cos:
            ts = c.get("client_signed_at")
            signed_date = ""
            if ts:
                try:
                    signed_date = datetime.fromtimestamp(int(ts)).strftime("%b %-d, %Y")
                except (ValueError, OSError):
                    pass
            amt = float(c.get("amount", 0))
            sign = "+" if amt >= 0 else "-"
            co_rows.append([
                f"#{c.get('co_number','?')}",
                Paragraph(c.get("description", ""), body),
                f"{sign}${abs(amt):,.2f}",
                signed_date,
            ])
        co_table = Table(co_rows,
                          colWidths=[0.5 * inch, 3.5 * inch, 1.1 * inch, 1.1 * inch])
        co_table.setStyle(_table_style_header_row())
        elements.append(co_table)

    # ── Invoices table ────────────────────────────────────────────────
    if invoices:
        elements.append(Paragraph("Invoices Issued", h2))
        inv_rows = [["Number", "Billed", "Paid", "Balance"]]
        for i in invoices:
            owed = float(i.get("amount_due", 0)) - float(i.get("amount_paid", 0))
            inv_rows.append([
                i.get("invoice_number", ""),
                f"${float(i.get('amount_due', 0)):,.2f}",
                f"${float(i.get('amount_paid', 0)):,.2f}",
                f"${owed:,.2f}" if owed > 0 else "Paid",
            ])
        inv_table = Table(inv_rows,
                           colWidths=[2.0 * inch, 1.4 * inch, 1.4 * inch, 1.4 * inch])
        inv_table.setStyle(_table_style_header_row())
        elements.append(inv_table)

    # ── Recent activity highlights ────────────────────────────────────
    if logs:
        elements.append(Paragraph("Project Activity Highlights", h2))
        for l in logs[:5]:
            date_str = l.get("date", "")
            wd = l.get("work_done", "")[:240]
            elements.append(Paragraph(
                f"<b>{date_str}</b>: {wd}", body))
            elements.append(Spacer(1, 4))

    # ── Warranty ──────────────────────────────────────────────────────
    warranty_text = (business.get("policies") or {}).get("warranty", "") if isinstance(business.get("policies"), dict) else ""
    if warranty_text:
        elements.append(Paragraph("Warranty", h2))
        elements.append(Paragraph(warranty_text, body))

    # ── Thank-you closing ─────────────────────────────────────────────
    elements.append(Spacer(1, 0.25 * inch))
    elements.append(Paragraph(
        f"Thank you for trusting {biz_name} with your project. "
        f"If anything comes up post-closeout that you'd like us to take "
        f"a look at, please don't hesitate to reach out.",
        body))
    if biz_phone or biz_email:
        contact_bits = []
        if biz_phone: contact_bits.append(biz_phone)
        if biz_email: contact_bits.append(biz_email)
        elements.append(Spacer(1, 4))
        elements.append(Paragraph(" · ".join(contact_bits), small))

    doc.build(elements)
    return out


def _table_style_header_row() -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2240")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 10),
        ("ALIGN",      (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN",      (0, 0), (0, -1), "LEFT"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f4f6fb")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ])


def _fmt_iso_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%B %-d, %Y")
    except (ValueError, OSError):
        return ""
