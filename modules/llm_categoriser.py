"""LLM-powered merchant categorisation.

One batched gpt-4o-mini call classifies every UNIQUE merchant with a
confidence level. Dramatically more accurate than the tiny Naive-Bayes model
because the LLM already knows what real-world merchants are. Results are
cached per merchant (in the user's snapshot), so repeat uploads only pay for
NEW merchants. Returns None without a key so callers fall back gracefully.
"""

import json
import os

from config import OPENAI_MODEL, OWN_ACCOUNT_HINTS

CHUNK = 120          # merchants per API call (keeps responses well-formed)
CONFIDENCES = {"high", "medium", "low"}


def has_llm():
    """True when the model layer should run.

    FINIO_DISABLE_LLM forces the offline path (keyword rules + Naive Bayes).
    The test suite sets it so tests stay fast, deterministic and free — now
    that the model classifies EVERY merchant rather than only the ones the
    rules missed, leaving it on turned the suite into a network-bound job.
    """
    if os.getenv("FINIO_DISABLE_LLM"):
        return False
    return bool(os.getenv("OPENAI_API_KEY"))


# Guidance for the cases a keyword list gets wrong. These are the failure
# modes that actually showed up on real statements: a merchant's NAME often
# points at the wrong category (fuel stations sell food, supermarkets sell
# fuel, clothing shops carry suburb names that look like other categories).
_DISAMBIGUATION = (
    "Judge what was actually BOUGHT, not what words appear in the name:\n"
    "- Fuel and service stations (Ampol, Caltex, BP, 7-Eleven, Shell, United) "
    "are Transport even when the name mentions food or a supermarket brand.\n"
    "- Supermarket-branded fuel (e.g. 'CALTEX WOOLWORTHS FUEL', "
    "'COLES EXPRESS') is Transport, not Groceries.\n"
    "- Independent grocers and small supermarkets (Foodworks, Supabarn, IGA, "
    "Harris Farm) are Groceries even if you do not recognise the name.\n"
    "- Cafes, bakeries and restaurants with place-style names "
    "('The Grounds of Alexandria') are Food & Dining.\n"
    "- Suburb and state words (BONDI, RYDE, NSW, PARRAMATTA) are location "
    "noise. Never categorise from them.\n"
    "- Parking and tolls are Transport even when the name contains a shop or "
    "market word.\n"
    "- Pharmacies are Health; supermarkets that also sell medicine are "
    "Groceries.\n"
    "- Gyms and fitness memberships are Subscriptions, not Health — they are "
    "a recurring membership fee, which is how this app groups them.\n"
    "- Car parts, servicing and repairs (Supercheap Auto, Repco, mechanics) "
    "are Transport, because they are costs of running a vehicle.\n"
    "- Phone, mobile and internet bills (Telstra, Optus, Vodafone, TPG, "
    "Belong) are Subscriptions — they are recurring plans.\n"
    "- Utility bills — electricity, gas, water, internet — are Housing & Rent. "
    "That covers retailers you may not recognise by name (Engie, Alinta, "
    "Momentum, Red Energy, Dodo, Superloop); a numeric reference in the name "
    "is billing noise, not a reason to give up.\n"
    "- Cinemas, events, concerts, ticketing, games and attractions are "
    "Entertainment.\n"
    "- Never answer \"Other\" as a way of hedging. \"Other\" is a real "
    "category, correct for genuine miscellaneous spending. But if you truly "
    "cannot identify a merchant, "
    "answer null with conf low so a keyword fallback can try.\n"
)


def _format_input(merchants, context):
    """One line per merchant, with the spending signal we have.

    The merchant name alone is often ambiguous. Amount and frequency
    disambiguate a lot: a $9 charge at a fuel station is a snack, a $75 one is
    fuel; a fixed amount every month is a subscription. The raw bank
    description is included too because cleaning can strip useful words.
    """
    rows = []
    for m in merchants:
        c = (context or {}).get(m) or {}
        bits = [f'"{m}"']
        if c.get("count"):
            bits.append(f"seen {c['count']}x")
        if c.get("avg") is not None:
            bits.append(f"avg ${c['avg']:,.2f}")
        if c.get("total") is not None:
            bits.append(f"total ${c['total']:,.2f}")
        if c.get("samples"):
            bits.append("raw: " + " | ".join(c["samples"][:2]))
        rows.append(" — ".join(bits))
    return "\n".join(rows)


def categorise_merchants(merchants, categories, context=None):
    """Classify merchant names → {merchant: {"category": str|None, "confidence": str}}.

    `categories` is the allowed spend-category list (built-ins + the user's
    custom ones). `context` optionally maps merchant → {count, total, avg,
    samples} so the model can use spending behaviour, not just the name.

    A merchant the model can't place gets category None and confidence "low" —
    those become quiz questions. Returns None when there is no key or every
    call fails (caller falls back to rules, then the local model).
    """
    merchants = [m for m in dict.fromkeys(merchants) if str(m).strip()]
    if not merchants or not has_llm():
        return None

    from openai import OpenAI
    client = OpenAI()

    system = (
        "You classify Australian bank-statement merchants into spending "
        "categories. Categories (use EXACTLY these strings): "
        + json.dumps(list(categories))
        + ".\n\n" + _DISAMBIGUATION
        + '\nRespond with JSON: {"results": [{"m": merchant, "c": category-or-null, '
        '"conf": "high"|"medium"|"low"}]}. Copy "m" back EXACTLY as given. '
        "Use null for c only when you genuinely cannot tell (person-to-person "
        "payments, opaque references, unknown acronyms) and set conf to low. "
        "Do not guess wildly; medium means plausible, high means certain. "
        "Every input merchant must appear exactly once in results."
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
                    {"role": "user", "content": _format_input(batch, context)},
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

    def add(merchant, kind, suggested, rows, hint=None):
        key = merchant.strip().upper()
        if not merchant or key in known or key in questions:
            return
        total = float(rows["amount"].abs().sum())
        questions[key] = {
            "merchant": merchant,
            "kind": kind,                      # 'category' or 'flow'
            "suggested": suggested,            # best guess (may be None)
            "hint": hint,                      # why we're suggesting it
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
    # A RECURRING INCOMING transfer from the same sender is usually real income
    # (family support, a housemate's share, cash work) rather than the user
    # shuffling their own money — so suggest "income" for those instead of
    # blindly defaulting to "transfer".
    if "flow" in df.columns:
        tr = df[df["flow"] == "transfer"]
        if not tr.empty:
            for merchant, rows in tr.groupby(tr[name_col].astype(str)):
                if rows["amount"].abs().sum() < 100:
                    continue
                incoming = rows[rows["amount"] > 0]
                mostly_in = len(incoming) >= max(1, int(len(rows) * 0.8))
                # Your own savings/app transfers are NOT income, however often
                # money comes back in.
                own = any(h in str(merchant).upper() for h in OWN_ACCOUNT_HINTS)
                if own:
                    add(merchant, "flow", "transfer", rows,
                        hint="looks like your own account")
                elif mostly_in and len(incoming) >= 3:
                    add(merchant, "flow", "income", rows,
                        hint=f"money in {len(incoming)} times — looks like income")
                elif mostly_in:
                    add(merchant, "flow", "transfer", rows, hint="money coming in")
                else:
                    add(merchant, "flow", "transfer", rows, hint="money going out")

    ranked = sorted(questions.values(), key=lambda q: q["total"], reverse=True)
    return ranked[:limit]
