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


def categorise_data(df):
    df = df.copy()
    df["category"] = None

    # 1. Transfers are tagged outright — never spend, never sent to the model.
    if "is_transfer" in df.columns:
        df.loc[df["is_transfer"], "category"] = TRANSFERS_LABEL

    # Only genuine expenses (negative, non-transfer) need a spend category.
    expense_mask = (df["amount"] < 0) & (df["category"].isna())
    if not expense_mask.any():
        return df

    # 2. Rules layer first, on the cleaned merchant name.
    name_col = "merchant_clean" if "merchant_clean" in df.columns else "description"
    df.loc[expense_mask, "category"] = df.loc[expense_mask, name_col].apply(rule_category)

    # 3. ML model only fills genuine unknowns the rules couldn't place.
    unknown_mask = expense_mask & df["category"].isna()
    if unknown_mask.any():
        X, y = load_training_data()
        model = train_model(X, y)
        df.loc[unknown_mask, "category"] = model.predict(
            df.loc[unknown_mask, name_col]
        )

    return df
