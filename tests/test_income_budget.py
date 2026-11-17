"""Income-aware budgets use user choices and complete months, not arbitrary ratios."""
from unittest.mock import Mock

import pandas as pd
import pytest

from modules.analytics import compute_averages
from modules.income_budget import budget_plan, budget_question_answer, monthly_income_question_answer
from modules import ai_coach
from schemas import ProfileRequest


def history():
    rows = []
    for month in (1, 2):
        end = 31 if month == 1 else 28
        rows += [
            {'date': f'2026-{month:02d}-01', 'amount': 4000, 'flow': 'income', 'category': 'Income', 'description': 'PAY'},
            {'date': f'2026-{month:02d}-10', 'amount': -1100, 'flow': 'expense', 'category': 'Food & Dining', 'description': 'FOOD'},
            {'date': f'2026-{month:02d}-15', 'amount': -1600, 'flow': 'expense', 'category': 'Housing', 'description': 'RENT'},
            {'date': f'2026-{month:02d}-{end}', 'amount': -100, 'flow': 'expense', 'category': 'Transport', 'description': 'TRAIN'},
        ]
    return rows


def test_complete_month_category_baseline_excludes_partial_edge():
    rows = history() + [{'date': '2026-03-11', 'amount': -9000, 'flow': 'expense',
                         'category': 'Food & Dining', 'description': 'PARTIAL'}]
    average = compute_averages(pd.DataFrame(rows))
    assert average['months_used'] == 2
    assert average['monthly']['spent'] == 2800
    assert average['category_monthly']['Food & Dining'] == 1100


def test_income_and_savings_target_make_budget_assessment_specific():
    averages = compute_averages(pd.DataFrame(history()))
    plan = budget_plan(averages, 4000, 500, {'budgets': [
        {'category': 'Food & Dining', 'suggested_limit': 1153.45, 'limit_source': 'baseline'}]})
    answer = budget_question_answer('is 1153.45 for food noth alot', plan)
    assert '28.8%' in answer
    assert 'previous period' in answer
    assert '$1,100.00 across 2 complete months' in answer
    assert '$1,700.00 of other usual spending' in answer
    assert '$646.55 of room' in answer
    assert plan['remaining_after_savings_target'] == 700


def test_shortfall_and_missing_income_are_honest():
    averages = compute_averages(pd.DataFrame(history()))
    without_income = budget_plan(averages, None, None)
    assert 'Add it in Profile' in budget_question_answer('is $1153.45 for food too much?', without_income)
    with_shortfall = budget_plan(averages, 3000, 500)
    answer = budget_question_answer('is $1153.45 for food too much?', with_shortfall)
    assert 'exceeds the remaining room by $353.45' in answer
    impossible = budget_question_answer('is $1153.45 for food too much?',
                                        budget_plan(averages, 2000, 500))
    assert 'already exceed income' in impossible
    assert 'zero food budget' in impossible


def test_no_complete_month_does_not_extrapolate_partial_history():
    averages = compute_averages(pd.DataFrame(history()[:3]))
    plan = budget_plan(averages, 4000, 500)
    assert plan['complete_months'] == 0
    answer = budget_question_answer('is $1153.45 for food too much?', plan)
    assert "don't have a complete calendar month" in answer
    assert 'Your Food & Dining spending averaged' not in answer


def test_income_can_reject_an_unaffordable_amount_without_full_history():
    averages = compute_averages(pd.DataFrame(history()[:3]))
    answer = budget_question_answer('is $1153.45 for food too much?',
                                    budget_plan(averages, 1200, 200))
    assert 'exceeds your income after your $200.00 savings target by $153.45' in answer
    assert "don't have a complete calendar month" in answer


def test_budget_questions_cover_other_categories_without_mislabeling_groceries():
    plan = budget_plan(compute_averages(pd.DataFrame(history())), 4000, 500)
    transport = budget_question_answer('is $200 for transport too high?', plan)
    groceries = budget_question_answer('is $200 for groceries too high?', plan)
    assert 'for Transport' in transport
    assert 'Your Transport spending averaged $100.00' in transport
    assert 'for Groceries' in groceries
    assert 'for Food & Dining' not in groceries


def test_budget_amount_question_is_calculated_not_left_to_model(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(ai_coach, 'get_client', lambda: provider)
    context = {'income_budget': budget_plan(compute_averages(pd.DataFrame(history())), 4000, 500),
               'income': 1000, 'spent': 200, 'saved': 800}
    result = ai_coach.coach_chat('is $1153.45 for food a lot?', context)
    assert result['source'] == 'calculated'
    assert '$4,000.00 monthly take-home income' in result['text']
    provider.chat.completions.create.assert_not_called()


def test_average_monthly_income_uses_income_flow_not_expense_category(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(ai_coach, 'get_client', lambda: provider)
    plan = budget_plan(compute_averages(pd.DataFrame(history())), 3500, 500)
    context = {'income_budget': plan, 'income': 2180, 'spent': 887.14, 'saved': 1292.86,
               'currency': 'AUD', 'date_range': {'start': '2026-06-01', 'end': '2026-06-11'}}
    question = 'how much do i get income per month on avergae'
    assert ai_coach.requested_total_tool(question) is None
    result = ai_coach.coach_chat(question, context)
    assert result['source'] == 'calculated'
    assert 'income averaged AUD 4,000.00 per month' in result['text']
    assert 'entered AUD 3,500.00' in result['text']
    assert 'Net spending for income' not in result['text']
    assert [fact['kind'] for fact in result['verified_facts']] == ['monthly_income']
    assert result['verified_facts'][0]['observed_average'] == 4000
    provider.chat.completions.create.assert_not_called()


def test_partial_statement_is_not_extrapolated_to_monthly_income():
    partial = compute_averages(pd.DataFrame(history()[:3]))
    plan = budget_plan(partial, 3500, None)
    reply, fact = monthly_income_question_answer('average monthly income', plan, {
        'currency': 'AUD', 'income': 2180,
        'date_range': {'start': '2026-06-01', 'end': '2026-06-11'}})
    assert 'no complete calendar month' in reply
    assert 'AUD 2,180.00' in reply
    assert 'not a monthly average' in reply
    assert 'entered AUD 3,500.00' in reply
    assert fact['observed_average'] is None
    assert fact['selected_income'] == 2180


def test_generic_income_total_fallback_does_not_search_expenses():
    context = {'income': 2180, 'currency': 'AUD',
               'date_range': {'start': '2026-06-01', 'end': '2026-06-11'}}
    transactions = [{'date': '2026-06-04', 'amount': -50, 'flow': 'expense',
                     'category': 'Other', 'merchant': 'Income Cafe'}]
    answer = ai_coach.fallback_coach_response('how much income did I get?', context, transactions)
    assert 'AUD 2,180.00 of income' in answer
    assert 'spent' not in answer.lower()


@pytest.mark.parametrize('payload', [
    {'monthly_income': -1}, {'monthly_income': float('nan')},
    {'monthly_savings_target': -1}, {'monthly_savings_target': float('inf')},
])
def test_profile_rejects_invalid_budget_inputs(payload):
    with pytest.raises(ValueError):
        ProfileRequest(**payload)
