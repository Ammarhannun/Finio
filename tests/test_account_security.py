"""Sensitive operations fail closed before any financial read or write."""
from unittest.mock import Mock
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
from fastapi.testclient import TestClient

import main
from api.deps import AuthUser, get_current_user
from modules import db
from scripts.check_service import check_service


@pytest.fixture
def sensitive_client(monkeypatch):
    main.app.dependency_overrides[get_current_user] = lambda: AuthUser('synthetic', 'synthetic-token')
    main.app.dependency_overrides[main.upload_user] = lambda: AuthUser('synthetic', 'synthetic-token')
    client = Mock()
    monkeypatch.setattr(db, 'get_client', lambda *args: client)
    main._RATE.clear()
    yield TestClient(main.app), client
    main.app.dependency_overrides.clear()


@pytest.mark.parametrize('state,status,code', [
    ({'session_active': False, 'mfa_satisfied': True, 'recent_auth': True}, 401, None),
    ({'session_active': True, 'mfa_satisfied': False, 'recent_auth': True}, 403, 'mfa_required'),
    ({'session_active': True, 'mfa_satisfied': True, 'recent_auth': False}, 403, 'reauth_required'),
])
@pytest.mark.parametrize('path', ['/data/export', '/data/delete'])
def test_sensitive_routes_refuse_before_financial_access(sensitive_client, monkeypatch, state, status, code, path):
    browser, database = sensitive_client
    monkeypatch.setattr(db, 'security_status', lambda _: state)
    profile = Mock(side_effect=AssertionError('Financial data read before proof'))
    monkeypatch.setattr(db, 'get_user_profile', profile)
    response = browser.get(path) if path.endswith('export') else browser.post(path, json={'confirmation': 'DELETE'})
    assert response.status_code == status
    if code:
        assert response.json()['detail']['code'] == code
    profile.assert_not_called()
    database.rpc.assert_not_called()


def test_unavailable_security_checks_never_return_private_errors_or_delete(sensitive_client, monkeypatch):
    browser, database = sensitive_client
    monkeypatch.setattr(db, 'security_status', Mock(side_effect=RuntimeError('private database detail')))
    response = browser.post('/data/delete', json={'confirmation': 'DELETE'})
    assert response.status_code == 503
    assert 'private database' not in response.text
    database.rpc.assert_not_called()


def test_verified_recent_session_can_export_and_delete(sensitive_client, monkeypatch):
    browser, database = sensitive_client
    monkeypatch.setattr(db, 'security_status', lambda _: {'session_active': True, 'mfa_satisfied': True, 'recent_auth': True})
    monkeypatch.setattr(db, 'get_user_profile', lambda *_: {'age': 30})
    monkeypatch.setattr(db, 'paged', lambda *_: [])
    assert browser.get('/data/export').status_code == 200
    assert browser.post('/data/delete', json={'confirmation': 'DELETE'}).json()['deleted'] is True
    database.rpc.assert_called_once_with('finio_delete_data', {})


def test_export_contains_complete_rows_for_only_the_verified_owner(sensitive_client, monkeypatch):
    from tests.test_schema_compatibility import Client
    browser, _ = sensitive_client
    own = {'id': 'row-a', 'user_id': 'synthetic', 'date': '2026-06-01',
           'merchant': 'SYNTHETIC SALARY', 'amount': 2000, 'flow': 'income'}
    other = {'id': 'row-b', 'user_id': 'other', 'merchant': 'OTHER ACCOUNT', 'amount': 999}
    database = Client({'users': [{'id': 'synthetic', 'age': 30}],
                       'transactions': [own, other]}, legacy=False)
    monkeypatch.setattr(db, 'get_client', lambda *_: database)
    monkeypatch.setattr(db, 'security_status', lambda _: {
        'session_active': True, 'mfa_satisfied': True, 'recent_auth': True})
    response = browser.get('/data/export')
    assert response.status_code == 200
    assert response.json()['data']['transactions'] == [own]


@pytest.mark.parametrize('payload', [None, [], True, {}, {'session_active': 'true', 'mfa_satisfied': True, 'recent_auth': True}])
def test_invalid_database_security_state_fails_closed(payload):
    client = Mock()
    client.rpc.return_value.execute.return_value.data = payload
    with pytest.raises(ValueError):
        db.security_status(client)


def test_failed_requests_do_not_log_private_urls_or_query_values(sensitive_client, caplog):
    browser, _ = sensitive_client
    browser.get('/private-sensitive-name?token=synthetic-secret')
    assert 'route=[unmatched] status=404' in caplog.text
    assert 'private-sensitive-name' not in caplog.text
    assert 'synthetic-secret' not in caplog.text


@pytest.mark.parametrize('url', ['https://user:secret@example.invalid', 'http://example.invalid',
    'https://example.invalid/?token=secret', 'https://example.invalid/private'])
def test_monitor_rejects_credentials_and_unexpected_targets(url):
    with pytest.raises(ValueError):
        check_service(url)


def test_readiness_rejects_missing_security_rules_even_when_financial_schema_is_ready(monkeypatch):
    monkeypatch.setenv('FINIO_ENV','test')
    monkeypatch.setattr(db,'is_configured',lambda:True)
    client=Mock()
    client.rpc.side_effect=lambda name,_: SimpleNamespace(execute=lambda:SimpleNamespace(data=name=='finio_ready_v7'))
    monkeypatch.setattr(db,'get_client',lambda:client)
    assert TestClient(main.app).get('/ready').status_code==503


def test_monitor_distinguishes_liveness_from_readiness_and_ignores_response_bodies():
    class Handler(BaseHTTPRequestHandler):
        ready=True
        def do_GET(self):
            self.send_response(200 if self.path=='/' or self.ready else 503)
            self.end_headers()
            self.wfile.write(b'private response body must not appear in monitor output')
        def log_message(self,*args): pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        url='http://127.0.0.1:'+str(server.server_port)
        assert check_service(url)['healthy'] is True
        Handler.ready=False
        result=check_service(url)
        assert result=={'healthy':False,'checks':{'liveness':200,'readiness':503}}
        assert 'private response' not in str(result)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
