"""
Full Finio test suite — run from project root:
  python -m tests.test_full_suite
"""

import io
import time
import os
import sys
import tempfile
import traceback
from datetime import date
from pathlib import Path

# project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The categoriser sends every merchant to the model now, so leaving that on
# would make this suite slow, costly and dependent on the network. Tests cover
# the offline path (keyword rules + Naive Bayes); eval/run_eval.py measures the
# model path against labelled data.
os.environ.setdefault("FINIO_DISABLE_LLM", "1")

from config import CATEGORIES, DISCLAIMER, SAMPLE_CSV, TRAINING_CSV

passed = 0
failed = 0
skipped = 0
failures = []
ALL_TESTS = []


def test(name):
    def decorator(fn):
        def wrapper():
            global passed, failed, skipped
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except SkipTest as e:
                skipped += 1
                print(f"  SKIP  {name} — {e}")
            except Exception as e:
                failed += 1
                failures.append((name, e, traceback.format_exc()))
                print(f"  FAIL  {name} — {e}")

        ALL_TESTS.append((name, wrapper))
        return wrapper

    return decorator


class SkipTest(Exception):
    pass


def assert_eq(a, b, msg=""):
    assert a == b, f"{msg} expected {b!r}, got {a!r}"


def assert_in(item, container, msg=""):
    assert item in container, f"{msg}{item!r} not in {container!r}"


def assert_true(cond, msg=""):
    assert cond, msg or "assertion failed"


def assert_raises(exc_type, fn, msg=""):
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(msg or f"expected {exc_type.__name__}")


# ── bank_parser ──────────────────────────────────────────────────────────────


@test("bank_parser: parse sample CSV")
def _():
    from modules.bank_parser import parse_bank_csv

    df = parse_bank_csv(SAMPLE_CSV)
    assert_eq(len(df), 20)
    assert_in("date", df.columns)
    assert_in("amount", df.columns)
    assert_in("description", df.columns)


@test("bank_parser: cleans asterisks in descriptions")
def _():
    from modules.bank_parser import parse_bank_csv

    df = parse_bank_csv(SAMPLE_CSV)
    uber = df[df["description"].str.contains("UBER EATS", na=False)].iloc[0]
    assert "*" not in uber["description"]


@test("bank_parser: missing columns raises ValueError")
def _():
    from modules.bank_parser import normalize
    import pandas as pd

    bad = pd.DataFrame({"foo": [1], "bar": [2]})
    assert_raises(ValueError, lambda: normalize(bad))


def _parse_csv_string(csv_text):
    from modules.bank_parser import parse_bank_csv

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv_text)
        path = f.name
    try:
        return parse_bank_csv(path)
    finally:
        os.unlink(path)


def _check_income_expense(df, income, expense):
    inc = round(df[df["amount"] > 0]["amount"].sum(), 2)
    exp = round(df[df["amount"] < 0]["amount"].sum(), 2)
    assert_eq(inc, income, "income")
    assert_eq(exp, expense, "expense")


@test("bank_parser: separate Debit/Credit columns")
def _():
    df = _parse_csv_string(
        "Date,Description,Debit,Credit\n"
        "01/01/2026,SALARY,,3200.00\n"
        "02/01/2026,UBER EATS,24.50,\n"
    )
    _check_income_expense(df, 3200.0, -24.5)


@test("bank_parser: ISO date format YYYY-MM-DD")
def _():
    df = _parse_csv_string(
        "Date,Amount,Description\n"
        "2026-01-01,3200.00,SALARY\n"
        "2026-01-02,-24.50,UBER EATS\n"
    )
    _check_income_expense(df, 3200.0, -24.5)


@test("bank_parser: headerless CSV (CommBank style)")
def _():
    df = _parse_csv_string(
        "01/01/2026,3200.00,SALARY,5200.00\n"
        "02/01/2026,-24.50,UBER EATS,5175.50\n"
    )
    _check_income_expense(df, 3200.0, -24.5)


@test("bank_parser: positive amounts with Type column")
def _():
    df = _parse_csv_string(
        "Date,Amount,Description,Type\n"
        "01/01/2026,3200.00,SALARY,CREDIT\n"
        "02/01/2026,24.50,UBER EATS,DEBIT\n"
    )
    _check_income_expense(df, 3200.0, -24.5)


@test("bank_parser: case-insensitive / alt headers")
def _():
    df = _parse_csv_string(
        "date,value,narrative\n"
        "01/01/2026,3200.00,SALARY\n"
        "02/01/2026,-24.50,UBER EATS\n"
    )
    _check_income_expense(df, 3200.0, -24.5)


# ── pdf_parser ───────────────────────────────────────────────────────────────


@test("pdf_parser: parses a transaction line")
def _():
    from modules.pdf_parser import _parse_line

    row = _parse_line("01/01/2026 WOOLWORTHS 1234 84.20 3,365.80")
    assert row is not None, "expected a parsed row"
    assert_eq(row["description"], "WOOLWORTHS 1234")
    assert_eq(row["raw_amount"], 84.20)
    assert_eq(row["balance"], 3365.80)


@test("pdf_parser: ignores non-transaction lines")
def _():
    from modules.pdf_parser import _parse_line

    assert _parse_line("Account 06 2000 1234 5678") is None
    assert _parse_line("Opening balance 250.00") is None


@test("pdf_parser: CR/DR markers set sign")
def _():
    from modules.pdf_parser import _apply_signs, _parse_line

    rows = [
        _parse_line("01/01/2026 SALARY 3,200.00 CR"),
        _parse_line("02/01/2026 UBER EATS 24.50 DR"),
    ]
    signed = _apply_signs(rows)
    assert_eq(signed[0]["amount"], 3200.0)
    assert_eq(signed[1]["amount"], -24.5)


@test("pdf_parser: opening balance seeds first row sign")
def _():
    from modules.pdf_parser import _apply_signs, _find_opening_balance, _parse_line

    lines = [
        "Opening balance 250.00",
        "01/01/2026 SALARY ACME 3,200.00 3,450.00",
        "02/01/2026 WOOLWORTHS 84.20 3,365.80",
    ]
    opening = _find_opening_balance(lines)
    assert_eq(opening, 250.0)
    rows = [r for line in lines if (r := _parse_line(line))]
    signed = _apply_signs(rows, opening)
    assert_eq(signed[0]["amount"], 3200.0, "salary should be income")
    assert_eq(signed[1]["amount"], -84.2, "groceries should be expense")


# ── data_processor ───────────────────────────────────────────────────────────


@test("data_processor: metrics on sample data")
def _():
    from modules.bank_parser import parse_bank_csv
    from modules.data_processor import process_transactions

    df, metrics = process_transactions(parse_bank_csv(SAMPLE_CSV))
    assert_eq(metrics["total_income"], 3200.0)
    assert_eq(metrics["transaction_count"], 20)
    assert_true(metrics["total_spent"] > 1000)
    assert_true(df["is_expense"].sum() == 19)
    assert_true(df["is_weekend"].dtype == bool)


# ── categoriser ──────────────────────────────────────────────────────────────


@test("categoriser: training data loads")
def _():
    from modules.categoriser import load_training_data

    X, y = load_training_data()
    assert_true(len(X) >= 30)
    assert_in("Food & Dining", set(y))


@test("categoriser: expenses categorised, income not")
def _():
    from modules.bank_parser import parse_bank_csv
    from modules.categoriser import categorise_data
    from modules.data_processor import process_transactions

    df, _ = process_transactions(parse_bank_csv(SAMPLE_CSV))
    df = categorise_data(df)
    income = df[df["amount"] > 0]
    expenses = df[df["amount"] < 0]
    assert_true(income["category"].isna().all())
    assert_true(expenses["category"].notna().all())


# ── bill_detector ────────────────────────────────────────────────────────────


@test("bill_detector: keeps regular stable bills, drops variable and one-off")
def _():
    import pandas as pd

    from modules.bill_detector import detect_bills

    rows = []
    # Real bill: 4 monthly Netflix charges, identical amount.
    for m in range(4):
        rows.append({
            "date": pd.Timestamp("2026-01-15") + pd.DateOffset(months=m),
            "amount": -19.99, "description": "NETFLIX.COM AU",
            "merchant_clean": "NETFLIX.COM", "flow": "expense",
            "category": "Subscriptions",
        })
    # Not a bill: same restaurant monthly but wildly variable amounts (high CV).
    for m, amt in enumerate([12, 80, 35, 120]):
        rows.append({
            "date": pd.Timestamp("2026-01-03") + pd.DateOffset(months=m),
            "amount": -amt, "description": "SOME RESTAURANT",
            "merchant_clean": "SOME RESTAURANT", "flow": "expense",
            "category": "Food & Dining",
        })
    # Not a bill: a single one-off purchase.
    rows.append({
        "date": pd.Timestamp("2026-02-02"), "amount": -500,
        "description": "JB HI-FI", "merchant_clean": "JB HI-FI",
        "flow": "expense", "category": "Shopping",
    })

    bills = detect_bills(pd.DataFrame(rows))
    merchants = {b["merchant"] for b in bills}
    assert_in("NETFLIX.COM", merchants)
    assert_true("SOME RESTAURANT" not in merchants)
    assert_true("JB HI-FI" not in merchants)
    netflix = next(b for b in bills if b["merchant"] == "NETFLIX.COM")
    assert_eq(netflix["frequency"], "monthly")
    assert_eq(netflix["count"], 4)


@test("bill_detector: tolerates a skipped billing period")
def _():
    import pandas as pd

    from modules.bill_detector import _classify_interval

    # Fortnightly with one missed charge (42-day gap == 3 fortnights).
    dates = pd.to_datetime([
        "2026-01-01", "2026-01-15", "2026-01-29", "2026-03-12", "2026-03-26",
    ])
    label, regular = _classify_interval(dates)
    assert_eq(label, "fortnightly")
    assert_true(regular)


@test("data_processor: flags transfer keywords")
def _():
    from modules.data_processor import _is_transfer

    assert_true(_is_transfer("PAYID TRANSFER TO FRIEND"))
    assert_true(not _is_transfer("WOOLWORTHS 1234"))


# ── savings_forecaster ───────────────────────────────────────────────────────


@test("savings_forecaster: on track for sample goal")
def _():
    from modules.pipeline import run_full_pipeline

    f = run_full_pipeline(SAMPLE_CSV, goal_amount=5000, goal_date="2026-06-30")[
        "forecast"
    ]
    assert_true(f["on_track"])
    assert_true(f["current_saved"] > 2000)
    assert_true(f["projected_total"] > f["current_saved"])
    assert_in("disclaimer", f)


@test("savings_forecaster: current_saved equals income minus spent")
def _():
    from modules.pipeline import run_full_pipeline

    r = run_full_pipeline(SAMPLE_CSV)
    saved = r["metrics"]["total_income"] - r["metrics"]["total_spent"]
    assert_eq(round(saved, 2), r["forecast"]["current_saved"])


# ── analytics ────────────────────────────────────────────────────────────────


@test("analytics: risk low on sample")
def _():
    from modules.pipeline import run_full_pipeline

    a = run_full_pipeline(SAMPLE_CSV)["analysis"]
    assert_eq(a["risk_label"], "low")
    assert_true(len(a["category_breakdown"]) >= 5)
    pcts = sum(c["pct"] for c in a["category_breakdown"])
    assert_true(99 <= pcts <= 101)


@test("analytics: strong savings pattern detected")
def _():
    from modules.pipeline import run_full_pipeline

    patterns = run_full_pipeline(SAMPLE_CSV)["analysis"]["patterns"]
    types = {p["type"] for p in patterns}
    assert_in("strong_savings", types)


# ── spend_check ──────────────────────────────────────────────────────────────


# Deterministic metrics so verdicts don't depend on sample-data balance.
# burn=10/day → safety_buffer = 70; horizon 30 days → 300 burn over the window.
def _spend_metrics(net_saved=1000, latest_balance=None):
    return {
        "net_saved": net_saved,
        "latest_balance": latest_balance,
        "daily_burn_rate": 10,
    }


@test("spend_check: green for small purchase")
def _():
    from modules.spend_check import check_purchase

    # current 1000 - 10 - 300 = 690 >= buffer 70 → green
    result = check_purchase(None, _spend_metrics(), 10, days_ahead=30)
    assert_eq(result["verdict"], "green")
    assert_in("disclaimer", result)


@test("spend_check: red for huge purchase")
def _():
    from modules.spend_check import check_purchase

    # current 1000 - 5000 - 300 < 0 → red
    result = check_purchase(None, _spend_metrics(), 5000, days_ahead=30)
    assert_eq(result["verdict"], "red")


@test("spend_check: yellow for medium purchase")
def _():
    from modules.spend_check import check_purchase

    # current 1000 - 680 - 300 = 20: above 0 but below buffer 70 → yellow
    result = check_purchase(None, _spend_metrics(), 680, days_ahead=30)
    assert_eq(result["verdict"], "yellow")


@test("spend_check: prefers real balance over net_saved when present")
def _():
    from modules.spend_check import check_purchase

    # net_saved is negative (overspent period) but the real balance is healthy;
    # the verdict should use the balance, not net_saved.
    m = _spend_metrics(net_saved=-200, latest_balance=2000)
    result = check_purchase(None, m, 100, days_ahead=30)
    assert_eq(result["verdict"], "green")
    assert_true(result["uses_balance"])
    assert_eq(result["current_net"], 2000)


# ── invest ───────────────────────────────────────────────────────────────────


@test("invest: can_invest true on sample")
def _():
    from modules.pipeline import run_full_pipeline

    inv = run_full_pipeline(SAMPLE_CSV, age=22)["invest"]
    assert_true(inv["readiness"]["can_invest"])
    assert_eq(inv["etf"]["recommended"], "NDQ")
    assert_eq(inv["first_1000"]["remaining"], 0)


@test("invest: 50/30/20 splits income")
def _():
    from modules.invest import split_income_503020

    s = split_income_503020(3000)
    assert_eq(s["needs"], 1500.0)
    assert_eq(s["wants"], 900.0)
    assert_eq(s["savings"], 600.0)


@test("invest: blocks ETF when buffer too low")
def _():
    from modules.invest import invest_readiness

    metrics = {"total_income": 1000, "total_spent": 950, "net_saved": 50, "savings_rate": 5.0}
    compare = {"savings_gap": -100}
    forecast = {"on_track": True}
    r = invest_readiness(metrics, compare, forecast)
    assert_true(not r["can_invest"])


# ── personality ──────────────────────────────────────────────────────────────


@test("personality: returns type and action plan")
def _():
    from modules.pipeline import run_full_pipeline

    p = run_full_pipeline(SAMPLE_CSV)["personality"]
    assert_in(p["personality_type"], ["Planner", "Spender", "Subscriber", "Balanced"])
    assert_eq(len(p["action_plan"]), 3)
    assert_eq(p["disclaimer"], DISCLAIMER)


# ── budget_setter ────────────────────────────────────────────────────────────


@test("budget_setter: all 7 categories")
def _():
    from modules.pipeline import run_full_pipeline

    b = run_full_pipeline(SAMPLE_CSV)["budgets"]
    cats = {row["category"] for row in b["budgets"]}
    assert_eq(cats, set(CATEGORIES))


# ── history ──────────────────────────────────────────────────────────────────


@test("history: update_streak first upload")
def _():
    from modules.history import update_streak

    s = update_streak(None)
    assert_eq(s["current_streak"], 1)


@test("history: streak increments within 7 days")
def _():
    from modules.history import update_streak

    s = update_streak("2026-06-01", today=date(2026, 6, 5), current_streak=2, best_streak=2)
    assert_eq(s["current_streak"], 3)


@test("history: streak resets after 7 days")
def _():
    from modules.history import update_streak

    s = update_streak("2026-06-01", today=date(2026, 6, 10), current_streak=5, best_streak=5)
    assert_eq(s["current_streak"], 1)


@test("history: build_snapshot has required fields")
def _():
    from modules.pipeline import run_full_pipeline

    snap = run_full_pipeline(SAMPLE_CSV)["snapshot"]
    for key in ("month", "risk_label", "patterns", "disclaimer"):
        assert_in(key, snap)


# ── ai_coach ─────────────────────────────────────────────────────────────────


@test("ai_coach: fallback spend question")
def _():
    from modules.ai_coach import fallback_coach_response, run_pipeline_context

    ctx = run_pipeline_context()["context"]
    text = fallback_coach_response("How much did I spend?", ctx)
    assert_true("1044" in text.replace(",", "") or "1,044" in text)
    # The disclaimer is its own field and its own line in the widget. Welding
    # it onto the end of the answer made every reply a run-on.
    assert_true(DISCLAIMER not in text,
                f"disclaimer leaked into the answer: {text!r}")


@test("ai_coach: coach_chat returns the disclaimer as its own field")
def _():
    from modules.ai_coach import coach_chat, run_pipeline_context

    ctx = run_pipeline_context()["context"]
    r = coach_chat("What's my biggest category?", ctx)
    assert_in("disclaimer", r)
    assert_eq(r["disclaimer"], DISCLAIMER)
    assert_in(r["source"], ["openai", "fallback"])
    # ...and NOT glued onto the reply, on either the model or fallback path.
    assert_true(DISCLAIMER not in r["text"],
                f"disclaimer leaked into the reply: {r['text']!r}")


@test("ai_coach: explain_etf blocked when cannot invest")
def _():
    from modules.ai_coach import explain_etf_nudge

    inv = {"readiness": {"can_invest": False, "reason": "Build buffer first."}}
    r = explain_etf_nudge(inv, {})
    assert_eq(r["source"], "fallback")
    assert_in(DISCLAIMER, r["text"])


# ── pipeline ─────────────────────────────────────────────────────────────────


@test("pipeline: full result shape")
def _():
    from modules.pipeline import run_full_pipeline

    r = run_full_pipeline(SAMPLE_CSV, goal_amount=8000, age=30)
    keys = {
        "metrics",
        "analysis",
        "bills",
        "anomalies",
        "averages",
        "forecast",
        "spend_forecast",
        "budgets",
        "invest",
        "personality",
        "snapshot",
        "context",
        "transactions",
        "goal_recommendation",
        "goal_used",
        "period",
        "all_transactions",
        "llm_categories",
        "pending_questions",
        "disclaimer",
    }
    assert_eq(set(r.keys()), keys)
    assert_eq(len(r["transactions"]), 20)
    assert_eq(len(r["all_transactions"]), 20)
    assert_in("merchant", r["transactions"][0])
    assert_eq(r["forecast"]["target_amount"], 8000)
    assert_eq(r["invest"]["etf"]["recommended"], "VGS")  # age 30
    assert_eq(r["goal_used"]["amount"], 8000)


@test("pipeline: analyze_stored re-slices full history by period")
def _():
    from modules.pipeline import analyze_stored, run_full_pipeline

    r = run_full_pipeline(SAMPLE_CSV)
    full = r["all_transactions"]
    monthly = analyze_stored(full, period="monthly")
    all_time = analyze_stored(full, period="all")
    daily = analyze_stored(full, period="daily")

    assert_eq(monthly["period"]["selected"], "monthly")
    assert_eq(all_time["period"]["selected"], "all")
    # A single day can't have more spend than the whole history.
    assert daily["metrics"]["total_spent"] <= all_time["metrics"]["total_spent"]


@test("pipeline: overrides reclassify transactions and move the numbers")
def _():
    from modules.pipeline import analyze_stored, run_full_pipeline

    full = run_full_pipeline(SAMPLE_CSV)["all_transactions"]
    # Reclassifying an income merchant as an expense must lower income (income
    # rows are positive), proving the user's override flows through every metric.
    merchant = next(t["merchant"] for t in full if t["flow"] == "income")
    base = analyze_stored(full, period="all")
    changed = analyze_stored(
        full, period="all", overrides=[{"match": merchant, "flow": "expense"}]
    )
    assert changed["metrics"]["total_income"] < base["metrics"]["total_income"]


@test("pipeline: category override by match retags spending")
def _():
    from modules.pipeline import analyze_stored, run_full_pipeline

    full = run_full_pipeline(SAMPLE_CSV)["all_transactions"]
    # Pick a real expense merchant and force it into a category it isn't in.
    expense = next(t for t in full if t["flow"] == "expense")
    target = "Health" if expense["category"] != "Health" else "Transport"
    changed = analyze_stored(
        full, period="all",
        overrides=[{"match": expense["merchant"], "category": target}],
    )
    moved = [t for t in changed["transactions"] if t["merchant"] == expense["merchant"]]
    assert_true(moved and all(t["category"] == target for t in moved))


@test("pipeline: tx_key override retags exactly one transaction")
def _():
    from modules.pipeline import analyze_stored, run_full_pipeline

    full = run_full_pipeline(SAMPLE_CSV)["all_transactions"]
    base = analyze_stored(full, period="all")["transactions"]
    # Every record carries a stable key the frontend can pin a single edit to.
    assert_true(all("key" in t for t in base))
    victim = next(t for t in base if t["flow"] == "expense")
    target = "Health" if victim["category"] != "Health" else "Transport"

    changed = analyze_stored(
        full, period="all",
        overrides=[{"tx_key": victim["key"], "category": target}],
    )
    before = {t["key"]: t["category"] for t in base}
    after = {t["key"]: t["category"] for t in changed["transactions"]}
    assert_eq(after[victim["key"]], target)
    # Exactly one transaction moved — a single-row edit touches nothing else.
    moved_keys = [k for k in after if after[k] != before.get(k)]
    assert_eq(moved_keys, [victim["key"]])


@test("data_processor: duplicate transactions get distinct tx_keys")
def _():
    import pandas as pd

    from modules.data_processor import key_series

    df = pd.DataFrame([
        {"date": "2026-03-01", "description": "COFFEE CO", "amount": -4.0},
        {"date": "2026-03-01", "description": "COFFEE CO", "amount": -4.0},  # identical
        {"date": "2026-03-02", "description": "COFFEE CO", "amount": -4.0},
    ])
    keys = key_series(df)
    # All three distinct despite two identical rows — editing one won't hit both.
    assert_eq(len(set(keys)), 3)


@test("categoriser: model is trained once and cached")
def _():
    from modules.categoriser import get_model

    assert_true(get_model() is get_model())  # same instance → not retrained


@test("categoriser: active learning applies user corrections to new merchants")
def _():
    import pandas as pd

    from modules.categoriser import categorise_data, examples_from_overrides

    # tx_key-only rules don't become examples; a text+category rule does.
    overrides = [
        {"tx_key": "abc123", "category": "Health"},
        {"match": "ZZQWIDGET", "category": "Transport"},
    ]
    ex = examples_from_overrides(overrides)
    assert_eq(ex, [("ZZQWIDGET", "Transport")])

    # A brand-new merchant containing the learned token gets the user's category
    # via the augmented model (no rule matches this nonsense token).
    df = pd.DataFrame([{
        "date": pd.Timestamp("2026-03-01"), "amount": -25.0,
        "description": "ZZQWIDGET CO", "merchant_clean": "ZZQWIDGET CO",
        "is_transfer": False,
    }])
    out = categorise_data(df, user_examples=ex)
    assert_eq(out.iloc[0]["category"], "Transport")


@test("savings_forecaster: spend forecast projects next month and runway")
def _():
    import pandas as pd

    from modules.savings_forecaster import forecast_spending

    rows = []
    for m in range(3):
        for d in range(4):
            rows.append({
                "date": pd.Timestamp("2026-01-01") + pd.DateOffset(months=m, days=d * 5),
                "amount": -100.0, "flow": "expense",
            })
    out = forecast_spending(pd.DataFrame(rows), {"latest_balance": 1000, "daily_burn_rate": 20})
    assert_true(out["projected_next_month"] > 0)
    assert_eq(out["runway_days"], 50)   # 1000 / 20


@test("llm_categoriser: quiz asks about unsure + big transfers, skips known")
def _():
    import pandas as pd

    from modules.llm_categoriser import build_questions

    rows = [
        {"date": "2026-03-01", "amount": -40.0, "flow": "expense",
         "merchant_clean": "MYSTERY SHOP", "description": "MYSTERY SHOP"},
        {"date": "2026-03-02", "amount": -300.0, "flow": "transfer",
         "merchant_clean": "PAYID JOHN", "description": "PAYID JOHN"},
        {"date": "2026-03-03", "amount": -20.0, "flow": "expense",
         "merchant_clean": "KNOWN CAFE", "description": "KNOWN CAFE"},
    ]
    df = pd.DataFrame(rows)
    llm = {
        "MYSTERY SHOP": {"category": None, "confidence": "low"},
        "KNOWN CAFE": {"category": "Food & Dining", "confidence": "high"},
    }
    qs = build_questions(df, llm, overrides=[])
    merchants = [q["merchant"] for q in qs]
    assert_in("MYSTERY SHOP", merchants)      # unsure expense → asked
    assert_in("PAYID JOHN", merchants)        # big transfer → asked
    assert_true("KNOWN CAFE" not in merchants)  # confident → not asked
    # Biggest money first, and never more than 6.
    assert_eq(merchants[0], "PAYID JOHN")
    assert_true(len(qs) <= 6)
    # A merchant the user already fixed is never asked about.
    qs2 = build_questions(df, llm, overrides=[{"match": "MYSTERY SHOP", "category": "Shopping"}])
    assert_true("MYSTERY SHOP" not in [q["merchant"] for q in qs2])


@test("bank_parser: noise words are stripped whole-word only")
def _():
    from modules.bank_parser import clean_merchant_name

    # " CO" must not be torn out of the middle of a word.
    assert_eq(clean_merchant_name("SQ *THE COFFEE SHOP SYDNEY"), "THE COFFEE SHOP")
    assert_eq(clean_merchant_name("TRANSFER TO XX0642 COMMBANK APP"),
              "TRANSFER TO COMMBANK APP")
    # Real legal suffixes still go.
    assert_eq(clean_merchant_name("JC CHICKEN PTY. LTD RYDE NSWAU"), "JC CHICKEN")
    assert_eq(clean_merchant_name("PAYPAL *SPOTIFY"), "SPOTIFY")


@test("pipeline: stored merchant is the CLEAN name and tx_keys survive a re-slice")
def _():
    from modules.pipeline import analyze_stored, run_full_pipeline

    r = run_full_pipeline(SAMPLE_CSV)
    full = r["all_transactions"]
    res = analyze_stored(full, period="all")
    # Same identity on both sides → single-row edits keep working after upload.
    assert_eq({t["key"] for t in full}, {t["key"] for t in res["transactions"]})
    # And merchant names don't change shape between upload and re-slice.
    assert_eq(
        [m["merchant"] for m in r["averages"]["top_merchants"]],
        [m["merchant"] for m in res["averages"]["top_merchants"]],
    )


@test("llm_categoriser: recurring money IN suggests income, own account does not")
def _():
    import pandas as pd

    from modules.llm_categoriser import build_questions

    rows = []
    for i in range(5):          # a person paying you repeatedly → income
        rows.append({"date": f"2026-03-0{i+1}", "amount": 400.0, "flow": "transfer",
                     "merchant_clean": "FAST TRANSFER FROM SAM", "description": "x"})
    for i in range(5):          # your own banking-app top-ups → still a transfer
        rows.append({"date": f"2026-03-1{i}", "amount": 300.0, "flow": "transfer",
                     "merchant_clean": "TRANSFER FROM COMMBANK APP", "description": "y"})
    rows.append({"date": "2026-03-20", "amount": -900.0, "flow": "transfer",
                 "merchant_clean": "TRANSFER TO A FRIEND", "description": "z"})

    qs = {q["merchant"]: q for q in build_questions(pd.DataFrame(rows), {}, overrides=[])}
    assert_eq(qs["FAST TRANSFER FROM SAM"]["suggested"], "income")
    assert_eq(qs["TRANSFER FROM COMMBANK APP"]["suggested"], "transfer")
    assert_eq(qs["TRANSFER TO A FRIEND"]["suggested"], "transfer")


@test("eval: categoriser and retrieval stay above their quality floors")
def _():
    # The offline half of the evaluation harness runs as a regression guard, so
    # a change that quietly makes the AI worse fails here instead of shipping.
    from eval.run_eval import (
        MIN_CATEGORY_F1,
        MIN_RETRIEVAL_HIT_RATE,
        eval_categoriser,
        eval_retrieval,
    )

    cat = eval_categoriser()
    assert_true(
        cat["f1_macro"] >= MIN_CATEGORY_F1,
        f"categoriser macro F1 {cat['f1_macro']} below floor {MIN_CATEGORY_F1}",
    )
    ret = eval_retrieval()
    assert_true(
        ret["hit_rate"] >= MIN_RETRIEVAL_HIT_RATE,
        f"retrieval hit-rate {ret['hit_rate']} below floor {MIN_RETRIEVAL_HIT_RATE}",
    )


@test("anomaly: flags an outlier charge for its category")
def _():
    import pandas as pd

    from modules.anomaly import detect_anomalies

    rows = [{
        "date": pd.Timestamp("2026-03-01") + pd.Timedelta(days=i),
        "amount": -10.0, "flow": "expense",
        "category": "Food & Dining", "merchant_clean": "CAFE",
    } for i in range(8)]
    rows.append({
        "date": pd.Timestamp("2026-03-20"), "amount": -200.0, "flow": "expense",
        "category": "Food & Dining", "merchant_clean": "FANCY RESTAURANT",
    })
    out = detect_anomalies(pd.DataFrame(rows))
    assert_true(out and out[0]["merchant"] == "FANCY RESTAURANT")
    assert_eq(out[0]["amount"], 200.0)


@test("history: same-day re-upload does not inflate the streak")
def _():
    from modules.history import update_streak

    same = update_streak("2026-03-10", today="2026-03-10", current_streak=3, best_streak=5)
    assert_eq(same["current_streak"], 3)      # unchanged same day
    nxt = update_streak("2026-03-10", today="2026-03-11", current_streak=3, best_streak=5)
    assert_eq(nxt["current_streak"], 4)       # next day continues
    gap = update_streak("2026-03-10", today="2026-04-10", current_streak=3, best_streak=5)
    assert_eq(gap["current_streak"], 1)       # long gap resets


@test("rag: retrieves the relevant knowledge-base snippet")
def _():
    from modules.rag import search

    hits = search("what is an ETF and diversification")
    assert_true(hits and hits[0]["id"] == "etf")


@test("ai_coach: fallback answers a concept question from the knowledge base")
def _():
    from modules.ai_coach import fallback_coach_response

    out = fallback_coach_response("explain superannuation to me", {"patterns": []})
    assert_in("super", out.lower())


@test("ai_coach: generate_insight returns text plus a separate disclaimer")
def _():
    from modules.ai_coach import generate_insight

    ctx = {
        "spent": 1200, "saved": 300, "savings_rate": 20.0,
        "top_categories": [{"category": "Food & Dining", "amount": 500, "pct": 40}],
    }
    out = generate_insight(ctx)
    assert_true(out["text"])
    assert_in("not financial advice", out["disclaimer"].lower())
    # The disclaimer is its own field and its own line on every page — gluing it
    # onto the sentence made a run-on AND duplicated what was already on screen.
    assert_true("not financial advice" not in out["text"].lower(),
                f"disclaimer leaked into the insight text: {out['text']!r}")


@test("invest: menu lists crypto and more than just ETFs")
def _():
    from modules.invest import investment_menu

    types = {row["type"] for row in investment_menu(can_invest=True, age=25)}
    assert_in("Crypto", types)
    assert_in("ETFs", types)
    assert len(types) >= 3


@test("savings_forecaster: forecast exposes a monthly trend")
def _():
    from modules.pipeline import run_full_pipeline

    f = run_full_pipeline(SAMPLE_CSV, goal_amount=5000, goal_date="2026-12-31")[
        "forecast"
    ]
    assert_in("monthly_rate", f)
    assert_in("months_remaining", f)
    assert f["months_remaining"] >= 0


@test("pipeline: recommends a goal when none given")
def _():
    from modules.pipeline import run_full_pipeline

    r = run_full_pipeline(SAMPLE_CSV)
    rec = r["goal_recommendation"]
    assert rec["amount"] > 0, "recommended amount should be positive"
    assert rec["target_date"], "should suggest a target date"
    # with no goal passed, the pipeline falls back to the recommendation
    assert_eq(r["goal_used"]["amount"], rec["amount"])


@test("savings_forecaster: recommend_goal handles overspending")
def _():
    from modules.savings_forecaster import recommend_goal

    metrics = {
        "total_income": 1000,
        "total_spent": 1500,
        "net_saved": -500,
        "savings_rate": None,
        "date_range": {"start": "2026-01-01", "end": "2026-01-31", "days": 30},
    }
    rec = recommend_goal(metrics)
    # Overspending → a starter safety buffer sized to ~one month of spending
    # ($1,500/mo here), never below the $500 floor.
    assert_eq(rec["amount"], 1500)
    assert_true(rec["amount"] >= 500)
    assert_in("buffer", rec["rationale"])


# ── db (unit, no network) ────────────────────────────────────────────────────


@test("db: is_configured reflects env")
def _():
    from modules import db

    has = bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_ANON_KEY"))
    assert_eq(db.is_configured(), has)


# ── API ──────────────────────────────────────────────────────────────────────


@test("API: GET / health")
def _():
    from fastapi.testclient import TestClient
    from main import app

    r = TestClient(app).get("/")
    assert_eq(r.status_code, 200)
    assert_eq(r.json()["status"], "ok")


@test("API: POST /analyze success without auth")
def _():
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    with open(SAMPLE_CSV, "rb") as f:
        r = client.post(
            "/analyze",
            files={"file": ("sample.csv", f, "text/csv")},
            data={"goal_amount": 5000, "goal_date": "2026-06-30", "age": 22},
        )
    assert_eq(r.status_code, 200)
    d = r.json()
    assert_eq(d["persisted"], False)
    assert_eq(d["analysis"]["risk_label"], "low")


@test("API: POST /analyze rejects non-csv")
def _():
    from fastapi.testclient import TestClient
    from main import app

    r = TestClient(app).post(
        "/analyze",
        files={"file": ("bad.txt", b"hello", "text/plain")},
    )
    assert_eq(r.status_code, 400)


@test("API: POST /analyze bad bearer format returns 401")
def _():
    from fastapi.testclient import TestClient
    from main import app
    from modules import db

    if not db.is_configured():
        raise SkipTest("Supabase not configured")

    client = TestClient(app)
    with open(SAMPLE_CSV, "rb") as f:
        r = client.post(
            "/analyze",
            files={"file": ("sample.csv", f, "text/csv")},
            headers={"Authorization": "NotBearer xyz"},
        )
    assert_eq(r.status_code, 401)


@test("API: protected routes reject unauthenticated requests")
def _():
    from fastapi.testclient import TestClient
    from main import app
    from modules import db

    expected = 401 if db.is_configured() else 503
    client = TestClient(app)
    for method, path, kwargs in [
        ("get", "/dashboard", {}),
        ("get", "/invest", {}),
        ("get", "/coach/history", {}),
        ("post", "/coach", {"json": {"message": "hi"}}),
        ("post", "/spend-check", {"json": {"amount": 50}}),
    ]:
        r = getattr(client, method)(path, **kwargs)
        assert_eq(r.status_code, expected, f"{method} {path}")


def _mock_auth_client():
    from fastapi.testclient import TestClient
    from api.deps import AuthUser, get_current_user
    from main import app

    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        user_id="00000000-0000-0000-0000-000000000001",
        token="test-token",
    )
    return TestClient(app), app


@test("API: coach rejects empty message")
def _():
    client, app = _mock_auth_client()
    try:
        r = client.post("/coach", json={"message": ""})
        assert_eq(r.status_code, 422)
    finally:
        app.dependency_overrides.clear()


@test("API: spend-check rejects invalid amount")
def _():
    client, app = _mock_auth_client()
    try:
        r = client.post("/spend-check", json={"amount": 0})
        assert_eq(r.status_code, 422)

        r = client.post("/spend-check", json={"amount": -10})
        assert_eq(r.status_code, 422)
    finally:
        app.dependency_overrides.clear()


@test("API: spend-check rejects days_ahead out of range")
def _():
    client, app = _mock_auth_client()
    try:
        r = client.post("/spend-check", json={"amount": 50, "days_ahead": 0})
        assert_eq(r.status_code, 422)

        r = client.post("/spend-check", json={"amount": 50, "days_ahead": 91})
        assert_eq(r.status_code, 422)
    finally:
        app.dependency_overrides.clear()


@test("API: CORS headers on response")
def _():
    from fastapi.testclient import TestClient
    from main import app

    r = TestClient(app).options(
        "/",
        headers={
            "Origin": "http://localhost:5500",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert_true(r.status_code in (200, 204))


@test("API: invalid token returns 401 on protected route")
def _():
    from fastapi.testclient import TestClient
    from main import app
    from modules import db

    if not db.is_configured():
        raise SkipTest("Supabase not configured")

    r = TestClient(app).get(
        "/dashboard",
        headers={"Authorization": "Bearer invalid-token-xyz"},
    )
    assert_eq(r.status_code, 401)


# ── edge cases ───────────────────────────────────────────────────────────────


@test("edge: pipeline handles income-only CSV")
def _():
    from modules.pipeline import run_full_pipeline

    csv = "Date,Amount,Description\n01/01/2026,1000.00,SALARY\n02/01/2026,500.00,BONUS\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write(csv)
        path = f.name
    try:
        r = run_full_pipeline(path)
        assert_eq(r["metrics"]["total_income"], 1500.0)
        assert_eq(r["metrics"]["total_spent"], 0.0)
        assert_eq(r["analysis"]["risk_label"], "low")
    finally:
        os.unlink(path)


@test("edge: training CSV readable")
def _():
    assert_true(TRAINING_CSV.exists())
    assert_true(len(TRAINING_CSV.read_text().strip().splitlines()) >= 31)


@test("anomaly: blowouts are caught in small categories (no z-score ceiling)")
def _():
    import pandas as pd
    from modules.anomaly import detect_anomalies

    # A plain (x-mean)/std score is capped at (n-1)/sqrt(n), so with a 2.5
    # threshold nothing in a category of <=8 rows could EVER be flagged.
    rows = [{"date": f"2026-01-{i + 1:02d}", "amount": -25.0, "flow": "expense",
             "category": "Food & Dining", "description": "CAFE"} for i in range(4)]
    rows.append({"date": "2026-01-28", "amount": -5000.0, "flow": "expense",
                 "category": "Food & Dining", "description": "BLOWOUT"})
    found = detect_anomalies(pd.DataFrame(rows))
    assert_eq(len(found), 1, "a $5000 charge among $25 meals must be flagged")
    assert_eq(found[0]["merchant"], "BLOWOUT")


@test("anomaly: steady spending produces no false positives")
def _():
    import pandas as pd
    from modules.anomaly import detect_anomalies

    rows = [{"date": f"2026-01-{i + 1:02d}", "amount": -(20 + (i % 11)),
             "flow": "expense", "category": "Food & Dining",
             "description": "CAFE"} for i in range(28)]
    assert_eq(detect_anomalies(pd.DataFrame(rows)), [])


@test("anomaly: identical amounts (MAD=0) still detect a spike")
def _():
    import pandas as pd
    from modules.anomaly import detect_anomalies

    rows = [{"date": f"2026-01-{i + 1:02d}", "amount": -25.0, "flow": "expense",
             "category": "Food & Dining", "description": "CAFE"} for i in range(10)]
    rows.append({"date": "2026-01-28", "amount": -900.0, "flow": "expense",
                 "category": "Food & Dining", "description": "SPIKE"})
    found = detect_anomalies(pd.DataFrame(rows))
    assert_eq(len(found), 1, "MAD=0 must fall back, not skip the category")
    assert_eq(found[0]["merchant"], "SPIKE")


@test("anomaly: an unusually CHEAP charge is never flagged")
def _():
    import pandas as pd
    from modules.anomaly import detect_anomalies

    rows = [{"date": f"2026-01-{i + 1:02d}", "amount": -500.0, "flow": "expense",
             "category": "Rent", "description": "RENT"} for i in range(10)]
    rows.append({"date": "2026-01-28", "amount": -21.0, "flow": "expense",
                 "category": "Rent", "description": "TINY"})
    assert_eq(detect_anomalies(pd.DataFrame(rows)), [])


@test("categoriser: keyword rules match whole words, not substrings")
def _():
    from modules.categoriser import rule_category

    # Plain substring matching made these confidently wrong: "BOND" fired
    # inside "BONDI", "SUPER" inside "SUPERCHEAP", "MARKET" inside
    # "MARKETPLACE" — and the rules layer used to outrank everything.
    assert_eq(rule_category("COTTON ON BONDI JUNCTION"), "Shopping")
    assert_true(rule_category("PETSTOCK MARKETPLACE") != "Groceries")
    # Car servicing is Transport (a running cost), matching what the model is
    # told — "SUPER" must not drag it into Groceries.
    assert_eq(rule_category("SUPERCHEAP AUTO ALEXANDRIA"), "Transport")
    # Genuine whole-word matches still work.
    assert_eq(rule_category("WOOLWORTHS METRO EASTWOOD"), "Groceries")
    assert_eq(rule_category("UBER EATS SYDNEY"), "Food & Dining")
    assert_eq(rule_category("NETFLIX.COM"), "Subscriptions")


def _expense_df(names):
    import pandas as pd
    return pd.DataFrame({
        "description": names,
        "merchant_clean": names,
        "amount": [-40.0] * len(names),
        "flow": ["expense"] * len(names),
    })


@test("categoriser: a confident model answer beats a conflicting keyword rule")
def _():
    import modules.categoriser as cat

    # A keyword rule matches this merchant, but the model is confident it is
    # something else. The model must win — that ordering is the whole point,
    # because a keyword list cannot know what a merchant actually sells.
    assert_eq(cat.rule_category("COTTON ON BONDI"), "Shopping")

    df = cat.categorise_data(
        _expense_df(["COTTON ON BONDI"]),
        llm_cache={"COTTON ON BONDI": {"category": "Other", "confidence": "high"}},
    )
    assert_eq(df["category"].iloc[0], "Other")


@test("categoriser: a low-confidence model answer falls through to the rules")
def _():
    import modules.categoriser as cat

    # An unsure guess must never silently set a category — it falls back and
    # becomes a quiz question instead.
    df = cat.categorise_data(
        _expense_df(["UBER EATS SYDNEY"]),
        llm_cache={"UBER EATS SYDNEY":
                   {"category": "Shopping", "confidence": "low"}},
    )
    assert_eq(df["category"].iloc[0], "Food & Dining")


@test("categoriser: works with no model available (offline path)")
def _():
    import modules.categoriser as cat

    df = cat.categorise_data(_expense_df(["WOOLWORTHS METRO", "NETFLIX.COM"]))
    assert_eq(list(df["category"]), ["Groceries", "Subscriptions"])


@test("categoriser: merchant context carries amount and frequency signal")
def _():
    from modules.categoriser import _merchant_context

    df = _expense_df(["CALTEX RYDE", "CALTEX RYDE", "KMART"])
    df.loc[0, "amount"] = -70.0
    df.loc[1, "amount"] = -80.0
    ctx = _merchant_context(df, df["flow"] == "expense", "merchant_clean",
                            ["CALTEX RYDE"])
    assert_eq(ctx["CALTEX RYDE"]["count"], 2)
    assert_eq(ctx["CALTEX RYDE"]["total"], 150.0)
    assert_eq(ctx["CALTEX RYDE"]["avg"], 75.0)
    assert_true("KMART" not in ctx, "only requested merchants are described")


@test("categoriser: Naive Bayes does not overrule the model's 'I don't know'")
def _():
    import modules.categoriser as cat
    import pandas as pd

    # A merchant no keyword rule can match, that the model also could not
    # place. A ~30-example NB has strictly less information than the thing
    # that just abstained, yet it never abstains itself — that is how a $663
    # ENGIE energy bill ended up filed under Food & Dining.
    df = pd.DataFrame({
        "description": ["ZZQ UNKNOWN VENDOR 8891"],
        "merchant_clean": ["ZZQ UNKNOWN VENDOR 8891"],
        "amount": [-663.73],
        "flow": ["expense"],
    })
    assert_true(cat.rule_category("ZZQ UNKNOWN VENDOR 8891") is None,
                "fixture must not be rescued by a keyword rule")
    out = cat.categorise_data(df, llm_cache={
        "ZZQ UNKNOWN VENDOR 8891": {"category": None, "confidence": "low"}})
    assert_eq(out["category"].iloc[0], "Other",
              "an abstention must stay honest, not become a guess")


@test("anomaly: judged against the merchant's own history, not the category")
def _():
    import pandas as pd
    from modules.anomaly import detect_anomalies

    # Groceries mixes $5 convenience runs with $300 Costco shops, so a
    # category median of ~$8 flagged a BELOW-average Costco trip as unusual.
    rows = [{"date": f"2026-0{1 + i // 28}-{i % 28 + 1:02d}", "amount": -6.0,
             "flow": "expense", "category": "Groceries",
             "description": "IGA UNSW"} for i in range(20)]
    rows += [{"date": f"2026-03-{i + 1:02d}", "amount": -310.0, "flow": "expense",
              "category": "Groceries", "description": "COSTCO"} for i in range(5)]
    # A normal-for-Costco shop, far above the category median but ordinary here.
    rows.append({"date": "2026-04-01", "amount": -300.0, "flow": "expense",
                 "category": "Groceries", "description": "COSTCO"})
    found = detect_anomalies(pd.DataFrame(rows))
    assert_true(not any(a["merchant"] == "COSTCO" for a in found),
                f"a typical Costco shop must not be flagged: {found}")


@test("anomaly: reports what 'typical' was measured against")
def _():
    import pandas as pd
    from modules.anomaly import detect_anomalies

    rows = [{"date": f"2026-01-{i + 1:02d}", "amount": -20.0, "flow": "expense",
             "category": "Food & Dining", "description": "CAFE"} for i in range(8)]
    rows.append({"date": "2026-01-20", "amount": -900.0, "flow": "expense",
                 "category": "Food & Dining", "description": "CAFE"})
    found = detect_anomalies(pd.DataFrame(rows))
    assert_eq(len(found), 1)
    assert_eq(found[0]["basis"], "merchant")
    assert_eq(found[0]["compared_to"], "CAFE")


@test("period: an empty window returns a result instead of raising")
def _():
    import pandas as pd
    from modules.period import resolve_periods

    # min()/max() on an empty frame give NaT, and .normalize() on NaT raised
    # AttributeError — turning a legitimately empty result into a 500.
    empty = pd.DataFrame({"date": pd.to_datetime([]), "amount": []})
    for period in ("daily", "weekly", "monthly", "all", "custom"):
        out = resolve_periods(empty, period=period)
        assert_eq(out["label"], "no transactions")
        assert_eq(out["prior"], (None, None))


@test("goal: presets say when the user cannot fund them yet")
def _():
    from modules.savings_forecaster import recommend_goal

    # Not saving anything: every preset must be flagged, because the money has
    # to be freed up first. The dashboard renders this flag.
    broke = recommend_goal({"total_income": 3000.0, "total_spent": 3200.0,
                            "net_saved": -200.0}, monthly_saved=-200.0)
    assert_true(broke["presets"], "should still offer a starter buffer")
    assert_true(all(p["achievable"] is False for p in broke["presets"]),
                "presets must not look affordable when nothing is being saved")

    saving = recommend_goal({"total_income": 3000.0, "total_spent": 2000.0,
                             "net_saved": 1000.0}, monthly_saved=1000.0)
    assert_true(all(p["achievable"] is True for p in saving["presets"]))


@test("css: an unfundable goal preset is visually distinct")
def _():
    css = (ROOT / "frontend" / "styles.css").read_text()
    html = (ROOT / "frontend" / "dashboard.html").read_text()
    assert_in(".goal-preset.stretch", css)
    # The flag existed in the API but was dropped by the renderer.
    assert_in("achievable === false", html)


@test("css: dashboard columns finish flush, with no dead space")
def _():
    import re

    css = (ROOT / "frontend" / "styles.css").read_text()
    # Strip comments first — prose explaining the old value is not a rule.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # align-items:start let a shorter column end where its content ran out,
    # leaving ~280px of nothing beside the taller one.
    starts = re.findall(r"\.dash-two-col\s*\{[^}]*align-items:\s*start", css)
    assert_eq(starts, [], "dash-two-col must stretch, not start")
    assert_true(re.search(r"\.dash-two-col\s*\{[^}]*align-items:\s*stretch", css),
                "dash-two-col should stretch its columns")
    # The last card soaks up the leftover height so both columns end together.
    assert_in(".dash-col > .card:last-child", css)


@test("css: one gutter token, so dashboard spacing cannot drift")
def _():
    css = (ROOT / "frontend" / "styles.css").read_text()
    # There were two competing spacing systems: an earlier .dash-two-col block
    # and a later one that silently overrode it. A single token keeps the
    # rows in step.
    assert_eq(css.count("--dash-gap"), 0, "duplicate gutter token reintroduced")
    assert_true(css.count("--gap: 1.25rem") == 1, "--gap should be defined once")
    for row in (".cards-row", ".dash-chart-row", ".dash-two-col"):
        assert_in(row, css)


@test("css: a chat bubble takes the width its text needs")
def _():
    import re

    css = (ROOT / "frontend" / "styles.css").read_text()
    block = css[css.index(".cw-msg.user {"):]
    block = block[:block.index("}")]

    # A PERCENTAGE max-width here resolves against .cw-turn, which is
    # shrink-to-fit and therefore sized BY this element. Browsers break that
    # circularity by collapsing the bubble: "hi finiooo" rendered 86px wide and
    # wrapped onto two lines inside a 736px column. Cap in ch instead.
    assert_true("%" not in re.sub(r"/\*.*?\*/", "", block, flags=re.S),
                f"no percentage cap on the shrink-to-fit bubble: {block!r}")
    assert_in("max-width: 46ch", block)
    # The responsive cap belongs on the turn, which has a definite parent.
    turn = css[css.index(".cw-turn.user {"):]
    assert_in("max-width: 88%", turn[:turn.index("}")])


@test("coach: the composer is a real multi-line box, not a one-line input")
def _():
    js = (ROOT / "frontend" / "coach-widget.js").read_text()
    css = (ROOT / "frontend" / "styles.css").read_text()

    assert_in('<textarea id="cw-input"', js)
    assert_true('<input id="cw-input"' not in js, "the one-line input should be gone")

    # A textarea does not submit on Enter, so that has to be wired explicitly —
    # otherwise sending a message would break entirely.
    assert_in("requestSubmit()", js)
    assert_in("e.shiftKey", js)          # Shift+Enter must still make a newline

    # It grows with the text but stops, so it can never swallow the thread.
    assert_in("INPUT_MAX_PX", js)
    assert_in("autoGrow", js)
    assert_in(".cw-form textarea", css)
    assert_in("resize: none", css)


@test("css: the chat has a capped, centred reading column")
def _():
    css = (ROOT / "frontend" / "styles.css").read_text()
    # In split mode the panel can be 1400px+. A line of text running the full
    # width is hard to read — the eye loses its place on the return sweep.
    # Growing the side padding centres the column while keeping the scrollbar
    # and hover backgrounds full-bleed.
    assert_in("--cw-read", css)
    for block in (".cw-messages", ".cw-form"):
        i = css.index(block + " {")
        assert_in("var(--cw-read", css[i:i + 600],
                  f"{block} must share the capped reading column: ")


@test("coach: split mode loads the conversation, not a blank panel")
def _():
    js = (ROOT / "frontend" / "coach-widget.js").read_text()
    # setOpen() was the ONLY caller of loadHistory(), so restoring in split
    # mode (which goes through enterSplit) reopened the panel empty — the
    # thread was on the server, nothing ever fetched it.
    assert_in("function hydrate()", js)
    split = js[js.index("function enterSplit("):js.index("function enterSplit(") + 1600]
    assert_in("hydrate()", split,
              "enterSplit must load the conversation, or the panel opens blank: ")


@test("coach: the small panel is draggable and cannot leave the screen")
def _():
    js = (ROOT / "frontend" / "coach-widget.js").read_text()
    css = (ROOT / "frontend" / "styles.css").read_text()

    assert_in("makeDraggable", js)
    assert_in("PANEL_POS_KEY", js)          # position survives navigation
    assert_in("savePanelPos", js)
    # Clamped on both axes so it can never be dragged out of reach.
    drag = js[js.index("function makeDraggable()"):]
    assert_in("clamp(baseX + dx", drag)
    assert_in("clamp(baseY + dy", drag)
    # Header controls must not start a drag.
    assert_in("e.target.closest('button, a, input, select')", drag)
    # Only the floating panel gets a grab cursor — split/full are docked.
    assert_in("#coach-panel.open:not(.split):not(.full) .cw-head", css)


# ── Layout invariants (split mode overflow) ─────────────────────────────────

def _css():
    return (ROOT / "frontend" / "styles.css").read_text()


@test("css: main is a query container so layout follows the content column")
def _():
    css = _css()
    # Opening the coach shrinks <main> without changing the window, so any
    # breakpoint that keys off the VIEWPORT keeps the desktop layout and the
    # contents spill out of their cards. Container queries are what make the
    # components respond to the width they are actually given.
    assert_in("container-type: inline-size", css)
    assert_in("container-name: page", css)
    assert_true(css.count("@container") >= 4,
                "layout breakpoints should be container queries, not only media queries")


@test("css: components that overflowed in split mode collapse by container width")
def _():
    import re

    css = _css()
    blocks = re.findall(r"@container page \([^)]*\)\s*\{(.*?)\n\}", css, re.S)
    body = "\n".join(blocks)
    # Each of these was visibly clipped when the coach was open.
    for selector in (".cards-row", ".goal-presets", ".dash-two-col",
                     ".dash-chart-row", ".tx-row"):
        assert_in(selector, body,
                  f"{selector} must collapse on CONTAINER width, not viewport: ")


@test("css: grid children can shrink instead of pushing out of their card")
def _():
    css = _css()
    # Grid/flex children default to min-width:auto and refuse to shrink below
    # their content — which is how long money figures escaped their boxes.
    assert_in("min-width: 0", css)
    assert_in("overflow-wrap: anywhere", css)


@test("css: braces stay balanced")
def _():
    css = _css()
    assert_eq(css.count("{"), css.count("}"), "unbalanced braces in styles.css")


@test("analytics: the chart series flags part-months instead of hiding them")
def _():
    from modules.analytics import compute_averages
    import pandas as pd

    # Statement runs 1 Mar to 12 Apr: March is whole, April is not. Plotting
    # April's smaller total beside March reads as a collapse in spending when
    # it is only 12 days of data.
    dates = pd.to_datetime(["2026-03-01", "2026-03-20", "2026-03-31",
                            "2026-04-01", "2026-04-12"])
    df = pd.DataFrame({
        "date": dates,
        "amount": [-500.0, -400.0, -100.0, -80.0, -60.0],
        "description": ["A", "B", "C", "D", "E"],
        "flow": ["expense"] * 5,
        "category": ["Groceries"] * 5,
    })
    series = compute_averages(df)["spend_series"]
    by_month = {s["month"]: s for s in series}
    assert_eq(by_month["2026-03"]["partial"], False, "March is a whole month")
    assert_eq(by_month["2026-04"]["partial"], True, "April is only 12 days")
    # income is exposed so the tooltip can show what "kept" is made of.
    assert_true("income" in by_month["2026-03"])


# ── Flow contradictions (found on real data) ────────────────────────────────

def _flow_df(rows):
    import pandas as pd
    return pd.DataFrame(rows)


@test("flow: money going OUT can never be counted as income")
def _():
    from modules.data_processor import reconcile_flow_contradictions

    # A -$4,800 row labelled income silently SUBTRACTED $4,800 from reported
    # income, because compute_metrics sums the income rows.
    df, mask = reconcile_flow_contradictions(_flow_df([
        {"amount": -4800.0, "flow": "income", "category": "Transfers"},
        {"amount": 3200.0, "flow": "income", "category": None},
    ]))
    assert_eq(df["flow"].iloc[0], "transfer", "outgoing money must not be income")
    assert_eq(df["flow"].iloc[1], "income", "real income is untouched")
    assert_eq(int(mask.sum()), 1)


@test("flow: a Transfers row is never counted as spending")
def _():
    from modules.data_processor import reconcile_flow_contradictions

    # This made "Transfers" the single biggest spending category on a real
    # statement ($14,071), which is exactly what config says must never happen.
    df, _ = reconcile_flow_contradictions(_flow_df([
        {"amount": -1500.0, "flow": "expense", "category": "Transfers"},
        {"amount": -42.0, "flow": "expense", "category": "Groceries"},
    ]))
    assert_eq(df["flow"].iloc[0], "transfer")
    assert_eq(df["flow"].iloc[1], "expense", "real spending is untouched")


@test("flow: a refund stays an expense offset, not a transfer")
def _():
    from modules.data_processor import reconcile_flow_contradictions

    # expense + POSITIVE amount is legitimate (a refund reducing spend), so the
    # reconciliation must be asymmetric and leave it alone.
    df, mask = reconcile_flow_contradictions(_flow_df([
        {"amount": 54.99, "flow": "expense", "category": "Shopping"},
    ]))
    assert_eq(df["flow"].iloc[0], "expense")
    assert_eq(int(mask.sum()), 0)


@test("flow: transfers stay out of total_spent and the breakdown")
def _():
    from modules.data_processor import add_flags, compute_metrics
    import pandas as pd

    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"]),
        "amount": [-9211.0, -100.0, 3000.0],
        "description": ["TRANSFER TO LAITH HUSSEIN", "WOOLWORTHS RYDE", "SALARY"],
    })
    # The user marked the transfer row as spending; the category still says it
    # is a transfer, and the category is authoritative.
    flagged = add_flags(df, overrides=[
        {"match": "TRANSFER TO LAITH HUSSEIN", "flow": "expense"}])
    flagged["category"] = ["Transfers", "Groceries", None]
    from modules.data_processor import reconcile_flow_contradictions
    flagged, _ = reconcile_flow_contradictions(flagged)

    m = compute_metrics(flagged)
    assert_eq(m["total_spent"], 100.0, "the $9,211 transfer must not be spending")
    assert_eq(m["total_income"], 3000.0)


@test("flow: contradictory rules are reported so the user can fix them")
def _():
    from modules.data_processor import contradictory_flow_rules
    import pandas as pd

    df = pd.DataFrame({
        "amount": [-4800.0, -1500.0, -42.0],
        "description": ["TRANSFER TO LAITH", "TRANSFER TO LAITH", "WOOLWORTHS"],
    })
    warned = contradictory_flow_rules(df, [
        {"match": "TRANSFER TO LAITH", "flow": "income"},
        {"match": "WOOLWORTHS", "flow": "expense"},
    ])
    assert_eq(len(warned), 1)
    assert_eq(warned[0]["count"], 2)
    assert_eq(warned[0]["total"], 6300.0)


@test("spend-check: the reset button clears every field it claims to")
def _():
    import re

    html = (ROOT / "frontend" / "spend-check.html").read_text()
    start = html.index("getElementById('check-another')")
    handler = html[start:html.index("scrollIntoView", start)]

    # A typo'd id fails silently at runtime and node --check cannot see it —
    # the same class of bug as calling an un-imported helper.
    ids = set(re.findall(r"getElementById\('([^']+)'\)", handler))
    for element_id in ids - {"check-another"}:
        assert_in(f'id="{element_id}"', html,
                  f"reset handler touches #{element_id} which is not in the page: ")

    # It has to actually reset the inputs, not just hide the verdict.
    for field in ("merchant", "amount", "days-ahead"):
        assert_in(f"getElementById('{field}').value", handler,
                  f"reset must clear the {field} field: ")
    assert_in('id="check-another"', html, "the button must exist in the verdict card: ")


@test("spend-check: the verdict still works when history is unavailable")
def _():
    from fastapi.testclient import TestClient
    import main as app_main

    # The 003 migration may not have been run. History is a nice-to-have; the
    # answer the user asked for must never depend on it.
    class NoTable:
        def table(self, name):
            if name == "spend_checks":
                raise Exception('relation "public.spend_checks" does not exist')
            class Q:
                data = []
                def select(s, *a, **k): return s
                def eq(s, *a, **k): return s
                def order(s, *a, **k): return s
                def limit(s, *a, **k): return s
                def execute(s): return s
            return Q()

    saved = (app_main.db.get_client, app_main.db.load_dashboard)
    app_main.db.get_client = lambda *a, **k: NoTable()
    app_main.db.load_dashboard = lambda c, u: {
        "month": "2026-06", "goal": {},
        "metrics": {"latest_balance": 3000.0, "net_saved": 500.0,
                    "daily_burn_rate": 40.0}}

    class U:
        user_id = "u1"
        token = "t"

    app_main.app.dependency_overrides[app_main.get_current_user] = lambda: U()
    try:
        client = TestClient(app_main.app)
        r = client.post("/spend-check",
                        json={"merchant": "JB Hi-Fi", "amount": 900, "days_ahead": 30})
        assert_eq(r.status_code, 200)
        assert_in(r.json()["verdict"], ["green", "yellow", "red"])
        h = client.get("/spend-check/history")
        assert_eq(h.status_code, 200)
        assert_eq(h.json()["checks"], [])
    finally:
        app_main.db.get_client, app_main.db.load_dashboard = saved
        app_main.app.dependency_overrides.clear()


@test("spend-check: each check is logged, newest first")
def _():
    from modules import db

    saved = []

    class Rec:
        def table(self, name):
            class Q:
                def insert(s, row): saved.append(row); return s
                def execute(s): return s
            return Q()

    db.save_spend_check(Rec(), "u1", {
        "merchant": "JB Hi-Fi", "purchase_amount": 900.0, "days_ahead": 30,
        "verdict": "red", "projected_balance": -120.0,
    })
    assert_eq(len(saved), 1)
    assert_eq(saved[0]["merchant"], "JB Hi-Fi")
    assert_eq(saved[0]["verdict"], "red")
    assert_eq(saved[0]["user_id"], "u1")
    assert_true("checked_at" in saved[0])


# ── Re-classify (propose, then the user confirms) ───────────────────────────

def _reclassify_client(transactions, overrides, proposal):
    """TestClient for POST /reclassify with the DB and the model stubbed."""
    from fastapi.testclient import TestClient
    import main as app_main

    saved = {k: getattr(app_main.db, k, None) for k in (
        "get_client", "load_dashboard", "get_all_transactions", "get_overrides",
        "get_custom_categories", "get_budget_targets", "get_user_profile",
        "get_streak", "get_goal")}
    saved_llm = app_main.categorise_merchants

    app_main.db.get_client = lambda *a, **k: object()
    app_main.db.load_dashboard = lambda c, u: {
        "month": "2026-06", "metrics": {"total_spent": 1.0}, "goal": {}}
    app_main.db.get_all_transactions = lambda c, u: transactions
    app_main.db.get_overrides = lambda c, u: overrides
    app_main.db.get_custom_categories = lambda c, u: []
    app_main.db.get_budget_targets = lambda c, u: {}
    app_main.db.get_user_profile = lambda c, u: {}
    app_main.db.get_streak = lambda c, u: None
    app_main.db.get_goal = lambda c, u: None
    app_main.categorise_merchants = lambda m, cats, context=None: proposal

    class U:
        user_id = "u1"
        token = "t"

    app_main.app.dependency_overrides[app_main.get_current_user] = lambda: U()
    app_main._RATE.clear()

    def restore():
        for k, v in saved.items():
            if v is not None:
                setattr(app_main.db, k, v)
        app_main.categorise_merchants = saved_llm
        app_main.app.dependency_overrides.clear()

    return TestClient(app_main.app), restore


_RECLASS_TX = [
    {"date": "2026-06-01", "amount": -65.0, "merchant": "CALTEX WOOLWORTHS FUEL",
     "category": "Groceries", "is_expense": True},
    {"date": "2026-06-03", "amount": -72.4, "merchant": "CALTEX WOOLWORTHS FUEL",
     "category": "Groceries", "is_expense": True},
    {"date": "2026-06-09", "amount": -31.0, "merchant": "UBER EATS",
     "category": "Food & Dining", "is_expense": True},
    {"date": "2026-06-11", "amount": -90.0, "merchant": "PINNED MERCHANT",
     "category": "Shopping", "is_expense": True},
]


@test("reclassify: proposes only confident, genuinely different categories")
def _():
    proposal = {
        "CALTEX WOOLWORTHS FUEL": {"category": "Transport", "confidence": "high"},
        "UBER EATS": {"category": "Food & Dining", "confidence": "high"},
    }
    client, restore = _reclassify_client(_RECLASS_TX, [], proposal)
    try:
        body = client.post("/reclassify").json()
    finally:
        restore()

    names = [c["merchant"] for c in body["changes"]]
    assert_in("CALTEX WOOLWORTHS FUEL", names)
    # Already correct → nothing to confirm.
    assert_true("UBER EATS" not in names)
    change = body["changes"][0]
    assert_eq(change["from"], "Groceries")
    assert_eq(change["to"], "Transport")
    assert_eq(change["count"], 2)
    assert_eq(change["total"], 137.4)


@test("reclassify: never re-decides a merchant the user set themselves")
def _():
    proposal = {
        "CALTEX WOOLWORTHS FUEL": {"category": "Transport", "confidence": "high"},
        "PINNED MERCHANT": {"category": "Health", "confidence": "high"},
    }
    overrides = [{"match": "PINNED MERCHANT", "category": "Shopping"}]
    client, restore = _reclassify_client(_RECLASS_TX, overrides, proposal)
    try:
        body = client.post("/reclassify").json()
    finally:
        restore()

    names = [c["merchant"] for c in body["changes"]]
    assert_true("PINNED MERCHANT" not in names,
                "a user's own correction must never be proposed away")
    assert_eq(body["skipped_pinned"], 1)


@test("reclassify: an unsure answer is never offered for confirmation")
def _():
    proposal = {
        "CALTEX WOOLWORTHS FUEL": {"category": "Transport", "confidence": "low"},
        "UBER EATS": {"category": None, "confidence": "low"},
    }
    client, restore = _reclassify_client(_RECLASS_TX, [], proposal)
    try:
        body = client.post("/reclassify").json()
    finally:
        restore()
    assert_eq(body["changes"], [])


@test("reclassify: writes nothing by itself")
def _():
    # The endpoint must be a pure proposal — applying goes through /overrides
    # only after the user confirms.
    import main as app_main

    writes = []
    proposal = {"CALTEX WOOLWORTHS FUEL": {"category": "Transport", "confidence": "high"}}
    client, restore = _reclassify_client(_RECLASS_TX, [], proposal)
    saved = (app_main.db.save_overrides, app_main.db.save_snapshot)
    app_main.db.save_overrides = lambda *a, **k: writes.append("save_overrides")
    app_main.db.save_snapshot = lambda *a, **k: writes.append("save_snapshot")
    try:
        assert_eq(client.post("/reclassify").status_code, 200)
    finally:
        app_main.db.save_overrides, app_main.db.save_snapshot = saved
        restore()
    assert_eq(writes, [], "reclassify must not persist anything")


@test("reclassify: 503 when the server has no model configured")
def _():
    client, restore = _reclassify_client(_RECLASS_TX, [], None)
    try:
        assert_eq(client.post("/reclassify").status_code, 503)
    finally:
        restore()


# ── Persistence-layer regressions (audit fixes) ─────────────────────────────
# These use a fake Supabase client so they run offline and assert on the exact
# bugs found in the audit, not on incidental behaviour.

class _FakeQuery:
    def __init__(self, rows, log=None, table=None):
        self.rows, self.log, self.table_name = list(rows), log, table

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def neq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def lt(self, *a, **k): return self

    def order(self, field, desc=False):
        self.rows = sorted(self.rows, key=lambda r: r.get(field) or "", reverse=desc)
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    # Writes are no-ops by default; individual tests patch these to record calls.
    def upsert(self, *a, **k): return self
    def insert(self, *a, **k): return self
    def delete(self, *a, **k): return self

    def execute(self):
        return self

    @property
    def data(self):
        return self.rows


class _FakeClient:
    """Counts table touches so we can assert on read amplification."""

    def __init__(self, tables):
        self.tables, self.reads = tables, {}

    def table(self, name):
        self.reads[name] = self.reads.get(name, 0) + 1
        return _FakeQuery(self.tables.get(name, []), table=name)


@test("db: newest chats survive the row cap in list_chats")
def _():
    from modules import db

    rows = [
        {"chat_id": "old", "role": "user", "message": f"m{i}",
         "timestamp": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}"}
        for i in range(2400)
    ]
    rows += [
        {"chat_id": "newest", "role": "user", "message": "today",
         "timestamp": f"2026-08-16T10:0{i}:00"} for i in range(3)
    ]
    chats = db.list_chats(_FakeClient({"chat_history": rows}), "u1")
    ids = [c["chat_id"] for c in chats]
    # An oldest-first fetch with a cap used to hide the user's recent chats.
    assert_true("newest" in ids, f"newest chat missing from sidebar: {ids}")
    assert_eq(ids[0], "newest")


@test("db: snapshot + history are read once per request, not re-fetched")
def _():
    from modules import db

    snap = [{"month": "2026-06-01", "summary_json": {
        "metrics": {}, "overrides": [], "budget_targets": {}, "custom_categories": []}}]
    txs = [{"date": "2026-06-01", "amount": -5.0, "merchant": "A",
            "category": "food", "is_expense": True}] * 50
    client = _FakeClient({"snapshots": snap, "transactions": txs})

    db.load_dashboard(client, "u1")
    db.get_all_transactions(client, "u1")
    db.get_all_transactions(client, "u1")
    db.get_overrides(client, "u1")
    db.get_budget_targets(client, "u1")
    db.get_custom_categories(client, "u1")

    assert_eq(client.reads["snapshots"], 1)
    assert_eq(client.reads["transactions"], 1)


@test("db: a write invalidates the per-request read memo")
def _():
    from modules import db

    snap = [{"month": "2026-06-01", "summary_json": {"overrides": ["before"]}}]
    client = _FakeClient({"snapshots": snap})
    assert_eq(db.get_overrides(client, "u1"), ["before"])

    # save_snapshot must drop the memo so a later read sees the new row.
    client.tables["snapshots"] = [
        {"month": "2026-06-01", "summary_json": {"overrides": ["after"]}}]
    db.save_snapshot(client, "u1", "2026-06-01", {"overrides": ["after"]})
    assert_eq(db.get_overrides(client, "u1"), ["after"])


@test("db: an empty parse can never wipe stored history")
def _():
    from modules import db

    deletes = []

    class Guard(_FakeClient):
        def table(self, name):
            q = super().table(name)
            q.delete = lambda *a, **k: (deletes.append(name), q)[1]
            return q

    client = Guard({"transactions": [{"date": "2026-01-01", "amount": -1.0,
                                      "merchant": "A", "category": "food",
                                      "is_expense": True}]})
    db.replace_all_transactions(client, "u1", [])
    assert_eq(deletes, [], "empty replacement must not delete anything")


@test("db: budgets upsert in one round-trip, skipping unset limits")
def _():
    from modules import db

    calls = []

    class Rec(_FakeClient):
        def table(self, name):
            q = super().table(name)
            q.upsert = lambda payload, **k: (calls.append(payload), q)[1]
            return q

    rows = [{"category": "food", "suggested_limit": 300.0},
            {"category": "transport", "suggested_limit": None},
            {"category": "fun", "suggested_limit": 120.0}]
    db.upsert_budgets(Rec({}), "u1", "2026-06-01", rows)
    assert_eq(len(calls), 1, "should be one batched upsert")
    assert_eq(len(calls[0]), 2, "rows with no limit must be skipped")


@test("api: paid endpoints are rate limited per user")
def _():
    import main as app_main

    app_main._RATE.clear()
    limit, _window = app_main.RATE_LIMITS["/coach"]
    for _ in range(limit):
        app_main._rate_limit("user-a", "/coach")
    try:
        app_main._rate_limit("user-a", "/coach")
        raise AssertionError("expected a 429 once over the limit")
    except app_main.HTTPException as exc:
        assert_eq(exc.status_code, 429)
    # A different user is unaffected.
    app_main._rate_limit("user-b", "/coach")
    app_main._RATE.clear()


@test("api: profile updates invalidate cached period views")
def _():
    import main as app_main

    app_main._VIEW_CACHE.clear()
    app_main._VIEW_CACHE["u1|monthly|None|None|None"] = (9e9, {"stale": True})
    app_main._VIEW_CACHE["u2|monthly|None|None|None"] = (9e9, {"keep": True})
    app_main._cache_clear_user("u1")
    assert_true("u1|monthly|None|None|None" not in app_main._VIEW_CACHE)
    assert_true("u2|monthly|None|None|None" in app_main._VIEW_CACHE)
    app_main._VIEW_CACHE.clear()


@test("api: view cache evicts expired entries and stays bounded")
def _():
    import main as app_main

    app_main._VIEW_CACHE.clear()
    app_main._VIEW_CACHE["old|monthly|None|None|None"] = (0.0, {"expired": True})
    for i in range(app_main._VIEW_CACHE_MAX + 50):
        app_main._VIEW_CACHE[f"u{i}|monthly|None|None|None"] = (time.time(), {"i": i})
    app_main._cache_evict()
    assert_true("old|monthly|None|None|None" not in app_main._VIEW_CACHE)
    assert_true(len(app_main._VIEW_CACHE) <= app_main._VIEW_CACHE_MAX)
    app_main._VIEW_CACHE.clear()


def main():
    print("\n=== Finio full test suite ===\n")

    for _name, fn in ALL_TESTS:
        fn()

    print(f"\n=== Results: {passed} passed, {failed} failed, {skipped} skipped ===\n")
    if failures:
        print("Failures:\n")
        for name, err, tb in failures:
            print(f"--- {name} ---")
            print(tb)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
