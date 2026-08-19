import hashlib

import pandas as pd
import re as _re

from config import CATEGORY_RULES, TRANSFERS_LABEL, TRAINING_CSV
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline


def load_training_data():
    df = pd.read_csv(TRAINING_CSV)
    X = df["description"]
    y = df["category"]
    return X, y

def train_model(X, y):
    model = Pipeline([
            ("vectorizer", CountVectorizer()),
            ("classifier", MultinomialNB()),
        ])
    model.fit(X, y)
    return model


_MODEL = None
_USER_MODELS = {}          # signature -> model trained with that user's examples
_MAX_USER_MODELS = 20      # bound memory
_EXAMPLE_WEIGHT = 3        # repeat user corrections so they outweigh one base row


def examples_from_overrides(overrides):
    """Turn the user's saved category corrections into (text -> category) training
    examples. Text-match rules carry the text directly; exact tx_key rules are
    skipped here (they're already enforced verbatim by apply_category_overrides).
    This is the 'active learning' signal: corrections teach the model to
    generalise to NEW similar merchants on the next upload.
    """
    out = []
    for r in overrides or []:
        cat, text = r.get("category"), r.get("match")
        if cat and text:
            out.append((str(text), str(cat)))
    return out


def _sig(examples):
    return hashlib.md5(
        "|".join(f"{t}=>{c}" for t, c in sorted(examples)).encode()
    ).hexdigest()


def get_model(extra_examples=None):
    """Return the trained categoriser, cached so it trains once per process.

    With `extra_examples` (the user's corrections) it trains an augmented model
    — base training data plus the user's (text->category) examples, weighted so
    they actually move predictions — cached per example-set signature.
    """
    global _MODEL
    if not extra_examples:
        if _MODEL is None:
            X, y = load_training_data()
            _MODEL = train_model(X, y)
        return _MODEL

    sig = _sig(extra_examples)
    if sig not in _USER_MODELS:
        X, y = load_training_data()
        ex_X = pd.Series([t for t, _ in extra_examples] * _EXAMPLE_WEIGHT)
        ex_y = pd.Series([c for _, c in extra_examples] * _EXAMPLE_WEIGHT)
        Xa = pd.concat([X, ex_X], ignore_index=True)
        ya = pd.concat([y, ex_y], ignore_index=True)
        if len(_USER_MODELS) >= _MAX_USER_MODELS:
            _USER_MODELS.clear()
        _USER_MODELS[sig] = train_model(Xa, ya)
    return _USER_MODELS[sig]


# Keyword rules are matched on WORD BOUNDARIES, not raw substrings. Plain
# `in` matching produced confidently wrong categories that were hard to spot:
# "COTTON ON BONDI" hit the Housing keyword "BOND", "SUPERCHEAP AUTO" hit the
# Groceries keyword "SUPER", and "MARKETPLACE" hit "MARKET". Because the rules
# layer used to run before everything else, those wrong answers won outright.
_RULE_PATTERNS = [
    (
        category,
        _re.compile(
            "|".join(
                r"\b" + _re.escape(k.strip()).replace(r"\ ", r"\s+") + r"\b"
                for k in keywords
            )
        ),
    )
    for category, keywords in CATEGORY_RULES
]


def rule_category(name):
    """Keyword categoriser: the first rule whose keyword appears as a whole word
    wins. Rules are ordered (specific before general) in config.CATEGORY_RULES.
    Returns a category string, or None if no rule matches.

    This is the OFFLINE path and the safety net — when a key is set the LLM
    decides first (see categorise_data), because a keyword list cannot tell
    that "CALTEX WOOLWORTHS FUEL" is fuel rather than groceries.
    """
    text = str(name).upper()
    for category, pattern in _RULE_PATTERNS:
        if pattern.search(text):
            return category
    return None


def _merchant_context(df, mask, name_col, merchants):
    """Spending signal per merchant for the model: how often, how much, and a
    couple of raw bank descriptions.

    The cleaned name alone is frequently ambiguous — amount and frequency
    resolve a lot of it (a $9 fuel-station charge is a snack, a $75 one is
    fuel; an identical amount every month is a subscription), and the raw
    description keeps words that cleaning strips.
    """
    wanted = set(merchants)
    sub = df.loc[mask, [name_col, "amount"] + (
        ["description"] if "description" in df.columns and name_col != "description" else []
    )]
    out = {}
    for merchant, rows in sub.groupby(sub[name_col].astype(str)):
        if merchant not in wanted:
            continue
        amounts = rows["amount"].abs()
        entry = {
            "count": int(len(rows)),
            "total": round(float(amounts.sum()), 2),
            "avg": round(float(amounts.mean()), 2),
        }
        if "description" in rows.columns:
            samples = [str(d).strip() for d in rows["description"].head(2) if str(d).strip()]
            # Only worth sending when it adds something the clean name lacks.
            entry["samples"] = [s for s in samples if s.upper() != merchant.upper()]
        out[merchant] = entry
    return out


def categorise_data(df, user_examples=None, llm_cache=None, categories=None,
                    llm_meta=None):
    """Categorise expenses. Order of authority:
    1. keyword rules (deterministic, free)
    2. LLM (cached per merchant; the accuracy workhorse when a key is set)
    3. Naive Bayes (offline fallback only)
    Category overrides (the user's own corrections) are applied AFTER this by
    the pipeline, so they always win.

    `llm_cache` is {merchant: {category, confidence}} from previous runs — only
    NEW merchants hit the API. When `llm_meta` (a dict) is supplied it receives
    the merged cache so the caller can persist it.
    """
    df = df.copy()
    df["category"] = None

    # 1. Transfers are tagged outright — never spend, never sent to the model.
    if "is_transfer" in df.columns:
        df.loc[df["is_transfer"], "category"] = TRANSFERS_LABEL

    # Only genuine expenses need a spend category. Use flow when present so
    # refunds (positive-amount expenses) get categorised too; fall back to sign.
    if "flow" in df.columns:
        expense_mask = (df["flow"] == "expense") & (df["category"].isna())
    else:
        expense_mask = (df["amount"] < 0) & (df["category"].isna())
    if not expense_mask.any():
        return df

    name_col = "merchant_clean" if "merchant_clean" in df.columns else "description"

    # 2. LLM decides FIRST, for every expense merchant (cache-first).
    #
    # This used to run only on merchants the keyword rules failed to match,
    # which capped accuracy at whatever the keyword list could do: the rules
    # confidently called "CALTEX WOOLWORTHS FUEL" Groceries and "AMPOL FOODARY"
    # Food & Dining, and the model — which knows both are petrol stations —
    # never got asked. The model leads now; rules are the offline fallback.
    from config import CATEGORIES
    from modules.llm_categoriser import categorise_merchants

    cats = list(categories or CATEGORIES)
    cache = dict(llm_cache or {})
    names = df.loc[expense_mask, name_col].astype(str)
    new_merchants = [m for m in names.unique() if m not in cache]
    if new_merchants:
        fresh = categorise_merchants(
            new_merchants, cats,
            context=_merchant_context(df, expense_mask, name_col, new_merchants),
        )
        if fresh:
            cache.update(fresh)
    if llm_meta is not None:
        llm_meta["cache"] = cache
    if cache:
        # Only take the model's answer when it is actually confident. A "low"
        # answer falls through to the rules and becomes a quiz question, so an
        # uncertain guess never silently sets a category.
        def _confident(m):
            res = cache.get(m) or {}
            if res.get("category") and res.get("confidence") in ("high", "medium"):
                return res["category"]
            return None

        df.loc[expense_mask, "category"] = names.map(_confident).values

    # 3. Keyword rules fill in whatever the model could not place (and carry
    #    the whole job when there is no API key).
    unknown_mask = expense_mask & df["category"].isna()
    if unknown_mask.any():
        df.loc[unknown_mask, "category"] = (
            df.loc[unknown_mask, name_col].apply(rule_category)
        )

    # 4. Naive Bayes only for whatever is STILL unknown (no key / LLM unsure).
    unknown_mask = expense_mask & df["category"].isna()
    if unknown_mask.any():
        model = get_model(user_examples)
        df.loc[unknown_mask, "category"] = model.predict(
            df.loc[unknown_mask, name_col]
        )

    return df
