"""Generate a realistic text-based bank statement PDF for testing the parser."""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent / "sample_statement.pdf"

# date, description, amount, running balance — mimics a CommBank-style layout
ROWS = [
    ("01/01/2026", "SALARY ACME PTY LTD", "3,200.00", "3,450.00"),
    ("02/01/2026", "WOOLWORTHS 1234 SYDNEY", "84.20", "3,365.80"),
    ("03/01/2026", "UBER EATS", "32.50", "3,333.30"),
    ("05/01/2026", "NETFLIX.COM", "22.99", "3,310.31"),
    ("06/01/2026", "OPAL TRAVEL", "45.00", "3,265.31"),
    ("10/01/2026", "RENT LANDLORD JOHN", "950.00", "2,315.31"),
    ("12/01/2026", "COLES 5678", "63.10", "2,252.21"),
    ("15/01/2026", "REFUND AMAZON", "29.99", "2,282.20"),
]


def main():
    c = canvas.Canvas(str(OUT), pagesize=A4)
    width, height = A4
    y = height - 80

    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, y, "Everyday Account Statement")
    y -= 24
    c.setFont("Helvetica", 9)
    c.drawString(60, y, "Account 06 2000 1234 5678    Period 01 Jan 2026 - 31 Jan 2026")
    y -= 30

    c.setFont("Helvetica-Bold", 9)
    c.drawString(60, y, "Date")
    c.drawString(140, y, "Description")
    c.drawString(380, y, "Amount")
    c.drawString(470, y, "Balance")
    y -= 16

    c.drawString(60, y, "Opening balance")
    c.drawRightString(540, y, "250.00")
    y -= 16

    c.setFont("Helvetica", 9)
    for date, desc, amount, balance in ROWS:
        c.drawString(60, y, date)
        c.drawString(140, y, desc)
        c.drawRightString(440, y, amount)
        c.drawRightString(540, y, balance)
        y -= 16

    c.save()
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
