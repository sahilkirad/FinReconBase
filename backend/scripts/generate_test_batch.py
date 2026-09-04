"""
Test Batch PDF Generator

Generates a multi-page PDF with 50 realistic Indian GST invoices.
Each page is one invoice with:
- Invoice header with number and date
- Supplier and buyer details
- Line items with quantities and prices
- GST breakdown (CGST + SGST)
- TDS deduction
- Grand total and amount due
- Bank details

Usage:
    python scripts/generate_test_batch.py [output_path] [num_invoices]
"""

import random
import sys
from datetime import date, timedelta
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


# --- Test Data ---

SUPPLIERS = [
    {"name": "Nexus Logistics Pvt Ltd", "gstin": "27AABCU9603R1ZM", "state": "Maharashtra"},
    {"name": "Transport Solutions India", "gstin": "09AADCB2230M1ZT", "state": "Uttar Pradesh"},
    {"name": "Freight Express Co", "gstin": "29AABCF1234N1ZK", "state": "Karnataka"},
    {"name": "Cargo Movers Ltd", "gstin": "06AABCC5678P1ZQ", "state": "Haryana"},
    {"name": "Swift Logistics", "gstin": "33AABCS9012R1ZL", "state": "Tamil Nadu"},
]

BUYERS = [
    {"name": "Acme Corp India", "gstin": "27AABCA1234B1ZV"},
    {"name": "Global Enterprises", "gstin": "09AABCG5678C1ZW"},
    {"name": "Tech Solutions Ltd", "gstin": "29AABCT9012D1ZX"},
]

DESCRIPTIONS = [
    "Freight Services - Regional",
    "Warehousing Charges",
    "Last Mile Delivery",
    "Cold Chain Transport",
    "Express Parcel Service",
    "Container Shipping",
    "Partial Truckload",
    "Full Truckload Transport",
    "Customs Clearance",
    "Packaging Services",
]

ITEM_UNITS = ["NOS", "KGS", "BOX", "PCS", "LOT"]


def generate_invoice_data(invoice_num: int) -> dict:
    """Generate realistic invoice data for a single invoice."""
    supplier = random.choice(SUPPLIERS)
    buyer = random.choice(BUYERS)
    
    # Generate line items (1-3 items)
    num_items = random.randint(1, 3)
    line_items = []
    subtotal = 0
    
    for i in range(num_items):
        qty = random.randint(1, 100)
        unit_price = random.randint(1000, 50000)  # In rupees
        taxable = qty * unit_price
        gst_rate = random.choice([5, 12, 18, 28])
        gst_amount = int(taxable * gst_rate / 100)
        
        line_items.append({
            "description": random.choice(DESCRIPTIONS),
            "hsn": f"{random.randint(1000, 9999)}",
            "qty": qty,
            "unit": random.choice(ITEM_UNITS),
            "unit_price": unit_price,
            "taxable": taxable,
            "gst_rate": gst_rate,
            "gst_amount": gst_amount,
            "total": taxable + gst_amount,
        })
        subtotal += taxable
    
    # Calculate totals
    total_gst = sum(item["gst_amount"] for item in line_items)
    grand_total = subtotal + total_gst
    tds_rate = random.choice([2, 5, 10])  # TDS percentage
    tds_amount = int(subtotal * tds_rate / 100)
    amount_due = grand_total - tds_amount
    
    # Generate dates
    invoice_date = date(2026, 8, 1) + timedelta(days=random.randint(0, 30))
    due_date = invoice_date + timedelta(days=30)
    
    return {
        "invoice_number": f"INV-2026-{invoice_num:04d}",
        "invoice_date": invoice_date.strftime("%d-%m-%Y"),
        "due_date": due_date.strftime("%d-%m-%Y"),
        "supplier": supplier,
        "buyer": buyer,
        "line_items": line_items,
        "subtotal": subtotal,
        "total_gst": total_gst,
        "grand_total": grand_total,
        "tds_rate": tds_rate,
        "tds_amount": tds_amount,
        "amount_due": amount_due,
    }


def format_currency(amount: int) -> str:
    """Format amount in Indian currency style."""
    s = str(amount)
    if len(s) <= 3:
        return s
    result = s[-3:]
    s = s[:-3]
    while s:
        result = s[-2:] + "," + result
        s = s[:-2]
    return result


def generate_invoice_page(invoice_data: dict) -> str:
    """Generate LaTeX content for a single invoice page."""
    inv = invoice_data
    supplier = inv["supplier"]
    buyer = inv["buyer"]
    
    # Line items table
    items_latex = ""
    for i, item in enumerate(inv["line_items"], 1):
        items_latex += f"""
        {i} & {item['description']} & {item['hsn']} & {item['qty']} {item['unit']} & 
        Rs.{format_currency(item['unit_price'])} & Rs.{format_currency(item['taxable'])} & 
        {item['gst_rate']}\\% & Rs.{format_currency(item['gst_amount'])} & 
        Rs.{format_currency(item['total'])} \\\\"""
    
    return f"""
    \\begin{{minipage}}{{\\textwidth}}
    \\vspace{{5mm}}
    
    \\textbf{{TAX INVOICE}} \\hfill \\textbf{{Invoice No: {inv['invoice_number']}}} \\\\
    \\textbf{{Date: {inv['invoice_date']}}} \\hfill \\textbf{{Due Date: {inv['due_date']}}} \\\\
    
    \\vspace{{3mm}}
    
    \\textbf{{From:}} \\\\
    {supplier['name']} \\\\
    GSTIN: {supplier['gstin']} \\\\
    State: {supplier['state']} \\\\
    
    \\vspace{{2mm}}
    
    \\textbf{{To:}} \\\\
    {buyer['name']} \\\\
    GSTIN: {buyer['gstin']} \\\\
    
    \\vspace{{3mm}}
    
    \\begin{{tabular}}{{|c|l|c|c|c|c|c|c|c|}}
    \\hline
    \\textbf{{S.No}} & \\textbf{{Description}} & \\textbf{{HSN}} & \\textbf{{Qty}} & 
    \\textbf{{Unit Price}} & \\textbf{{Taxable}} & \\textbf{{GST\\%}} & \\textbf{{GST Amt}} & \\textbf{{Total}} \\\\
    \\hline
    {items_latex}
    \\hline
    \\end{{tabular}}
    
    \\vspace{{3mm}}
    
    \\begin{{minipage}}{{0.5\\textwidth}}
    \\textbf{{Summary:}} \\\\
    Subtotal: Rs.{format_currency(inv['subtotal'])} \\\\
    Total GST: Rs.{format_currency(inv['total_gst'])} \\\\
    \\textbf{{Grand Total: Rs.{format_currency(inv['grand_total'])}}} \\\\
    TDS ({inv['tds_rate']}\\%): Rs.{format_currency(inv['tds_amount'])} \\\\
    \\textbf{{Amount Due: Rs.{format_currency(inv['amount_due'])}}}
    \\end{{minipage}}
    \\hfill
    \\begin{{minipage}}{{0.4\\textwidth}}
    \\textbf{{Bank Details:}} \\\\
    Bank: HDFC Bank \\\\
    A/C No: 50100012345678 \\\\
    IFSC: HDFC0001234 \\\\
    Branch: Mumbai Main \\\\
    
    \\vspace{{5mm}}
    \\textbf{{Authorized Signatory}}
    \\end{{minipage}}
    
    \\vspace{{5mm}}
    
    \\textbf{{This is a computer generated invoice}}
    
    \\end{{minipage}}
    \\newpage
    """


def generate_test_pdf(
    output_path: str = "test_batch_50.pdf",
    num_invoices: int = 50,
    seed: int | None = None,
):
    """Generate a multi-page PDF with test invoices."""
    if seed is not None:
        random.seed(seed)
    # Try to use reportlab (preferred)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        
        doc = SimpleDocTemplate(output_path, pagesize=A4)
        styles = getSampleStyleSheet()
        story = []
        
        for i in range(1, num_invoices + 1):
            inv = generate_invoice_data(i)
            
            # Invoice header
            story.append(Paragraph(f"<b>TAX INVOICE</b>", styles["Title"]))
            story.append(Paragraph(f"<b>Invoice No: {inv['invoice_number']}</b>", styles["Normal"]))
            story.append(Paragraph(f"<b>Date: {inv['invoice_date']}</b> | <b>Due: {inv['due_date']}</b>", styles["Normal"]))
            story.append(Spacer(1, 10))
            
            # Supplier info
            story.append(Paragraph(f"<b>From:</b> {inv['supplier']['name']}", styles["Normal"]))
            story.append(Paragraph(f"GSTIN: {inv['supplier']['gstin']}", styles["Normal"]))
            story.append(Spacer(1, 5))
            
            # Buyer info
            story.append(Paragraph(f"<b>To:</b> {inv['buyer']['name']}", styles["Normal"]))
            story.append(Paragraph(f"GSTIN: {inv['buyer']['gstin']}", styles["Normal"]))
            story.append(Spacer(1, 10))
            
            # Line items table
            table_data = [["S.No", "Description", "HSN", "Qty", "Unit Price", "Taxable", "GST%", "GST Amt", "Total"]]
            for j, item in enumerate(inv["line_items"], 1):
                table_data.append([
                    str(j),
                    item["description"],
                    item["hsn"],
                    f"{item['qty']} {item['unit']}",
                    f"Rs.{format_currency(item['unit_price'])}",
                    f"Rs.{format_currency(item['taxable'])}",
                    f"{item['gst_rate']}%",
                    f"Rs.{format_currency(item['gst_amount'])}",
                    f"Rs.{format_currency(item['total'])}",
                ])
            
            table = Table(table_data, colWidths=[30, 80, 40, 45, 60, 60, 35, 55, 60])
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ]))
            story.append(table)
            story.append(Spacer(1, 15))
            
            # Summary
            story.append(Paragraph(f"<b>Subtotal:</b> Rs.{format_currency(inv['subtotal'])}", styles["Normal"]))
            story.append(Paragraph(f"<b>Total GST:</b> Rs.{format_currency(inv['total_gst'])}", styles["Normal"]))
            story.append(Paragraph(f"<b>Grand Total:</b> Rs.{format_currency(inv['grand_total'])}", styles["Normal"]))
            story.append(Paragraph(f"<b>TDS ({inv['tds_rate']}%):</b> Rs.{format_currency(inv['tds_amount'])}", styles["Normal"]))
            story.append(Paragraph(f"<b>Amount Due:</b> Rs.{format_currency(inv['amount_due'])}", styles["Normal"]))
            story.append(Spacer(1, 10))
            
            # Bank details
            story.append(Paragraph("<b>Bank Details:</b>", styles["Normal"]))
            story.append(Paragraph("Bank: HDFC Bank | A/C: 50100012345678 | IFSC: HDFC0001234", styles["Normal"]))
            story.append(Spacer(1, 15))
            
            story.append(Paragraph("<b>Authorized Signatory</b>", styles["Normal"]))
            story.append(Paragraph("<i>This is a computer generated invoice</i>", styles["Normal"]))
            
            # Page break (except for last invoice)
            if i < num_invoices:
                from reportlab.platypus import PageBreak
                story.append(PageBreak())
        
        doc.build(story)
        print(f"Generated {output_path} with {num_invoices} invoices")
        return output_path
        
    except ImportError:
        print("reportlab not installed. Install with: pip install reportlab")
        print("Falling back to simple text file...")
        
        # Fallback: generate text file
        with open(output_path.replace(".pdf", ".txt"), "w") as f:
            for i in range(1, num_invoices + 1):
                inv = generate_invoice_data(i)
                f.write(f"=== Invoice {inv['invoice_number']} ===\n")
                f.write(f"Date: {inv['invoice_date']}\n")
                f.write(f"From: {inv['supplier']['name']} ({inv['supplier']['gstin']})\n")
                f.write(f"To: {inv['buyer']['name']} ({inv['buyer']['gstin']})\n")
                for item in inv["line_items"]:
                    f.write(f"  - {item['description']}: {item['qty']} x Rs.{item['unit_price']} = Rs.{item['taxable']}\n")
                f.write(f"Subtotal: Rs.{inv['subtotal']}\n")
                f.write(f"Grand Total: Rs.{inv['grand_total']}\n")
                f.write(f"TDS: Rs.{inv['tds_amount']}\n")
                f.write(f"Amount Due: Rs.{inv['amount_due']}\n")
                f.write("\n")
        
        print(f"Generated {output_path.replace('.pdf', '.txt')} with {num_invoices} invoices")
        return output_path.replace(".pdf", ".txt")


if __name__ == "__main__":
    output = sys.argv[1] if len(sys.argv) > 1 else "test_batch_50.pdf"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else None
    generate_test_pdf(output, count, seed)
