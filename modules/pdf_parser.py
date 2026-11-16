"""Extract transactions from text-based bank statement PDFs.

Bank PDFs are built for printing, not data, so this is a best-effort,
line-by-line text parser. It works on *text* PDFs (the kind you can select
text in), not scanned/image statements. Each detected line yields a date,
description, amount and (when present) a running balance.

Sign of the amount is resolved in this priority order:
  1. explicit CR / DR marker on the line
  2. a leading minus sign
  3. change in running balance vs the previous row (most reliable)
  4. fallback: treated as an expense (negative)
"""

import re

import pandas as pd
import pdfplumber
from config import MAX_TRANSACTION_ROWS
from modules.bank_parser import StatementRejected

# A line is a transaction only if it *starts* with a date.
_MONTH = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Sept|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
_DATE_RE = re.compile(
    r"^\s*("
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"        # 01/01/2026, 1-1-26
    rf"|\d{{1,2}}\s+{_MONTH}\s+\d{{2,4}}"    # 01 Jan 2026, 1 January 2026
    rf"|{_MONTH}\s+\d{{1,2}},?\s+\d{{2,4}}"  # Jan 01, 2026
    r")\b", re.IGNORECASE,
)

# Money: optional $, optional leading -, thousands separators, 2 decimals,
# optional trailing CR/DR. The 2-decimal requirement avoids matching
# reference/card numbers.
_AMOUNT_RE = re.compile(
    r"(?P<neg>-)?\$?\s?(?P<num>\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\s?(?P<drcr>CR|DR)?",
    re.IGNORECASE,
)

# Opening balance line. seeds the first transaction's sign via balance movement.
_OPENING_RE = re.compile(r"opening balance|balance brought forward|balance b/?f", re.I)


def _to_float(num_str):
    return float(num_str.replace(",", ""))


def _balance_value(match):
    value = _to_float(match.group("num"))
    return -value if match.group("neg") == "-" or (match.group("drcr") or "").upper() == "DR" else value


def _parse_line(line):
    """Return a raw dict for one statement line, or None if it isn't one."""
    if _OPENING_RE.search(line):
        return None

    date_match = _DATE_RE.match(line)
    if not date_match:
        return None

    date_str = date_match.group(1)
    rest = line[date_match.end():].strip()

    amounts = list(_AMOUNT_RE.finditer(rest))
    if not amounts:
        return None

    first = amounts[0]
    description = rest[: first.start()].strip()
    if not description:
        return None

    balance = None
    if len(amounts) >= 2:
        last = amounts[-1]
        balance = _balance_value(last)

    return {
        "date": date_str,
        "description": description,
        "raw_amount": _to_float(first.group("num")),
        "neg": first.group("neg") == "-",
        "drcr": (first.group("drcr") or "").upper(),
        "balance": balance,
    }


def _find_opening_balance(lines):
    """Return the statement's opening balance if a line declares one."""
    for line in lines:
        if _OPENING_RE.search(line):
            amounts = list(_AMOUNT_RE.finditer(line))
            if amounts:
                last = amounts[-1]
                return _balance_value(last)
    return None


def _apply_signs(rows, opening_balance=None):
    """Resolve +/- for each amount using markers, then balance movement."""
    prev_balance = opening_balance
    out = []
    for r in rows:
        amount = abs(r["raw_amount"])

        if r["drcr"] == "CR":
            amount = +amount
        elif r["drcr"] == "DR":
            amount = -amount
        elif r["neg"]:
            amount = -amount
        elif r["balance"] is not None and prev_balance is not None:
            amount = amount if r["balance"] >= prev_balance else -amount
        else:
            amount = -amount  # ambiguous line → assume expense

        if r["balance"] is not None and prev_balance is not None:
            if abs((r["balance"] - prev_balance) - amount) > 0.011:
                raise StatementRejected(
                    "The PDF amounts do not reconcile with its running balances. "
                    "Its layout may be unsupported or transactions may be missing. Export a CSV instead.")

        out.append(
            {
                "date": r["date"],
                "description": r["description"],
                "amount": amount,
                "balance": r["balance"],
            }
        )
        if r["balance"] is not None:
            prev_balance = r["balance"]
    return out


def extract_transactions(path):
    """Parse a text-based PDF into a date/description/amount/balance DataFrame."""
    rows = []
    opening_balance = None
    text_size = 0
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > 200:
            raise StatementRejected("PDF statements must contain no more than 200 pages.")
        for page in pdf.pages:
            text = page.extract_text() or ""
            text_size += len(text)
            if text_size > 5_000_000:
                raise StatementRejected("This PDF contains too much text. Export a shorter statement or a CSV.")
            lines = text.split("\n")
            if opening_balance is None and not rows:
                opening_balance = _find_opening_balance(lines)
            table_header = next((i for i, line in enumerate(lines)
                                 if re.search(r'\bdate\b.*\b(?:transaction|description|details)\b.*\bamount\b', line, re.I)), None)
            for index, line in enumerate(lines):
                parsed = _parse_line(line)
                if parsed:
                    rows.append(parsed)
                    if len(rows) > MAX_TRANSACTION_ROWS:
                        raise StatementRejected(f"A statement can contain at most {MAX_TRANSACTION_ROWS:,} transactions.")
                elif _DATE_RE.match(line) and not _OPENING_RE.search(line):
                    # A letter's issue date above a transaction table is not
                    # an incomplete transaction. Dated rows in the table still fail.
                    if table_header is not None and index < table_header and _DATE_RE.fullmatch(line.strip()):
                        continue
                    raise StatementRejected("A dated PDF row could not be read completely. Export a CSV instead.")
            page.close()
    if not rows:
        raise ValueError(
            "Couldn't read any transactions from this PDF. It may be a scanned "
            "image or an unsupported layout. Try exporting a CSV from your bank."
        )

    return pd.DataFrame(_apply_signs(rows, opening_balance))
