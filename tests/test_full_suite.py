"""
Full Finio test suite — run from project root:
  python -m tests.test_full_suite
"""

import io
import os
import sys
import tempfile
import traceback
from datetime import date
from pathlib import Path

# project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
    assert_in(DISCLAIMER, text)


@test("ai_coach: coach_chat always returns disclaimer")
def _():
    from modules.ai_coach import coach_chat, run_pipeline_context

    ctx = run_pipeline_context()["context"]
    r = coach_chat("What's my biggest category?", ctx)
    assert_in("disclaimer", r)
    assert_in(DISCLAIMER, r["text"])
    assert_in(r["source"], ["openai", "fallback"])


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


@test("ai_coach: generate_insight returns text with the disclaimer")
def _():
    from modules.ai_coach import generate_insight

    ctx = {
        "spent": 1200, "saved": 300, "savings_rate": 20.0,
        "top_categories": [{"category": "Food & Dining", "amount": 500, "pct": 40}],
    }
    out = generate_insight(ctx)
    assert_true(out["text"])
    assert_in("not financial advice", out["text"].lower())


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
