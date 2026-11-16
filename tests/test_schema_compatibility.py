"""Migration 004 stays readable while migration 005 is staged; no live data."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import main
from api.deps import AuthUser, get_current_user
from modules import db
from modules.pipeline import analyze_stored


class SchemaError(Exception):
    def __init__(self, code='42703', message='column snapshots.revision does not exist'):
        self.code, self.message = code, message
        super().__init__(message)


class Query:
    def __init__(self, client, table):
        self.client, self.name = client, table
        self.columns = '*'
        self.filters = []
        self.lo, self.hi = 0, None
        self.ordering = []
    def select(self, columns): self.columns = columns; return self
    def eq(self, key, value): self.filters.append((key, value, False)); return self
    def neq(self, key, value): self.filters.append((key, value, True)); return self
    def order(self, key, desc=False): self.ordering.append((key, desc)); return self
    def limit(self, n): self.hi = n; return self
    def range(self, lo, hi): self.lo, self.hi = lo, hi + 1; return self
    def execute(self):
        self.client.calls.append((self.name, self.columns, self.filters[:]))
        if self.client.failure:
            raise self.client.failure
        missing = {'revision', 'undo_json', 'verified_facts'} & set(self.columns.replace(' ', '').split(','))
        if self.client.legacy and missing:
            raise SchemaError(message=f'column {self.name}.{sorted(missing)[0]} does not exist')
        rows = deepcopy(self.client.tables.get(self.name, []))
        for key, value, negate in self.filters:
            rows = [r for r in rows if (r.get(key) != value if negate else r.get(key) == value)]
        for key, desc in reversed(self.ordering):
            rows.sort(key=lambda row: str(row.get(key, '')), reverse=desc)
        rows = rows[self.lo:self.hi]
        fields = self.columns.replace(' ', '').split(',')
        return SimpleNamespace(data=[r if self.columns.strip() == '*' else
                                    {k: r[k] for k in fields if k in r} for r in rows])


class Client:
    def __init__(self, tables=None, legacy=True, failure=None):
        self.tables, self.legacy, self.failure = tables or {}, legacy, failure
        self.calls = []
        self.rpc = Mock(side_effect=AssertionError('Legacy reads must never invoke a write RPC'))
    def table(self, name): return Query(self, name)


@pytest.fixture
def client(monkeypatch):
    rows = [
        {'user_id':'owner', 'date':'2026-02-01', 'amount':2000, 'merchant':'SALARY', 'flow':'income', 'category':'Income'},
        {'user_id':'owner', 'date':'2026-02-02', 'amount':-110, 'merchant':'COLES', 'flow':'expense', 'category':'Groceries'},
        {'user_id':'owner', 'date':'2026-02-03', 'amount':10, 'merchant':'COLES REFUND', 'flow':'expense', 'category':'Groceries'},
        {'user_id':'other', 'date':'2026-02-01', 'amount':99999, 'merchant':'PRIVATE OTHER', 'flow':'income', 'category':'Income'},
    ]
    result = analyze_stored(rows[:3])
    summary = {k: result[k] for k in ('metrics','analysis','bills','anomalies','averages','forecast','spend_forecast','budgets','invest','personality','context','goal_recommendation')}
    summary.update({'custom_categories':['Trips'], 'overrides':[], 'pending_questions':[]})
    c = Client({
        'snapshots':[{'user_id':'owner','month':'2026-02-01','summary_json':summary,'updated_at':'2026-02-04T00:00:00Z'},
                     {'user_id':'other','month':'2026-02-01','summary_json':{'metrics':{'total_income':99999}},'updated_at':'2026-02-05T00:00:00Z'}],
        'users':[{'id':'owner','email':'synthetic@example.invalid','age':34,'income_bracket':'full_time'}],
        'transactions':rows,
        'chat_history':[
            {'user_id':'owner','chat_id':'chosen','role':'assistant','message':'Chosen history','timestamp':'2026-02-01T00:00:00Z'},
            {'user_id':'owner','chat_id':'different','role':'assistant','message':'Different history','timestamp':'2026-02-02T00:00:00Z'},
            {'user_id':'other','chat_id':'chosen','role':'assistant','message':'Other owner','timestamp':'2026-02-03T00:00:00Z'},
        ],
    })
    monkeypatch.setattr(db, 'get_client', lambda *args: c)
    monkeypatch.setattr(db, 'get_user', lambda *args: SimpleNamespace(email='synthetic@example.invalid'))
    main.app.dependency_overrides[get_current_user] = lambda: AuthUser('owner','synthetic-token')
    main._VIEW_CACHE.clear(); main._RATE.clear()
    yield c, TestClient(main.app, raise_server_exceptions=False)
    main.app.dependency_overrides.clear()
    main._VIEW_CACHE.clear(); main._RATE.clear()


def test_saved_dashboard_and_period_work_before_upgrade(client):
    c, http = client
    stored = http.get('/dashboard')
    assert stored.status_code == 200, stored.text
    scoped = http.get('/dashboard?period=monthly&month=2026-02')
    assert scoped.status_code == 200, scoped.text
    for response in (stored, scoped):
        data = response.json()
        assert data['metrics']['total_income'] == 2000
        assert data['metrics']['total_spent'] == 100
        assert data['metrics']['net_saved'] == 1900
        assert data['editing_available'] is False
        assert data['can_undo'] is False
    assert c.tables['snapshots'][0]['summary_json']['metrics']['total_spent'] == 100
    c.rpc.assert_not_called()


@pytest.mark.parametrize('path,key', [('/profile','email'),('/transactions','transactions'),('/overrides','overrides'),('/budgets','budgets')])
def test_other_saved_views_work_before_upgrade(client, path, key):
    _, http = client
    response = http.get(path)
    assert response.status_code == 200, response.text
    assert key in response.json()
    assert response.json()['editing_available'] is False
    if path == '/transactions':
        assert len(response.json()['transactions']) == 3
        assert all(t['merchant'] != 'PRIVATE OTHER' for t in response.json()['transactions'])


def test_chat_facts_fallback_keeps_the_requested_conversation_and_user(client):
    c, _ = client
    rows = db.get_chat_history(c, 'owner', chat_id='chosen')
    assert [r['message'] for r in rows] == ['Chosen history']
    attempts = [filters for table, _, filters in c.calls if table == 'chat_history']
    assert len(attempts) == 2
    assert all(('user_id','owner',False) in filters and ('chat_id','chosen',False) in filters for filters in attempts)


@pytest.mark.parametrize('failure', [
    SchemaError('42501','permission denied'), SchemaError('PGRST301','JWT expired'),
    SchemaError('42703','column snapshots.month does not exist'), RuntimeError('Connection unavailable'),
])
def test_unrelated_errors_never_fall_back_or_widen_scope(failure):
    c = Client(failure=failure)
    with pytest.raises(type(failure)):
        db.get_latest_snapshot(c, 'owner')
    assert len(c.calls) == 1
    assert c.calls[0][2] == [('user_id','owner',False)]


def test_ready_schema_keeps_revision_and_undo():
    c = Client({'snapshots':[{'user_id':'owner','month':'2026-02-01','summary_json':{},'revision':'current','undo_json':{'label':'Budget'}}]}, legacy=False)
    state = db.edit_state(c, 'owner')
    assert state == {'revision':'current','can_undo':True,'undo_label':'Budget','editing_available':True}
    assert len(c.calls) == 1


@pytest.mark.parametrize('path,body', [('/budgets',{'targets':{'Groceries':400}}),('/profile',{'age':35}),('/reanalyze',{})])
def test_writes_stay_disabled_in_compatibility_mode(client, path, body):
    c, http = client
    response = http.post(path,json=body)
    assert response.status_code == 503, response.text
    assert 'service update' in response.text
    c.rpc.assert_not_called()


def test_old_snapshot_insight_is_read_only_and_does_not_send_finances_to_ai(client,monkeypatch):
    c, http = client
    generate = Mock(side_effect=AssertionError('Do not send data to AI during legacy read recovery'))
    monkeypatch.setattr(main,'generate_insight',generate)
    response = http.get('/insight?period=monthly')
    assert response.status_code == 200
    assert response.json()['source'] == 'saved'
    generate.assert_not_called(); c.rpc.assert_not_called()


def test_reading_does_not_mutate_original_snapshot(client):
    c, http = client
    before = deepcopy(c.tables)
    assert http.get('/dashboard?period=monthly').status_code == 200
    assert c.tables == before
