import hashlib

import pandas as pd
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


def rule_category(name):
    """PRIMARY categoriser: first keyword found in the cleaned merchant name
    wins. Rules are ordered (specific before general) in config.CATEGORY_RULES.
    Returns a category string, or None if no rule matches.
    """
    text = str(name).upper()
    for category, keywords in CATEGORY_RULES:
        for keyword in keywords:
            if keyword in text:
                return category
    return None


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

    # 2. Rules layer first, on the cleaned merchant name.
    name_col = "merchant_clean" if "merchant_clean" in df.columns else "description"
    df.loc[expense_mask, "category"] = df.loc[expense_mask, name_col].apply(rule_category)

    # 3. LLM layer for merchants the rules didn't recognise (cache-first).
    unknown_mask = expense_mask & df["category"].isna()
    if unknown_mask.any():
        from config import CATEGORIES
        from modules.llm_categoriser import categorise_merchants

        cats = list(categories or CATEGORIES)
        cache = dict(llm_cache or {})
        names = df.loc[unknown_mask, name_col].astype(str)
        new_merchants = [m for m in names.unique() if m not in cache]
        if new_merchants:
            fresh = categorise_merchants(new_merchants, cats)
            if fresh:
                cache.update(fresh)
        if llm_meta is not None:
            llm_meta["cache"] = cache
        if cache:
            mapped = names.map(lambda m: (cache.get(m) or {}).get("category"))
            df.loc[unknown_mask, "category"] = mapped.values

    # 4. Naive Bayes only for whatever is STILL unknown (no key / LLM unsure).
    unknown_mask = expense_mask & df["category"].isna()
    if unknown_mask.any():
        model = get_model(user_examples)
        df.loc[unknown_mask, "category"] = model.predict(
            df.loc[unknown_mask, name_col]
        )

    return df
