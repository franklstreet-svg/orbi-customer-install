"""
modules/invoice_pdf — generate a clean PDF invoice for the contractor module.

ReportLab is already in the install (used by chart_gen / pptx_gen / etc.).
This module turns one invoice + its project + the business profile into a
single-page PDF suitable for emailing to the customer.

Layout (top to bottom):
  · Contractor header (name, license#, address, phone, email) - left
  · "INVOICE" badge + invoice # + dates - right
  · Bill-to block (customer name + project address)
  · Line items table (label, qty, rate, amount)
  · Subtotal / retainage / amount due
  · Memo / payment terms

Output goes to data/invoice_pdfs/<invoice_id>.pdf. Same-id regeneration
overwrites in place.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                  Table, TableStyle, KeepTogether)


def generate(invoice: dict, project: dict, business: dict,
              out_dir: Path) -> Path:
    """Render the PDF for `invoice` (a dict from modules.invoices.get()).
    `project` from modules.projects.get(invoice['project_id']).
    `business` from modules.business_info.load(DATA_DIR) — for header.
    Returns the Path of the written PDF."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{invoice['id']}.pdf"

    doc = SimpleDocTemplate(
        str(out), pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.5 * inch, bottomMargin=0.6 * inch,
        title=f"Invoice {invoice.get('invoice_number','')}",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title", parent=styles["Normal"],
                                   fontSize=24, leading=28,
                                   textColor=colors.HexColor("#1a2240"),
                                   alignment=2)  # right
    biz_name_style = ParagraphStyle("BizName", parent=styles["Normal"],
                                      fontSize=14, leading=18,
                                      textColor=colors.HexColor("#1a2240"))
    small = ParagraphStyle("Small", parent=styles["Normal"],
                            fontSize=9, leading=12,
                            textColor=colors.HexColor("#444"))
    small_right = ParagraphStyle("SmallRight", parent=small, alignment=2)
    label = ParagraphStyle("Label", parent=styles["Normal"],
                            fontSize=8, leading=10,
                            textColor=colors.HexColor("#777"))
    label_right = ParagraphStyle("LabelRight", parent=label, alignment=2)
    body = ParagraphStyle("Body", parent=styles["Normal"],
                           fontSize=10, leading=13)

    biz_name = business.get("name") or "Contractor"
    biz_phone = (business.get("contact") or {}).get("phone") or ""
    biz_email = (business.get("contact") or {}).get("email") or ""
    biz_addr = business.get("address") or {}
    if isinstance(biz_addr, dict):
        addr_lines = []
        s = (biz_addr.get("street") or "").strip()
        c = (biz_addr.get("city") or "").strip()
        st = (biz_addr.get("state") or "").strip()
        zp = (biz_addr.get("zip") or "").strip()
        if s: addr_lines.append(s)
        if c or st or zp:
            addr_lines.append(", ".join(p for p in (c, f"{st} {zp}".strip()) if p))
        addr_str = "<br/>".join(addr_lines)
    else:
        addr_str = str(biz_addr)
    license_no = business.get("license") or business.get("license_no") or ""

    # ── Header: biz info left, INVOICE badge right ──────────────────────
    biz_block_lines = [f"<b>{biz_name}</b>"]
    if license_no:
        biz_block_lines.append(f"License #{license_no}")
    if addr_str:
        biz_block_lines.append(addr_str)
    contact_bits = []
    if biz_phone: contact_bits.append(biz_phone)
    if biz_email: contact_bits.append(biz_email)
    if contact_bits:
        biz_block_lines.append(" · ".join(contact_bits))
    biz_block = Paragraph("<br/>".join(biz_block_lines), small)

    inv_meta_lines = [
        f"<b>{invoice.get('invoice_number','')}</b>",
        f"Issued: {_fmt_date(invoice.get('issued_at'))}",
    ]
    if invoice.get("due_at"):
        inv_meta_lines.append(f"Due: {_fmt_date(invoice.get('due_at'))}")
    inv_meta = Paragraph("<br/>".join(inv_meta_lines), small_right)

    header_table = Table(
        [[Paragraph(biz_name, biz_name_style), Paragraph("INVOICE", title_style)],
         [biz_block, inv_meta]],
        colWidths=[3.6 * inch, 3.6 * inch],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (0, 0), 4),
    ]))

    # ── Bill-to + project ──────────────────────────────────────────────
    cust_name = project.get("customer_name") or ""
    cust_addr = project.get("address") or ""
    bill_to_block = Paragraph(
        f"<b>BILL TO</b><br/>{cust_name}<br/>{cust_addr}", small)
    project_label = project.get("label") or ""
    project_block = Paragraph(
        f"<b>PROJECT</b><br/>{cust_addr}"
        + (f"<br/>{project_label}" if project_label else ""),
        small_right)
    bt_table = Table(
        [[bill_to_block, project_block]],
        colWidths=[3.6 * inch, 3.6 * inch],
    )
    bt_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
    ]))

    # ── Line items table ───────────────────────────────────────────────
    items = invoice.get("line_items") or []
    data = [["Description", "Qty", "Rate", "Amount"]]
    for it in items:
        qty = it.get("qty", 1)
        rate = float(it.get("rate") or it.get("amount") or 0)
        amt = float(it.get("amount") or 0)
        data.append([
            Paragraph(str(it.get("label", "")), body),
            str(qty),
            f"${rate:,.2f}",
            f"${amt:,.2f}",
        ])
    items_table = Table(data, colWidths=[3.8 * inch, 0.6 * inch, 1.2 * inch, 1.4 * inch])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2240")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 10),
        ("ALIGN",      (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN",      (0, 0), (0, -1), "LEFT"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f4f6fb")]),
        ("LINEBELOW",  (0, -1), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))

    # ── Totals ─────────────────────────────────────────────────────────
    subtotal = float(invoice.get("subtotal", 0))
    retainage_pct = float(invoice.get("retainage_pct", 0))
    retainage_held = float(invoice.get("retainage_held", 0))
    amount_due = float(invoice.get("amount_due", 0))
    amount_paid = float(invoice.get("amount_paid", 0))
    balance = amount_due - amount_paid

    totals_rows = [["Subtotal", f"${subtotal:,.2f}"]]
    if retainage_pct > 0:
        totals_rows.append([f"Retainage held ({retainage_pct:.0f}%)",
                             f"-${retainage_held:,.2f}"])
    totals_rows.append(["Amount due", f"${amount_due:,.2f}"])
    if amount_paid > 0:
        totals_rows.append(["Paid",        f"-${amount_paid:,.2f}"])
        totals_rows.append(["Balance",     f"${balance:,.2f}"])
    totals_table = Table(totals_rows, colWidths=[1.8 * inch, 1.4 * inch])
    totals_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#1a2240")),
    ]))

    totals_wrapper = Table(
        [[Paragraph("", small), totals_table]],
        colWidths=[3.8 * inch, 3.4 * inch],
    )
    totals_wrapper.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
    ]))

    # ── Memo + payment terms ───────────────────────────────────────────
    memo = invoice.get("memo") or ""
    footer_lines = []
    if memo:
        footer_lines.append(f"<b>Note:</b> {memo}")
    footer_lines.append(
        f"<b>Payment:</b> Make checks payable to {biz_name}."
        + (f" Questions? {biz_email}" if biz_email else "")
    )
    footer = Paragraph("<br/><br/>".join(footer_lines), small)

    elements = [
        header_table,
        Spacer(1, 0.15 * inch),
        bt_table,
        Spacer(1, 0.2 * inch),
        items_table,
        totals_wrapper,
        Spacer(1, 0.4 * inch),
        footer,
    ]
    doc.build(elements)
    return out


def _fmt_date(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%b %-d, %Y")
    except (ValueError, OSError):
        return ""
