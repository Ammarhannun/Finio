"""Launch regressions. Offline by default; never writes to Supabase or calls AI."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

os.environ["FINIO_DISABLE_LLM"] = "1"

import pytest
from fastapi.testclient import TestClient
import main
from api.deps import AuthUser, get_current_user
from modules import db
from modules.pipeline import analyze_stored, run_full_pipeline, recompute_for_goal

CSV = b"Date,Amount,Description,Balance\n01/06/2026,2000,SALARY,2000\n02/06/2026,-100,COLES,1900\n03/06/2026,30,COLES REFUND,1930\n"


@pytest.fixture
def result(tmp_path):
    file = tmp_path / 'statement.csv'
    file.write_bytes(CSV)
    return run_full_pipeline(file, period='all')


@pytest.fixture
def client(monkeypatch):
    main.app.dependency_overrides[get_current_user] = lambda: AuthUser('launch-test', 'fake-token')
    main._RATE.clear()
    main._VIEW_CACHE.clear()
    monkeypatch.setattr(db, 'get_client', Mock(return_value=Mock()))
    for name, value in {
 'get_overrides': [], 'get_custom_categories': [], 'get_llm_categories': {},
 'get_user_profile': {'age': 34, 'income_bracket': 'middle'},
 'get_budget_targets': {'Groceries': 400},
 'get_goal': {'target_amount': 6000, 'target_date': '2027-12-01'},
 'get_all_transactions': [], 'get_embedded_merchants': set(),
 'persist_analysis': {},
    }.items():
        monkeypatch.setattr(db, name, Mock(return_value=value))
    yield TestClient(main.app, raise_server_exceptions=False)
    main.app.dependency_overrides.clear()
    main._RATE.clear()


def test_integer_amounts_are_strict_json(result):
    json.dumps(result, allow_nan=False)
    assert result['metrics']['total_income'] == 2000
    assert result['metrics']['total_spent'] == 70


def test_refunds_and_balance_survive_database_roundtrip(result):
    saved = db._tx_rows('launch-test', result['all_transactions'])
    restored = analyze_stored(saved, period='all')
    for key in ('total_income', 'total_spent', 'net_saved', 'latest_balance'):
        assert restored['metrics'][key] == result['metrics'][key]
    assert saved[-1]['flow'] == 'expense'
    assert saved[-1]['balance'] == 1930


@pytest.mark.parametrize('cap', [97, 500, 1000])
def test_pagination_does_not_truncate_at_server_cap(cap):
    rows = [{'id': i} for i in range(2501)]
    class Query:
        def range(self, start, end):
            self.data = rows[start:min(end + 1, start + cap)]
            return self
        def execute(self): return self
    assert db.paged(Query) == rows


@pytest.mark.parametrize('route', ['/analyze', '/analyze/preview'])
def test_anonymous_upload_never_calls_pipeline(monkeypatch, route):
    monkeypatch.setattr(db, 'is_configured', lambda: True)
    pipeline = Mock()
    monkeypatch.setattr(main, 'run_full_pipeline', pipeline)
    response = TestClient(main.app).post(route, files={'file': ('s.csv', CSV)})
    assert response.status_code == 401
    pipeline.assert_not_called()


def test_preview_is_read_only(client):
    response = client.post('/analyze/preview', files={'file': ('s.csv', CSV)})
    assert response.status_code == 200
    assert response.json()['count'] == 3
    assert response.json()['money_out'] == 100
    assert response.json()['money_in'] == 2030
    assert response.json()['best_effort_pdf'] is False
    db.persist_analysis.assert_not_called()


@pytest.mark.parametrize('contents', [b'Date,Amount,Description\nbad,5,test', b'Date,Amount,Description\n01/06/2026,NaN,test', b'not a statement'])
def test_bad_statement_returns_actionable_422(client, contents):
    for route in ('/analyze', '/analyze/preview'):
        assert client.post(route, files={'file': ('bad.csv', contents)}).status_code == 422
    db.persist_analysis.assert_not_called()


def test_upload_preserves_preferences(client, monkeypatch, result):
    pipeline = Mock(return_value=result)
    monkeypatch.setattr(main, 'run_full_pipeline', pipeline)
    response = client.post('/analyze', files={'file': ('s.csv', CSV)})
    assert response.status_code == 200, response.text
    settings = pipeline.call_args.kwargs
    assert settings['age'] == 34
    assert settings['budget_targets'] == {'Groceries': 400}
    assert settings['goal_amount'] == 6000
    assert settings['goal_date'] == '2027-12-01'
    assert db.persist_analysis.call_args.kwargs['age'] == 34


def test_replacement_requires_explicit_confirmation(client, monkeypatch):
    db.get_all_transactions.return_value = [{'existing': True}]
    pipeline = Mock()
    monkeypatch.setattr(main, 'run_full_pipeline', pipeline)
    assert client.post('/analyze', files={'file': ('s.csv', CSV)}).status_code == 409
    pipeline.assert_not_called()
    db.persist_analysis.assert_not_called()


def test_settings_failure_does_not_overwrite(client):
    db.get_goal.side_effect = RuntimeError('database unavailable')
    assert client.post('/analyze', files={'file': ('s.csv', CSV)}).status_code == 503
    db.persist_analysis.assert_not_called()


def test_save_failure_is_not_success(client):
    db.persist_analysis.side_effect = RuntimeError('write failed')
    response = client.post('/analyze', files={'file': ('s.csv', CSV)})
    assert response.status_code == 503
    assert 'previous data is unchanged' in response.json()['detail']


def test_health_remains_responsive_during_analysis(client, monkeypatch, result):
    entered, release = Event(), Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(main, 'run_full_pipeline', blocked)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(client.post, '/analyze', files={'file': ('s.csv', CSV)})
        try:
            assert entered.wait(5)
            assert client.get('/').status_code == 200
            assert client.post('/analyze', files={'file': ('s.csv', CSV)}).status_code == 429
        finally:
            release.set()
        assert pending.result().status_code == 200


def test_atomic_persistence_uses_one_rpc(monkeypatch, result):
    storage = Mock()
    monkeypatch.setattr(db, 'get_user', lambda token: SimpleNamespace(id='u1', email='test@example.invalid'))
    monkeypatch.setattr(db, 'get_client', lambda token: storage)
    monkeypatch.setattr(db, 'get_streak', lambda *args: None)
    db.persist_analysis('fake', result, goal_amount=6000, goal_date='2027-12-01', age=34,
                        budget_targets={'Groceries': 400})
    storage.table.assert_not_called()
    storage.rpc.assert_called_once()
    name, payload = storage.rpc.call_args.args
    assert name == 'finio_commit_analysis'
    assert payload['p_summary']['budget_targets'] == {'Groceries': 400}
    assert payload['p_rows'][-1]['flow'] == 'expense'
    json.dumps(payload, allow_nan=False)


def test_delete_requires_exact_confirmation(client):
    assert client.post('/data/delete', json={'confirmation': 'delete'}).status_code == 422
    db.get_client.assert_not_called()


def test_readiness_fails_closed(monkeypatch):
    monkeypatch.setattr(db, 'is_configured', lambda: True)
    monkeypatch.setattr(db, 'get_client', Mock(side_effect=RuntimeError('missing migration')))
    assert TestClient(main.app).get('/ready').status_code == 503


def test_oversized_content_length_rejected_before_work(client):
    assert client.post('/analyze', content=b'x', headers={'Content-Length': '12000000'}).status_code == 413
    db.persist_analysis.assert_not_called()


def test_chunked_request_cannot_bypass_limit():
    import asyncio
    from api.body_limit import BodyLimitMiddleware
    called, sent = [], []
    chunks = iter([{'type': 'http.request', 'body': b'123456', 'more_body': True},
                   {'type': 'http.request', 'body': b'78901', 'more_body': False}])
    async def inner(*args): called.append(True)
    async def receive(): return next(chunks)
    async def send(message): sent.append(message)
    asyncio.run(BodyLimitMiddleware(inner, upload_limit=10)(
        {'type': 'http', 'method': 'POST', 'path': '/analyze', 'headers': []}, receive, send))
    assert not called
    assert sent[0]['status'] == 413


@pytest.mark.parametrize('value, enabled', [('0', True), ('', True), ('1', False), ('true', False)])
def test_llm_flag_parses_boolean_values(monkeypatch, value, enabled):
    from modules import ai_coach, embeddings, llm_categoriser
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-test-only')
    monkeypatch.setenv('FINIO_DISABLE_LLM', value)
    assert ai_coach.has_llm() is enabled
    assert llm_categoriser.has_llm() is enabled
    assert embeddings.has_embeddings() is enabled


def test_same_user_mutations_are_serialized_and_guard_releases():
    from api.deps import mutation_guard
    with mutation_guard('one'):
        with pytest.raises(main.HTTPException) as error:
            with mutation_guard('one'): pass
        assert error.value.status_code == 409
        with mutation_guard('two'): pass
    with mutation_guard('one'): pass


# ── Upload hardening ────────────────────────────────────────────────────────
# An upload is untrusted input, and its extension is just a string the browser
# sent. These pin the refusals a statement upload must make.

def test_anonymous_upload_is_rejected_before_the_body_is_read():
    """No auth, no parsing. Anonymous upload is forbidden; /demo exists for that."""
    main.app.dependency_overrides.clear()
    response = TestClient(main.app, raise_server_exceptions=False).post(
        '/analyze', files={'file': ('statement.csv', CSV, 'text/csv')})
    assert response.status_code == 401


def test_extension_allowlist(client):
    response = client.post('/analyze', files={'file': ('payload.exe', b'MZ\x90\x00', 'application/octet-stream')})
    assert response.status_code == 400
    db.persist_analysis.assert_not_called()


@pytest.mark.parametrize('name,payload', [
    # Binary content renamed .csv is never executed, but it must be refused
    # clearly rather than surfacing as a parser traceback.
    ('zip', b'PK\x03\x04\x00\x00\x00binary\x00payload'),
    ('elf', b'\x7fELF\x02\x01\x01\x00' + b'\x00' * 40),
])
def test_binary_disguised_as_csv_is_refused(client, name, payload):
    response = client.post('/analyze', files={'file': ('statement.csv', payload, 'text/csv')})
    assert response.status_code == 422
    assert 'binary' in response.json()['detail'].lower()
    db.persist_analysis.assert_not_called()


def test_row_cap_refuses_rather_than_truncating(client):
    """Truncating would silently report the wrong finances, so it must refuse.

    The cap is checked while reading, so a 10 MB file is never fully loaded.
    """
    from config import MAX_TRANSACTION_ROWS
    rows = b''.join(b'2026-01-03,SHOP %d,-1.00\n' % i for i in range(MAX_TRANSACTION_ROWS + 5))
    response = client.post('/analyze', files={
        'file': ('statement.csv', b'Date,Description,Amount\n' + rows, 'text/csv')})
    assert response.status_code == 422
    assert str(MAX_TRANSACTION_ROWS) in response.json()['detail'].replace(',', '')
    db.persist_analysis.assert_not_called()


def test_statement_just_under_the_cap_is_accepted(client):
    from config import MAX_TRANSACTION_ROWS
    rows = b''.join(b'2026-01-%02d,SHOP %d,-1.00\n' % ((i % 28) + 1, i)
                    for i in range(MAX_TRANSACTION_ROWS - 1))
    response = client.post('/analyze', files={
        'file': ('statement.csv', b'Date,Description,Amount\n' + rows, 'text/csv')})
    assert response.status_code == 200


def test_empty_upload_is_refused(client):
    response = client.post('/analyze', files={'file': ('statement.csv', b'', 'text/csv')})
    assert response.status_code == 400
    db.persist_analysis.assert_not_called()


def test_parse_failures_stay_generic_but_our_own_limits_do_not(client):
    """Don't leak pandas/pdfminer internals; do explain our own refusals."""
    unparseable = client.post('/analyze', files={
        'file': ('statement.csv', b'this is not a statement\njust prose\n', 'text/csv')})
    assert unparseable.status_code == 422
    assert 'date, description and amount' in unparseable.json()['detail']

    ours = client.post('/analyze', files={
        'file': ('statement.csv', b'Date,Description,Amount\n2026-01-03,X,-999999999999\n', 'text/csv')})
    assert ours.status_code == 422
    assert 'supported range' in ours.json()['detail']


def test_uploads_are_not_left_on_disk(client, tmp_path, monkeypatch):
    """A failed parse must still remove the temporary file."""
    import tempfile
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    client.post('/analyze', files={'file': ('statement.csv', b'PK\x03\x04\x00bin\x00', 'text/csv')})
    client.post('/analyze', files={'file': ('statement.csv', CSV, 'text/csv')})
    assert list(tmp_path.iterdir()) == []


def test_api_responses_are_never_cached_or_framed(client):
    response = client.get('/')
    assert 'no-store' in response.headers['cache-control']
    assert response.headers['x-frame-options'] == 'DENY'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert "default-src 'none'" in response.headers['content-security-policy']
    assert response.headers['referrer-policy'] == 'no-referrer'
    # HSTS only behind a real certificate, or local dev gets pinned to HTTPS.
    assert 'strict-transport-security' not in response.headers


def test_hsts_is_sent_behind_a_proxy_terminating_tls(client):
    response = client.get('/', headers={'x-forwarded-proto': 'https'})
    assert 'max-age=31536000' in response.headers['strict-transport-security']
