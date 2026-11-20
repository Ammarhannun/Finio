"""Everyday coach questions against synthetic, multi-month statement history."""
from types import SimpleNamespace
from unittest.mock import Mock
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.deps import AuthUser, get_current_user
from modules import ai_coach, db
from modules.income_budget import budget_plan
from modules.pipeline import analyze_stored
import main


def build_synthetic_coach():
    rows = []
    for month, end in ((1, 31), (2, 28), (3, 31)):
        prefix = f"2026-{month:02d}"
        rows.extend([
            {"date": f"{prefix}-01", "amount": 4000, "merchant": "ACME PAYROLL", "category": "Income", "flow": "income"},
            {"date": f"{prefix}-03", "amount": -1500, "merchant": "RENT", "category": "Housing & Rent", "flow": "expense"},
            {"date": f"{prefix}-05", "amount": -550, "merchant": "MARKET", "category": "Groceries", "flow": "expense"},
            {"date": f"{prefix}-07", "amount": 50, "merchant": "MARKET REFUND", "category": "Groceries", "flow": "expense"},
            {"date": f"{prefix}-10", "amount": -350, "merchant": "CAFE", "category": "Food & Dining", "flow": "expense"},
            {"date": f"{prefix}-12", "amount": -150, "merchant": "TRAIN", "category": "Transport", "flow": "expense"},
            {"date": f"{prefix}-15", "amount": -100, "merchant": "STREAM", "category": "Subscriptions", "flow": "expense"},
            {"date": f"{prefix}-{end}", "amount": -800, "merchant": "OWN SAVINGS", "category": "Transfers", "flow": "transfer"},
        ])
    rows.extend([
        {"date": "2026-04-01", "amount": 2000, "merchant": "ACME PAYROLL", "category": "Income", "flow": "income"},
        {"date": "2026-04-06", "amount": -300, "merchant": "MARKET", "category": "Groceries", "flow": "expense"},
        {"date": "2026-04-11", "amount": -100, "merchant": "CAFE", "category": "Food & Dining", "flow": "expense"},
    ])
    data = analyze_stored(rows, budget_targets={"Food & Dining": 900},
                          period="monthly", period_anchor="2026-04-01")
    context = {
        **data["context"],
        "income_budget": budget_plan(data["averages"], 4000, 800, data["budgets"]),
        "confirmed_goal": None,
        "forecast_months_used": data["averages"]["months_used"],
    }
    return rows, data, context


@pytest.fixture
def synthetic_coach(monkeypatch):
    monkeypatch.setattr(ai_coach, "get_client", lambda: None)
    return build_synthetic_coach()


def test_synthetic_history_has_correct_monthly_truth(synthetic_coach):
    _, data, context = synthetic_coach
    assert data["averages"]["months_used"] == 3
    assert data["averages"]["monthly"] == {"spent": 2600.0, "income": 4000.0, "saved": 1400.0}
    assert context["income"] == 2000  # The selected April window is partial.
    assert context["income_budget"]["category_monthly"]["Groceries"] == 500


@pytest.mark.parametrize("question, expected, forbidden", [
    ("Hi", "Hi!", "You spent"),
    ("How much income do I get per month on average?", "AUD 4,000.00 per month", "Net spending for income"),
    ("How much income did I get in this period?", "recorded income was AUD 2,000.00", "You spent"),
    ("How much income did I get this month?", "recorded income was AUD 2,000.00", "income averaged"),
    ("What is my usual monthly spending?", "total spending averaged AUD 2,600.00 per month", "Net spending for total"),
    ("How much did I spend this month?", "recorded spending was AUD 400.00", "Groceries was AUD 300.00"),
    ("How much do I spend on groceries per month on average?", "Groceries averaged AUD 500.00 per month", "AUD 300.00"),
    ("Is $900 a month for Food & Dining too much?", "$50.00 of room", "recent spending patterns"),
    ("Is $1,200 for food too much?", "exceeds the remaining room by $250.00", "recent spending patterns"),
    ("What should my groceries budget be each month if I want to save $800?", "draft starting reference is your $500.00 historical average", "75% of your grocery budget"),
    ("How much did I spend on groceries?", "You spent $300.00", "You spent $500.00"),
    ("Am I on track to save?", "can't tell if you're on track without a savings goal", "on pace"),
    ("What is my biggest spending category?", "biggest spending category", "recorded spending was"),
    ("How much did I save this month?", "net savings were AUD 1,600.00", "great job"),
    ("What is my savings rate?", "80.0% of recorded income", "great job"),
    ("What should my monthly budgets be?", "Historical category averages:", "you've only spent"),
    ("Show me all my transfers", "no transactions classified as transfers", "no transfers recorded in your transaction history"),
    ("Can I afford a $700 laptop if my balance is $1,500?", "hypothetical until you confirm it", "you might be okay"),
])
def test_everyday_questions_use_the_right_financial_basis(synthetic_coach, question, expected, forbidden):
    rows, data, context = synthetic_coach
    result = ai_coach.coach_chat(question, context, transactions=data["transactions"])
    assert expected in result["text"]
    assert forbidden not in result["text"]
    assert result["proposed_actions"] == []
    if "income" in question.lower() and "average" in question.lower():
        assert [fact["kind"] for fact in result["verified_facts"]] == ["monthly_income"]
    if "usual monthly spending" in question.lower():
        assert [fact["kind"] for fact in result["verified_facts"]] == ["monthly_spending"]
    if "groceries budget be" in question.lower():
        assert [fact["kind"] for fact in result["verified_facts"]] == ["budget_plan"]
        assert result["verified_facts"][0]["monthly_savings_target"] == 800


def test_budget_question_does_not_call_model_even_when_available(synthetic_coach, monkeypatch):
    _, data, context = synthetic_coach
    provider = Mock()
    monkeypatch.setattr(ai_coach, "get_client", lambda: provider)
    result = ai_coach.coach_chat("Is $1,200 for food too much?", context,
                                transactions=data["transactions"])
    assert result["source"] == "calculated"
    provider.chat.completions.create.assert_not_called()


def test_plural_budget_request_creates_one_complete_review_batch(synthetic_coach, monkeypatch):
    _, data, context = synthetic_coach
    categories = ["Food & Dining", "Transport", "Groceries", "Housing & Rent", "Subscriptions"]
    rows = [{"category": name, "amount": amount, "why": "Monthly planning draft"}
            for name, amount in zip(categories, (350, 150, 500, 1500, 100))]
    call = SimpleNamespace(id="batch", function=SimpleNamespace(
        name="propose_budgets", arguments=json.dumps({"budgets": rows})))
    responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="", tool_calls=[call]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="Review these limits.", tool_calls=[]))])]
    provider = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=Mock(side_effect=responses))))
    monkeypatch.setattr(ai_coach, "get_client", lambda: provider)
    message = ("Using my latest data, propose monthly budgets for Food & Dining, Transport, "
               "Groceries, Housing & Rent, and Subscriptions together. Do not apply them.")
    result = ai_coach.coach_chat(message, context, transactions=data["transactions"])
    assert result["source"] == "openai"
    assert [p["category"] for p in result["proposed_actions"]] == categories
    assert all(p["kind"] == "budget" for p in result["proposed_actions"])
    assert provider.chat.completions.create.call_args_list[0].kwargs["tool_choice"]["function"]["name"] == "propose_budgets"


def test_plural_budget_request_without_entered_income_does_not_invent_limits(synthetic_coach, monkeypatch):
    _, data, context = synthetic_coach
    context["income_budget"]["monthly_income"] = None
    provider = Mock()
    monkeypatch.setattr(ai_coach, "get_client", lambda: provider)
    result = ai_coach.coach_chat("Propose budgets for groceries and transport", context,
                                transactions=data["transactions"])
    assert "enter your typical monthly take-home income" in result["text"]
    assert result["proposed_actions"] == []
    provider.chat.completions.create.assert_not_called()


def test_explicit_multiple_limits_need_no_income_or_model(synthetic_coach, monkeypatch):
    _, data, context = synthetic_coach
    context["income_budget"]["monthly_income"] = None
    provider = Mock()
    monkeypatch.setattr(ai_coach, "get_client", lambda: provider)
    result = ai_coach.coach_chat(
        "Set these exact monthly budgets for review: Food & Dining $800, "
        "Transport $250, Groceries $600, Entertainment $100, Subscriptions $80.",
        context, transactions=data["transactions"])
    assert result["source"] == "calculated"
    assert [p["amount"] for p in result["proposed_actions"]] == [800, 250, 600, 100, 80]
    assert all(p["why"] == "Amount you specified" for p in result["proposed_actions"])
    provider.chat.completions.create.assert_not_called()


def test_incomplete_explicit_limits_do_not_guess_missing_category(synthetic_coach, monkeypatch):
    _, data, context = synthetic_coach
    context["income_budget"]["monthly_income"] = None
    monkeypatch.setattr(ai_coach, "get_client", lambda: Mock())
    result = ai_coach.coach_chat("Set budgets for Food & Dining $800 and Transport", context,
                                transactions=data["transactions"])
    assert result["proposed_actions"] == []
    assert "enter your typical monthly" in result["text"]


def test_multi_budget_tool_rejects_limits_that_exceed_income(synthetic_coach):
    _, data, context = synthetic_coach
    result = ai_coach.run_tool("propose_budgets", {"budgets": [
        {"category": "Groceries", "amount": 2000},
        {"category": "Transport", "amount": 1500}]}, data["transactions"], context)
    assert result["ok"] is False
    assert "exceed entered monthly income" in result["error"]


def test_missing_category_history_cannot_be_invented_for_a_budget(synthetic_coach, monkeypatch):
    _, data, context = synthetic_coach
    provider = Mock()
    monkeypatch.setattr(ai_coach, "get_client", lambda: provider)
    result = ai_coach.coach_chat(
        "Propose budgets for Food & Dining and Entertainment",
        context, transactions=data["transactions"])
    assert result["source"] == "calculated"
    assert "no complete-month spending history for Entertainment" in result["text"]
    assert result["proposed_actions"] == []
    provider.chat.completions.create.assert_not_called()
    tool_result = ai_coach.run_tool("propose_budgets", {"budgets": [
        {"category": "Food & Dining", "amount": 350},
        {"category": "Entertainment", "amount": 200}]}, data["transactions"], context)
    assert tool_result["ok"] is False
    assert "do not invent limits" in tool_result["error"]


def test_question_savings_target_overrides_profile_only_for_budget_validation(synthetic_coach, monkeypatch):
    _, data, context = synthetic_coach
    rows = [{"category": category, "amount": amount} for category, amount in (
        ("Food & Dining", 350), ("Transport", 150), ("Groceries", 500),
        ("Housing & Rent", 1500), ("Subscriptions", 600))]
    assert ai_coach.run_tool("propose_budgets", {"budgets": rows}, data["transactions"], context)["ok"]
    call = SimpleNamespace(id="batch", function=SimpleNamespace(
        name="propose_budgets", arguments=json.dumps({"budgets": rows})))
    responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="", tool_calls=[call]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="Review these limits.", tool_calls=[]))])]
    provider = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=Mock(side_effect=responses))))
    monkeypatch.setattr(ai_coach, "get_client", lambda: provider)
    result = ai_coach.coach_chat(
        "I want to save $1,000 monthly. Propose budgets for Food & Dining, Transport, "
        "Groceries, Housing & Rent, and Subscriptions.", context,
        transactions=data["transactions"])
    assert result["proposed_actions"] == []
    assert context["income_budget"]["monthly_savings_target"] == 800
    assert "exceed entered monthly income" in provider.chat.completions.create.call_args_list[1].kwargs["messages"][-1]["content"]


def test_transfer_list_is_scoped_to_provided_transactions(synthetic_coach):
    rows, _, context = synthetic_coach
    context["date_range"] = {"start": "2026-01-01", "end": "2026-03-31"}
    result = ai_coach.coach_chat("Show all my transfers", context, transactions=rows)
    assert result["source"] == "calculated"
    assert "3 transfer transactions" in result["text"]
    assert "AUD 2,400.00 out" in result["text"]
    assert "Transfers are excluded" in result["text"]


def test_purchase_estimate_uses_requested_horizon_and_verified_balance(synthetic_coach, monkeypatch):
    _, data, context = synthetic_coach
    today = date.today().isoformat()
    context.update(available_balance={"amount": 1500, "is_current": True, "as_of": today},
                   latest_balance=1500, balance_date=today, user_entered_balance=True,
                   has_spending_baseline=True)
    provider = Mock()
    monkeypatch.setattr(ai_coach, "get_client", lambda: provider)
    result = ai_coach.coach_chat("Can I afford a $700 laptop next week?", context,
                                transactions=data["transactions"])
    assert result["source"] == "calculated"
    assert "over 7 days" in result["text"]
    assert "Projected balance" in result["text"]
    assert "not a guarantee" in result["text"]
    assert "can afford" not in result["text"]
    provider.chat.completions.create.assert_not_called()
    assert ai_coach._affordability_answer("Should I buy $100 of Bitcoin?", context) is None


def test_multi_budget_intent_recognises_followup_and_plurals():
    assert ai_coach.requested_action_tool("Propose monthly budgets for food and transport") == "propose_budgets"
    assert ai_coach.requested_action_tool("Set my groceries budget to $500") == "propose_budget"
    assert ai_coach.requested_action_tool("apply to all", [
        {"role": "user", "content": "Please propose budgets for Groceries and Transport"}]) == "propose_budgets"


def test_short_confirmation_continues_only_the_immediate_budget_offer():
    history = [
        {"role": "user", "content": "I want to save $500 per month, update my budgets"},
        {"role": "assistant", "content": "Would you like me to adjust your budget limits?"},
    ]
    assert ai_coach.requested_action_tool("yes adjust", history) == "propose_budgets"
    assert ai_coach.requested_action_tool("yes adjeust", history) == "propose_budgets"
    assert ai_coach.requested_action_tool("yes", history) == "propose_budgets"
    assert ai_coach.requested_action_tool("no", history) is None
    assert ai_coach.requested_action_tool("yes", history + [
        {"role": "user", "content": "What were my subscriptions last month?"},
        {"role": "assistant", "content": "Would you like me to show the transactions?"},
    ]) is None


def test_budget_followup_without_entered_income_requests_profile(synthetic_coach, monkeypatch):
    _, data, context = synthetic_coach
    context["income_budget"]["monthly_income"] = None
    provider = Mock()
    monkeypatch.setattr(ai_coach, "get_client", lambda: provider)
    result = ai_coach.coach_chat("yes adjust", context, history=[
        {"role": "user", "content": "I want to save $500 per month, update my budgets"},
        {"role": "assistant", "content": "Would you like me to adjust your budget limits?"},
    ], transactions=data["transactions"])
    assert "monthly take-home income in Profile" in result["text"]
    assert result["proposed_actions"] == []
    provider.chat.completions.create.assert_not_called()


def test_question_savings_scenario_does_not_become_the_category_amount(synthetic_coach):
    _, data, context = synthetic_coach
    result = ai_coach.coach_chat(
        "Is my Food & Dining budget too high if I want to save $1000?",
        context, transactions=data["transactions"])
    assert "$900.00 a month for Food & Dining" in result["text"]
    assert "exceeds the remaining room by $150.00" in result["text"]
    fact = result["verified_facts"][0]
    assert fact["kind"] == "budget_plan"
    assert fact["monthly_savings_target"] == 1000
    assert fact["savings_target_source"] == "question"


def test_http_coach_uses_all_dummy_history_for_monthly_income(monkeypatch):
    rows, data, _ = build_synthetic_coach()
    data.update(revision="11111111-1111-1111-1111-111111111111", month="2026-04")
    for name, value in {
        "get_client": object(), "get_all_transactions": rows, "get_transactions": rows,
        "get_overrides": [], "get_budget_targets": {"Food & Dining": 900},
        "get_user_profile": {"monthly_income": 4000, "monthly_savings_target": 800},
        "get_chat_history": [], "get_chat_title": "Synthetic coach test",
        "edit_state": {"revision": data["revision"]},
    }.items():
        monkeypatch.setattr(db, name, Mock(return_value=value))
    monkeypatch.setattr(db, "append_chat", Mock())
    monkeypatch.setattr(main, "_require_snapshot", lambda user: (object(), data))
    monkeypatch.setattr(main, "_rate_limit", lambda *args: None)
    monkeypatch.setattr(ai_coach, "get_client", lambda: None)
    main.app.dependency_overrides[get_current_user] = lambda: AuthUser("dummy-account", "dummy-token")
    main._VIEW_CACHE.clear()
    try:
        http = TestClient(main.app)
        for question, expected, fact_kind in (
            ("How much income do I get per month on average?", "AUD 4,000.00 per month", "monthly_income"),
            ("What is my usual monthly spending?", "AUD 2,600.00 per month", "monthly_spending"),
            ("How much did I spend this month?", "AUD 400.00", "summary"),
            ("What should my groceries budget be if I save $800?", "$500.00 historical average", "budget_plan"),
        ):
            response = http.post("/coach", json={
                "message": question, "period": "monthly", "month": "2026-04",
            })
            assert response.status_code == 200
            body = response.json()
            assert body["source"] == "calculated"
            assert expected in body["text"]
            assert body["verified_facts"][0]["kind"] == fact_kind
    finally:
        main.app.dependency_overrides.clear()
        main._VIEW_CACHE.clear()


def test_http_coach_returns_fresh_five_budget_batch_from_explicit_dummy_amounts(monkeypatch):
    rows, data, _ = build_synthetic_coach()
    data.update(revision="11111111-1111-1111-1111-111111111111", month="2026-04")
    for name, value in {
        "get_client": object(), "get_all_transactions": rows, "get_transactions": rows,
        "get_overrides": [], "get_budget_targets": {"Food & Dining": 900},
        "get_user_profile": {"monthly_income": None, "monthly_savings_target": None},
        "get_chat_history": [], "get_chat_title": "Synthetic coach test",
        "edit_state": {"revision": data["revision"]},
    }.items():
        monkeypatch.setattr(db, name, Mock(return_value=value))
    monkeypatch.setattr(db, "append_chat", Mock())
    monkeypatch.setattr(main, "_require_snapshot", lambda user: (object(), data))
    monkeypatch.setattr(main, "_rate_limit", lambda *args: None)
    monkeypatch.setattr(ai_coach, "get_client", lambda: Mock())
    main.app.dependency_overrides[get_current_user] = lambda: AuthUser("dummy-account", "dummy-token")
    main._VIEW_CACHE.clear()
    try:
        body = TestClient(main.app).post("/coach", json={
            "message": ("Set these exact monthly budgets for review: Food & Dining $800, "
                        "Transport $250, Groceries $600, Entertainment $100, Subscriptions $80."),
            "period": "monthly", "month": "2026-04",
        }).json()
        assert body["source"] == "calculated"
        assert len(body["proposed_actions"]) == 5
        assert all(p["revision"] == data["revision"] for p in body["proposed_actions"])
        assert [p["category"] for p in body["proposed_actions"]] == [
            "Food & Dining", "Transport", "Groceries", "Entertainment", "Subscriptions"]
    finally:
        main.app.dependency_overrides.clear()
        main._VIEW_CACHE.clear()
