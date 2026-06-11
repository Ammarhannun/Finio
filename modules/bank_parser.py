import re

import pandas as pd
from config import (
    COMMON_DATE_HEADERS,
    COMMON_AMOUNT_HEADERS,
    COMMON_DEBIT_HEADERS,
    COMMON_CREDIT_HEADERS,
    COMMON_DESCRIPTION_HEADERS,
    COMMON_BALANCE_HEADERS,
    COMMON_TYPE_HEADERS,
    DATE_FORMATS,
    NORMALIZED_COLUMNS,
    MERCHANT_PREFIXES,
    MERCHANT_NOISE,
    MERCHANT_TRAILING,
    SUBURB_STOPWORDS,
)

_KNOWN_HEADERS = (
    COMMON_DATE_HEADERS
    + COMMON_AMOUNT_HEADERS
    + COMMON_DEBIT_HEADERS
    + COMMON_CREDIT_HEADERS
    + COMMON_DESCRIPTION_HEADERS
    + COMMON_BALANCE_HEADERS
    + COMMON_TYPE_HEADERS
)


def _find_column(df, candidates):
    """Case-insensitive header lookup."""
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def _parse_dates(series):
    """Try known date formats in order, then fall back to day-first inference."""
    s = series.astype(str).str.strip()
    for fmt in DATE_FORMATS:
        parsed = pd.to_datetime(s, format=fmt, errors="coerce")
        if parsed.notna().mean() > 0.7:
            return parsed
    return pd.to_datetime(s, dayfirst=True, errors="coerce")


def _is_numeric(series):
    return pd.to_numeric(series, errors="coerce").notna().mean() > 0.7


def _is_date(series):
    return _parse_dates(series).notna().mean() > 0.7


def _has_header(df):
    known = {h.lower() for h in _KNOWN_HEADERS}
    return any(str(c).strip().lower() in known for c in df.columns)


def _assign_headerless_columns(df):
    """Infer column roles for CSVs exported without a header row (e.g. CommBank)."""
    date_col = next((c for c in df.columns if _is_date(df[c])), None)
    numeric_cols = [
        c for c in df.columns if c != date_col and _is_numeric(df[c])
    ]
    text_cols = [
        c for c in df.columns if c != date_col and c not in numeric_cols
    ]

    rename = {}
    if date_col is not None:
        rename[date_col] = "date"
    if text_cols:
        rename[text_cols[0]] = "description"

    # amount = the numeric column that has negatives (expenses); else first numeric
    amount_col = None
    for c in numeric_cols:
        if (pd.to_numeric(df[c], errors="coerce") < 0).any():
            amount_col = c
            break
    if amount_col is None and numeric_cols:
        amount_col = numeric_cols[0]
    if amount_col is not None:
        rename[amount_col] = "amount"

    # balance = a remaining numeric column (running total)
    remaining = [c for c in numeric_cols if c != amount_col]
    if remaining:
        rename[remaining[-1]] = "balance"

    return df.rename(columns=rename)


def load_csv(path):
    df = pd.read_csv(path)
    if not _has_header(df):
        df = pd.read_csv(path, header=None)
        df = _assign_headerless_columns(df)
    return df


def normalize(df):
    df = df.copy()

    date_col = _find_column(df, COMMON_DATE_HEADERS) or (
        "date" if "date" in df.columns else None
    )
    desc_col = _find_column(df, COMMON_DESCRIPTION_HEADERS) or (
        "description" if "description" in df.columns else None
    )
    amount_col = _find_column(df, COMMON_AMOUNT_HEADERS) or (
        "amount" if "amount" in df.columns else None
    )
    debit_col = _find_column(df, COMMON_DEBIT_HEADERS)
    credit_col = _find_column(df, COMMON_CREDIT_HEADERS)
    balance_col = _find_column(df, COMMON_BALANCE_HEADERS) or (
        "balance" if "balance" in df.columns else None
    )
    type_col = _find_column(df, COMMON_TYPE_HEADERS)

    missing = []
    if date_col is None:
        missing.append("date")
    if desc_col is None:
        missing.append("description")
    if amount_col is None and not (debit_col or credit_col):
        missing.append("amount")
    if missing:
        raise ValueError(
            f"Missing required columns: {missing}. Found: {list(df.columns)}"
        )

    out = pd.DataFrame()
    out["date"] = _parse_dates(df[date_col])
    out["description"] = df[desc_col]

    if amount_col is not None:
        amount = pd.to_numeric(df[amount_col], errors="coerce")
        # Some banks export positive amounts + a Type/Dr-Cr column to show direction
        non_null = amount.dropna()
        if type_col is not None and len(non_null) and (non_null >= 0).all():
            type_text = df[type_col].astype(str).str.upper()
            is_debit = type_text.str.contains(r"DEBIT|DR|WITHDRAW", regex=True)
            amount = amount.where(~is_debit, -amount)
        out["amount"] = amount
    else:
        # Separate Debit/Credit columns → single signed amount
        debit = (
            pd.to_numeric(df[debit_col], errors="coerce").fillna(0)
            if debit_col
            else 0
        )
        credit = (
            pd.to_numeric(df[credit_col], errors="coerce").fillna(0)
            if credit_col
            else 0
        )
        out["amount"] = credit - debit

    if balance_col is not None:
        out["balance"] = pd.to_numeric(df[balance_col], errors="coerce")

    cols = [c for c in NORMALIZED_COLUMNS if c in out.columns]
    out = out[cols]
    out = out.dropna(subset=["date", "amount"])
    return out.reset_index(drop=True)


_STORE_NUMBER_RE = re.compile(r"\b(?:STORE|ST|#)?\s?\d{3,}\b")  # store/card numbers
_XX_REF_RE = re.compile(r"\bXX\w+\b")  # masked account refs e.g. XX0642


def clean_descriptions(df):
    df = df.copy()

    df["description"] = (
        df["description"]
        .astype(str)
        .str.upper()
        .str.replace("*", " ", regex=False)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )

    df["merchant_clean"] = df["description"].apply(clean_merchant_name)
    return df


def clean_merchant_name(name):
    """Strip card-network prefixes, legal suffixes, store numbers, trailing
    state/country tokens and suburb noise so the real trading name is left for
    the categoriser. Input is already UPPERCASED by clean_descriptions.

    "SQ FEEL GOOD BURGERS"            -> "FEEL GOOD BURGERS"
    "JC CHICKEN PTY. LTD RYDE NSWAU"  -> "JC CHICKEN"
    "TRANSFER TO XX0642 COMMBANK APP" -> "TRANSFER TO COMMBANK APP"
    """
    text = str(name).upper().strip()

    for prefix in MERCHANT_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break

    text = text.replace(".", " ")
    text = _XX_REF_RE.sub(" ", text)

    for noise in MERCHANT_NOISE:
        text = text.replace(noise, " ")

    text = _STORE_NUMBER_RE.sub(" ", text)

    # Drop trailing state/country tokens and suburb words, working from the end
    # so we never strip a word that is part of the real name in the middle.
    tokens = [t for t in text.split() if t]
    while tokens and (tokens[-1] in MERCHANT_TRAILING or tokens[-1] in SUBURB_STOPWORDS):
        tokens.pop()
    # Remove any remaining suburb words anywhere in what's left.
    tokens = [t for t in tokens if t not in SUBURB_STOPWORDS]

    cleaned = " ".join(tokens).strip()
    return cleaned or text.strip()


def parse_bank_csv(path):
    if str(path).lower().endswith(".pdf"):
        from modules.pdf_parser import extract_transactions
        df = extract_transactions(path)
    else:
        df = load_csv(path)
    df = normalize(df)
    df = clean_descriptions(df)
    return df
