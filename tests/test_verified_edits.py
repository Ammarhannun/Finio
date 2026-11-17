"""Synthetic contracts for edits and statement facts; no network or credentials."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import main
from api.deps import AuthUser, get_current_user
from modules import db, ai_coach
from modules.pipeline import analyze_stored, run_full_pipeline

REV = str(uuid4())
NEW = str(uuid4())
ROWS = [
    {'date': '2026-01-01', 'amount': 2000, 'merchant': 'SALARY', 'category': 'Income', 'flow': 'income'},
    {'date': '2026-01-02', 'amount': -110, 'merchant': 'COLES', 'category': 'Groceries', 'flow': 'expense'},
    {'date': '2026-01-03', 'amount': 10, 'merchant': 'COLES REFUND', 'category': 'Groceries', 'flow': 'expense'},
    {'date': '2026-02-01', 'amount': -25, 'merchant': 'PAYID ALEX', 'category': 'Transfers', 'flow': 'transfer'},
    {'date': '2026-02-02', 'amount': -50, 'merchant': 'COLES', 'category': 'Groceries', 'flow': 'expense'},
]

@pytest.fixture
def client(monkeypatch):
    state = {'revision': REV, 'can_undo': True, 'undo_label': 'Goal'}
    data = analyze_stored(ROWS)
    data.update(state)
    data['month'] = '2026-02'
    data['goal'] = {'target_amount': 6000, 'target_date': '2027-12-01'}
    for name, value in {'is_configured': True, 'get_client': object(), 'load_dashboard': data,
        'get_all_transactions': ROWS, 'get_overrides': [], 'get_custom_categories': [],
        'get_budget_targets': {}, 'get_user_profile': {'age': 34, 'income_bracket': 'full_time', 'email': 'synthetic@example.invalid'},
        'get_goal': data['goal'], 'get_streak': {}, 'get_user': SimpleNamespace(email='synthetic@example.invalid'),
        'get_latest_snapshot': {'month': '2026-02', 'revision': REV, 'summary_json': {}},
        'edit_state': state, 'get_pending_questions': [{'merchant': 'PAYID ALEX'}]}.items():
        monkeypatch.setattr(db, name, Mock(return_value=value))
    def saved(*args, **kwargs):
        state['revision'] = NEW
        return {'revision': NEW, 'can_undo': True}
    commit = Mock(side_effect=saved)
    monkeypatch.setattr(db, 'commit_edit', commit)
    main.app.dependency_overrides[get_current_user] = lambda: AuthUser('synthetic', 'fake')
    main._VIEW_CACHE.clear()
    main._RATE.clear()
    yield TestClient(main.app, raise_server_exceptions=False), commit
    main.app.dependency_overrides.clear()
    main._VIEW_CACHE.clear()

@pytest.mark.parametrize('path,body', [
    ('/budgets', {'targets': {'Groceries': 400}}),
    ('/overrides', {'rules': [{'match': 'PAYID ALEX', 'flow': 'expense', 'category': 'Food & Drink'}]}),
    ('/goal', {'amount': 7000, 'target_date': '2027-12-01'}),
    ('/quiz', {'merchant': 'PAYID ALEX', 'flow': 'transfer'}),
    ('/profile', {'age': 35}), ('/reanalyze', {}), ('/changes/undo', {}),
])
def test_all_editor_routes_require_revision_and_commit_once(client, path, body):
    http, commit = client
    assert http.post(path, json=body).status_code == 428
    commit.assert_not_called()
    assert http.post(path, json={**body, 'revision': str(uuid4())}).status_code == 409
    commit.assert_not_called()
    response = http.post(path, json={**body, 'revision': REV})
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == NEW
    commit.assert_called_once()


def test_late_database_conflict_is_409(client):
    http, commit = client
    class Conflict(Exception): code = '40001'
    commit.side_effect = Conflict('private details')
    response = http.post('/budgets', json={'revision': REV, 'targets': {'Groceries': 500}})
    assert response.status_code == 409
    assert 'private details' not in response.text


def test_multiple_coach_budget_targets_commit_together_once(client):
    http, commit = client
    targets = {'Food & Dining': 800, 'Transport': 250, 'Groceries': 600}
    response = http.post('/budgets', json={'revision': REV, 'targets': targets})
    assert response.status_code == 200, response.text
    assert response.json()['targets'] == targets
    commit.assert_called_once()
    assert commit.call_args.kwargs['changes']['budget_targets'] == targets
    saved_rows = commit.call_args.kwargs['resliced']['budgets']['budgets']
    assert {row['category']: row['suggested_limit'] for row in saved_rows
            if row['category'] in targets} == targets
    stale = http.post('/budgets', json={'revision': REV, 'targets': {'Shopping': 300}})
    assert stale.status_code == 409
    commit.assert_called_once()


def test_monthly_income_and_savings_target_use_the_atomic_profile_edit(client, monkeypatch):
    http, commit = client
    profile = {'age': 34, 'income_bracket': 'full_time', 'email': 'synthetic@example.invalid',
               'monthly_income': None, 'monthly_savings_target': None}
    monkeypatch.setattr(db, 'get_user_profile', lambda *args: dict(profile))
    def save(*args, **kwargs):
        profile.update(kwargs['profile'])
        return {'revision': NEW, 'can_undo': True}
    commit.side_effect = save
    response = http.post('/profile', json={'revision': REV, 'monthly_income': 4000,
                                           'monthly_savings_target': 500})
    assert response.status_code == 200, response.text
    assert response.json()['monthly_income'] == 4000
    assert response.json()['monthly_savings_target'] == 500
    assert commit.call_args.kwargs['profile']['monthly_income'] == 4000


def test_dashboard_exposes_income_budget_without_counting_partial_latest_month(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr(db, 'get_user_profile', lambda *args: {
        'monthly_income': 4000, 'monthly_savings_target': 500})
    response = http.get('/dashboard?period=monthly&month=2026-02')
    assert response.status_code == 200, response.text
    plan = response.json()['income_budget']
    assert plan['monthly_income'] == 4000
    assert plan['monthly_savings_target'] == 500
    assert plan['complete_months'] == 1
    assert plan['usual_monthly_spending'] == 100


def test_savings_target_cannot_exceed_entered_monthly_income(client):
    http, commit = client
    response = http.post('/profile', json={'revision': REV, 'monthly_income': 1000,
                                           'monthly_savings_target': 1200})
    assert response.status_code == 422
    commit.assert_not_called()


def test_failed_commit_is_not_reported_as_success(client):
    http, commit = client
    commit.side_effect = RuntimeError('private database details')
    response = http.post('/goal', json={'revision': REV, 'amount': 7000, 'target_date': '2027-12-01'})
    assert response.status_code == 503
    assert 'Nothing changed' in response.text
    assert 'private database details' not in response.text


@pytest.mark.parametrize('code,copy', [('PGRST202','database update'), ('42703','database update'),
                                      ('42501','Sign in again'), ('23502','Your input is kept')])
def test_save_errors_are_actionable_without_exposing_database_content(client, code, copy):
    http, commit = client
    error = RuntimeError('PRIVATE FINANCIAL DATA')
    error.code = code
    commit.side_effect = error
    response = http.post('/goal', json={'revision': REV, 'amount': 7000, 'target_date': '2027-12-01'})
    assert response.status_code == 503
    assert copy in response.text
    assert 'PRIVATE FINANCIAL DATA' not in response.text


def test_review_notice_and_goal_uncertainty_follow_their_own_time_scope(client):
    http, _ = client
    january = http.get('/dashboard?period=monthly&month=2026-01').json()
    assert january['review']['count'] == 0
    assert january['history_review']['count'] == 1


def test_unresolved_transfers_cannot_produce_ai_savings_praise(monkeypatch):
    generate = Mock(side_effect=AssertionError('No AI recap before transfer review'))
    monkeypatch.setattr(ai_coach, 'call_llm', generate)
    response = ai_coach.generate_insight({'saved':1900,'transfer_review':{'count':2}})
    assert response['source'] == 'calculated'
    assert 'provisional' in response['text']
    generate.assert_not_called()


def test_ai_recap_is_instructed_to_use_statement_dates(monkeypatch):
    generate = Mock(return_value='Between 1 and 3 January 2026, recorded spending was $100.')
    monkeypatch.setattr(ai_coach, 'call_llm', generate)
    ai_coach.generate_insight({'date_range':{'start':'2026-01-01','end':'2026-01-03'}})
    prompt = generate.call_args.args[0]
    assert 'explicit statement dates' in prompt
    assert '2026-01-01' in prompt and '2026-01-03' in prompt


def test_quiz_answer_and_queue_are_one_commit(client):
    http, commit = client
    response = http.post('/quiz', json={'revision': REV, 'merchant': 'PAYID ALEX', 'flow': 'transfer'})
    assert response.status_code == 200
    changes = commit.call_args.kwargs['changes']
    assert changes['pending_questions'] == []
    assert changes['overrides'] == [{'match': 'PAYID ALEX', 'flow': 'transfer'}]


def test_month_totals_are_not_the_typical_month(client):
    http, _ = client
    jan = http.get('/dashboard?period=monthly&month=2026-01').json()
    feb = http.get('/dashboard?period=monthly&month=2026-02').json()
    assert jan['metrics']['total_spent'] == 100  # 110 purchase, 10 refund
    assert feb['metrics']['total_spent'] == 50  # excludes unresolved PayID
    assert feb['review'] == {'count': 1, 'merchant_count':1, 'money_out': 25, 'money_in': 0}
    assert jan['review']['count'] == 0
    assert jan['revision'] == feb['revision'] == REV
    assert feb['averages']['all']['spent'] == 150


def test_confirmed_transfers_leave_review_and_expenses_enter_spending():
    confirmed = analyze_stored(ROWS, overrides=[{'match': 'PAYID ALEX', 'flow': 'transfer'}])
    corrected = analyze_stored(ROWS, overrides=[{'match': 'PAYID ALEX', 'flow': 'expense', 'category': 'Other'}])
    assert confirmed['review']['count'] == corrected['review']['count'] == 0
    assert confirmed['metrics']['total_spent'] == 50
    assert corrected['metrics']['total_spent'] == 75


def test_review_queue_does_not_stop_at_six(tmp_path):
    file = tmp_path / 'synthetic.csv'
    file.write_text('Date,Description,Amount\n' + ''.join(f'2026-02-01,PAYID PERSON {i},-5\n' for i in range(12)))
    result = run_full_pipeline(file)
    assert len(result['pending_questions']) == 12
    assert result['review']['count'] == 12


def test_coach_calculations_survive_incorrect_model_prose(monkeypatch):
    tool = SimpleNamespace(id='tool1', function=SimpleNamespace(name='category_total', arguments='{"query":"COLES"}'))
    client = Mock()
    client.chat.completions.create.side_effect = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool], content=None))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[], content='You spent $90.'))]),
    ]
    monkeypatch.setattr(ai_coach, 'get_client', lambda: client)
    result = ai_coach.coach_chat('COLES spending?', {'income': 2000, 'spent': 100, 'saved': 1900,
        'date_range': {'start': '2026-01-01', 'end': '2026-01-03'}}, transactions=ROWS[:3])
    fact = result['verified_facts'][-1]
    assert fact['total'] == 100
    assert fact['gross_purchases'] == 110
    assert fact['refunds'] == 10
    assert fact['date_range']['end'] == '2026-01-03'
    assert '100.00' in result['text']
    assert '110.00' in result['text'] and '10.00' in result['text']
    assert '$90' not in result['text']
    assert result['source'] == 'calculated'
    assert client.chat.completions.create.call_count == 1


def test_fallback_only_attaches_calculated_facts_to_financial_questions(monkeypatch):
    monkeypatch.setattr(ai_coach, 'get_client', lambda: None)
    context = {'income': 2000, 'spent': 100, 'saved': 1900}
    assert ai_coach.coach_chat('hi', context)['verified_facts'] == []
    assert ai_coach.coach_chat('What is an ETF?', context)['verified_facts'] == []
    result = ai_coach.coach_chat('How much did I spend?', context)
    assert result['verified_facts'][0]['spent'] == 100


def test_commit_does_not_mutate_cached_snapshot(monkeypatch):
    original = {'summary_json': {'metrics': {'total_spent': 100}, 'insight': {'text': 'old'}}}
    monkeypatch.setattr(db, 'get_latest_snapshot', lambda *args: original)
    client = Mock()
    client.rpc.return_value.execute.return_value.data = {'revision': NEW}
    db.commit_edit(client, 'u', REV, changes={'budget_targets': {'Groceries': 500}})
    assert 'budget_targets' not in original['summary_json']
    assert 'insight' in original['summary_json']
    sent = client.rpc.call_args.args[1]
    assert sent['p_expected_revision'] == REV
    assert sent['p_summary']['budget_targets'] == {'Groceries': 500}
    assert 'insight' not in sent['p_summary']


def test_readiness_demands_the_new_capability(monkeypatch):
    monkeypatch.setattr(db, 'is_configured', lambda: True)
    client = Mock()
    client.rpc.return_value.execute.return_value.data = True
    monkeypatch.setattr(db, 'get_client', lambda: client)
    monkeypatch.setenv('FINIO_ENV', 'test')
    assert main.readiness() == {'status': 'ready'}
    assert [call.args for call in client.rpc.call_args_list] == [('finio_ready_v8', {}), ('finio_security_ready', {})]


def test_category_only_transfer_edit_does_not_hide_the_flow_question(tmp_path):
    path = tmp_path / 'synthetic.csv'
    path.write_text('Date,Description,Amount\n2026-02-01,PAYID ALEX,-25\n')
    result = run_full_pipeline(path, overrides=[{'match':'PAYID ALEX', 'category':'Other'}])
    assert result['review']['count'] == 1
    assert result['pending_questions'][0]['kind'] == 'flow'
