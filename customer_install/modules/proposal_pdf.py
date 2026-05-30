"""
modules/proposal_pdf — polished PDF proposal generator for outgoing bids.

Most contractors send bids as a Word doc or a scribbled estimate. This
turns a bid record + business profile into a real proposal PDF that
demos well and signals competence.

Layout:
  · Cover-style header: PROPOSAL, prospect name + project address, date
  · Contractor block: name, license, phone, email
  · Project overview: scope summary, project type
  · Investment: bid amount, with payment-terms language from business
  · "What you get" list pulled from services if scope_summary is empty
  · Validity + signature/accept line
  · Footer with license number

Same ReportLab pattern as invoice_pdf and closeout_pdf.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                  Table, TableStyle)


def generate(bid: dict, business: dict, out_dir: Path,
              validity_days: int = 30) -> Path:
    """Render the proposal PDF for `bid`. Returns the Path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"proposal_{bid['id']}.pdf"

    doc = SimpleDocTemplate(
        str(out), pagesize=letter,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Proposal — {bid.get('customer_name','')}",
    )

    styles = getSampleStyleSheet()
    cover_title = ParagraphStyle("CoverTitle", parent=styles["Normal"],
                                   fontSize=24, leading=30, spaceAfter=2,
                                   textColor=colors.HexColor("#1a2240"),
                                   fontName="Helvetica-Bold")
    cover_sub = ParagraphStyle("CoverSub", parent=styles["Normal"],
                                 fontSize=14, leading=18, spaceAfter=12,
                                 textColor=colors.HexColor("#555"))
    h2 = ParagraphStyle("H2", parent=styles["Normal"],
                         fontSize=13, leading=16, spaceBefore=12, spaceAfter=6,
                         textColor=colors.HexColor("#1a2240"),
                         fontName="Helvetica-Bold")
    body = ParagraphStyle("Body", parent=styles["Normal"],
                           fontSize=11, leading=15)
    big_amount = ParagraphStyle("BigAmount", parent=styles["Normal"],
                                  fontSize=28, leading=34, spaceBefore=8,
                                  textColor=colors.HexColor("#8b5cf6"),
                                  fontName="Helvetica-Bold",
                                  alignment=1)
    small = ParagraphStyle("Small", parent=styles["Normal"],
                            fontSize=9, leading=12,
                            textColor=colors.HexColor("#666"))

    biz_name = business.get("name") or "Contractor"
    biz_phone = (business.get("contact") or {}).get("phone") or ""
    biz_email = (business.get("contact") or {}).get("email") or ""
    license_no = business.get("license") or ""
    biz_addr = business.get("address") or {}
    if isinstance(biz_addr, dict):
        ap_parts = [
            (biz_addr.get("street") or "").strip(),
            (biz_addr.get("city") or "").strip(),
            f"{biz_addr.get('state') or ''} {biz_addr.get('zip') or ''}".strip(),
        ]
        ap = ", ".join(p for p in ap_parts if p)
    else:
        ap = str(biz_addr)

    customer = bid.get("customer_name") or "Customer"
    project_addr = bid.get("project_address") or ""
    project_type = bid.get("project_type") or "Your project"
    scope_summary = bid.get("scope_summary") or ""
    amount = float(bid.get("amount") or 0)
    today_str = datetime.now().strftime("%B %-d, %Y")
    valid_until = (datetime.now() + timedelta(days=validity_days)).strftime("%B %-d, %Y")

    elements = [
        Paragraph("PROPOSAL", cover_title),
        Paragraph(f"Prepared for {customer}"
                   + (f" — {project_addr}" if project_addr else ""),
                   cover_sub),
        Paragraph(f"<b>Date:</b> {today_str}", body),
        Paragraph(f"<b>Project:</b> {project_type}", body),
        Spacer(1, 0.05 * inch),
    ]

    # Contractor block
    elements.append(Paragraph("From", h2))
    biz_lines = [f"<b>{biz_name}</b>"]
    if license_no: biz_lines.append(f"License #{license_no}")
    if ap: biz_lines.append(ap)
    contact_bits = []
    if biz_phone: contact_bits.append(biz_phone)
    if biz_email: contact_bits.append(biz_email)
    if contact_bits: biz_lines.append(" · ".join(contact_bits))
    elements.append(Paragraph("<br/>".join(biz_lines), body))

    # Scope
    elements.append(Paragraph("Scope of work", h2))
    if scope_summary:
        elements.append(Paragraph(scope_summary, body))
    else:
        # Fallback: generic scope from services
        services = business.get("services") or []
        if services:
            elements.append(Paragraph(
                f"Work as discussed: {project_type}. Detailed scope to be "
                f"finalized at contract signing. {biz_name} provides these "
                f"services: " + ", ".join(services[:6]) + ".",
                body))
        else:
            elements.append(Paragraph(
                f"Work as discussed: {project_type}. Full scope to be "
                f"detailed in the signed contract.", body))

    # Investment block — make the price the focal point
    elements.append(Paragraph("Investment", h2))
    elements.append(Paragraph(f"${amount:,.2f}", big_amount))

    # Payment terms
    pay_terms = (business.get("policies") or {}).get("payment_methods", "") if isinstance(business.get("policies"), dict) else ""
    if pay_terms:
        elements.append(Spacer(1, 0.05 * inch))
        elements.append(Paragraph(f"<b>Payment terms:</b> {pay_terms}", body))

    # Warranty
    warranty = (business.get("policies") or {}).get("warranty", "") if isinstance(business.get("policies"), dict) else ""
    if warranty:
        elements.append(Paragraph("Warranty", h2))
        elements.append(Paragraph(warranty, body))

    # Validity + acceptance
    elements.append(Paragraph("Validity", h2))
    elements.append(Paragraph(
        f"This proposal is valid through <b>{valid_until}</b> "
        f"({validity_days} days from issue). Prices may need to be "
        f"adjusted thereafter due to material or labor cost changes.",
        body))

    elements.append(Spacer(1, 0.3 * inch))
    elements.append(Paragraph("Acceptance", h2))
    accept_table = Table(
        [["________________________________________", ""],
         [f"Customer signature ({customer})",
          "Date: ________________"]],
        colWidths=[3.8 * inch, 2.6 * inch],
    )
    accept_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#666")),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, 0), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
    ]))
    elements.append(accept_table)

    elements.append(Spacer(1, 0.3 * inch))
    elements.append(Paragraph(
        f"Thank you for considering {biz_name}. Looking forward to "
        f"working with you.", small))

    doc.build(elements)
    return out
