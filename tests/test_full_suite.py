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


@test("spend_check: green for small purchase")
def _():
    from modules.pipeline import run_full_pipeline
    from modules.spend_check import check_purchase

    r = run_full_pipeline(SAMPLE_CSV)
    result = check_purchase(None, r["metrics"], 10, days_ahead=30)
    assert_eq(result["verdict"], "green")
    assert_in("disclaimer", result)


@test("spend_check: red for huge purchase")
def _():
    from modules.pipeline import run_full_pipeline
    from modules.spend_check import check_purchase

    r = run_full_pipeline(SAMPLE_CSV)
    result = check_purchase(None, r["metrics"], 5000, days_ahead=30)
    assert_eq(result["verdict"], "red")


@test("spend_check: yellow for medium purchase")
def _():
    from modules.pipeline import run_full_pipeline
    from modules.spend_check import check_purchase

    r = run_full_pipeline(SAMPLE_CSV)
    result = check_purchase(None, r["metrics"], 900, days_ahead=30)
    assert_eq(result["verdict"], "yellow")


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
        "forecast",
        "budgets",
        "invest",
        "personality",
        "snapshot",
        "context",
        "transactions",
        "goal_recommendation",
        "goal_used",
        "period",
        "disclaimer",
    }
    assert_eq(set(r.keys()), keys)
    assert_eq(len(r["transactions"]), 20)
    assert_in("merchant", r["transactions"][0])
    assert_eq(r["forecast"]["target_amount"], 8000)
    assert_eq(r["invest"]["etf"]["recommended"], "VGS")  # age 30
    assert_eq(r["goal_used"]["amount"], 8000)


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
    assert_eq(rec["amount"], 1000)  # starter buffer when spending > income


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
