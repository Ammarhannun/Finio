"""LLM-powered merchant categorisation.

One batched gpt-4o-mini call classifies every UNIQUE merchant with a
confidence level. Dramatically more accurate than the tiny Naive-Bayes model
because the LLM already knows what real-world merchants are. Results are
cached per merchant (in the user's snapshot), so repeat uploads only pay for
NEW merchants. Returns None without a key so callers fall back gracefully.
"""

import json
import os

from config import OPENAI_MODEL

CHUNK = 120          # merchants per API call (keeps responses well-formed)
CONFIDENCES = {"high", "medium", "low"}


def has_llm():
    return bool(os.getenv("OPENAI_API_KEY"))


def categorise_merchants(merchants, categories):
    """Classify merchant names → {merchant: {"category": str|None, "confidence": str}}.

    `categories` is the allowed spend-category list (built-ins + the user's
    custom ones). A merchant the model can't place gets category None and
    confidence "low" — those become quiz questions. Returns None when there is
    no key or every call fails (caller falls back to the local model).
    """
    merchants = [m for m in dict.fromkeys(merchants) if str(m).strip()]
    if not merchants or not has_llm():
        return None

    from openai import OpenAI
    client = OpenAI()

    system = (
        "You classify Australian bank-statement merchant names into spending "
        "categories. Categories (use EXACTLY these strings): "
        + json.dumps(list(categories))
        + '. Respond with JSON: {"results": [{"m": merchant, "c": category-or-null, '
        '"conf": "high"|"medium"|"low"}]}. Rules: use null for c when you '
        "genuinely cannot tell (person-to-person payments, opaque references, "
        "unknown acronyms) and set conf to low. Do not guess wildly; medium "
        "means plausible, high means certain. Every input merchant must appear "
        "exactly once in results."
    )

    out = {}
    any_ok = False
    for i in range(0, len(merchants), CHUNK):
        batch = merchants[i:i + CHUNK]
        try:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(batch)},
                ],
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            for row in data.get("results", []):
                m = str(row.get("m", "")).strip()
                c = row.get("c")
                conf = row.get("conf") if row.get("conf") in CONFIDENCES else "low"
                if not m:
                    continue
                if c is not None and c not in categories:
                    # Model invented a label → treat as unsure, not wrong data.
                    c, conf = None, "low"
                out[m] = {"category": c, "confidence": conf}
            any_ok = True
        except Exception:
            continue  # one bad chunk shouldn't kill the rest

    if not any_ok:
        return None
    # Anything the model skipped counts as unsure.
    for m in merchants:
        out.setdefault(m, {"category": None, "confidence": "low"})
    return out


def build_questions(df, llm_results, overrides=None, limit=6):
    """The short quiz: merchants Finio is genuinely unsure about, ranked by
    money at stake, capped at `limit`.

    Two sources:
    - expenses whose LLM confidence is low / category unknown
    - transfer-flow merchants moving real money (classic 'PAYID JOHN' — only
      the user knows if that's income, spending or an internal transfer)
    Merchants the user already has a rule for are never asked about.
    """
    if df is None or df.empty:
        return []
    known = set()
    for r in overrides or []:
        if r.get("match"):
            known.add(str(r["match"]).strip().upper())

    name_col = "merchant_clean" if "merchant_clean" in df.columns else "description"
    questions = {}

    def add(merchant, kind, suggested, rows):
        key = merchant.strip().upper()
        if not merchant or key in known or key in questions:
            return
        total = float(rows["amount"].abs().sum())
        questions[key] = {
            "merchant": merchant,
            "kind": kind,                      # 'category' or 'flow'
            "suggested": suggested,            # model's best guess (may be None)
            "count": int(len(rows)),
            "total": round(total, 2),
        }

    # Unsure expenses.
    exp = df[df["flow"] == "expense"] if "flow" in df.columns else df[df["amount"] < 0]
    if llm_results:
        for merchant, res in llm_results.items():
            if res.get("confidence") == "low" or res.get("category") is None:
                rows = exp[exp[name_col].astype(str) == merchant]
                if len(rows):
                    add(merchant, "category", res.get("category"), rows)

    # Money-moving transfers (only the user knows what these really are).
    if "flow" in df.columns:
        tr = df[df["flow"] == "transfer"]
        if not tr.empty:
            for merchant, rows in tr.groupby(tr[name_col].astype(str)):
                if rows["amount"].abs().sum() >= 100:
                    add(merchant, "flow", "transfer", rows)

    ranked = sorted(questions.values(), key=lambda q: q["total"], reverse=True)
    return ranked[:limit]
