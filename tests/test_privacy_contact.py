"""Exercise the public form's trust boundaries without sending any real mail."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

from fastapi.testclient import TestClient
import httpx
import pytest

import main
from api import privacy_contact as contact

REAL_CLIENT = httpx.Client
ORIGIN = 'https://finio.example.invalid'


@pytest.fixture
def setup(monkeypatch):
    values = {
        'FINIO_ENV': 'production', 'FINIO_ORIGINS': ORIGIN,
        'FINIO_CONTACT_TO': 'private-operator@example.invalid',
        'FINIO_CONTACT_FROM': 'privacy@sender.example.invalid',
        'FINIO_CONTACT_RESEND_KEY': 'synthetic-mail-credential',
        'FINIO_CONTACT_TURNSTILE_SITE_KEY': 'synthetic-public-site-key',
        'FINIO_CONTACT_TURNSTILE_SECRET': 'synthetic-challenge-credential',
        'WEB_CONCURRENCY': '1', 'UVICORN_WORKERS': '1',
    }
    for key, value in values.items(): monkeypatch.setenv(key, value)
    contact._ATTEMPTS.clear()
    contact._CLIENTS.clear()
    sent = []
    monkeypatch.setattr(contact, '_verify', lambda *args: True)
    monkeypatch.setattr(contact, '_deliver', lambda config, payload, reference:
                        sent.append((payload.email, payload.topic, reference)))
    monkeypatch.setattr(main.db, 'get_client', lambda *a, **k:
                        pytest.fail('Privacy form accessed the financial database'))
    return TestClient(main.app, raise_server_exceptions=False), sent


def body(**changes):
    return {'email': 'requester@example.invalid', 'topic': 'delete',
            'message': 'Please explain how I can delete my saved data.',
            'token': 'synthetic-challenge-response', **changes}


def post(client, data=None, **kwargs):
    headers = {'Origin': ORIGIN, **kwargs.pop('headers', {})}
    return client.post('/privacy/contact', json=body() if data is None else data,
                       headers=headers, **kwargs)


def test_public_config_and_acknowledgement_never_disclose_operator_or_credentials(setup):
    client, sent = setup
    config = client.get('/privacy/contact/config')
    assert config.status_code == 200 and config.json() == {
        'enabled': True, 'site_key': 'synthetic-public-site-key'}
    response = post(client)
    assert response.status_code == 202 and response.json()['received'] is True
    assert len(sent) == 1 and sent[0][1] == 'delete'
    for output in (config.text, response.text):
        assert 'private-operator' not in output and 'credential' not in output
        assert 'requester@' not in output and 'delete my saved' not in output
    assert response.headers['cache-control'].startswith('no-store')


@pytest.mark.parametrize('key', ['FINIO_CONTACT_TO', 'FINIO_CONTACT_FROM',
    'FINIO_CONTACT_RESEND_KEY', 'FINIO_CONTACT_TURNSTILE_SITE_KEY', 'FINIO_CONTACT_TURNSTILE_SECRET'])
def test_incomplete_configuration_fails_closed_without_mail(setup, monkeypatch, key):
    client, sent = setup
    monkeypatch.delenv(key)
    assert client.get('/privacy/contact/config').json() == {'enabled': False, 'site_key': None}
    assert post(client).status_code == 503 and not sent


@pytest.mark.parametrize('origin', ['', 'null', 'https://attacker.invalid',
    ORIGIN + '.attacker.invalid', ORIGIN + '/path'])
def test_missing_or_unapproved_origin_cannot_submit(setup, origin):
    client, sent = setup
    assert post(client, headers={'Origin': origin}).status_code == 403
    assert not sent


@pytest.mark.parametrize('change', [
    {'email': 'visitor@example.invalid\r\nBcc: victim@example.invalid'},
    {'email': 'one@example.invalid,two@example.invalid'},
    {'email': 'Name <visitor@example.invalid>'},
    {'to': 'victim@example.invalid'},
    {'topic': 'Injected subject\r\nBcc: victim@example.invalid'},
    {'message': 'x' * 1501}, {'token': 'x' * 2049}, {'message': 'short'},
    {'message': 'synthetic\x00hidden text'}, {'token': None},
])
def test_relay_header_injection_and_oversized_fields_are_rejected_without_echo(setup, change):
    client, sent = setup
    response = post(client, body(**change))
    assert response.status_code == 422 and not sent
    assert 'victim@' not in response.text and 'synthetic' not in response.text


def test_bad_challenge_and_honeypot_never_send_mail(setup, monkeypatch):
    client, sent = setup
    monkeypatch.setattr(contact, '_verify', lambda *args: False)
    assert post(client).status_code == 400
    assert post(client, body(website='bot-filled-field')).status_code == 429
    assert not sent


def test_mail_failure_is_honest_and_keeps_provider_details_private(setup, monkeypatch):
    client, sent = setup
    def failed(*args): raise RuntimeError('private-operator@example.invalid synthetic-mail-credential')
    monkeypatch.setattr(contact, '_deliver', failed)
    response = post(client)
    assert response.status_code == 503 and 'could not confirm delivery' in response.text
    assert 'private-operator' not in response.text and 'credential' not in response.text
    assert not sent
    monkeypatch.setattr(contact, '_deliver', lambda *args: sent.append(True))
    assert post(client).status_code == 202 and sent == [True]


def test_forwarding_header_spoofing_does_not_reset_rate_limit(setup):
    client, sent = setup
    for n in range(3):
        assert post(client, headers={'X-Forwarded-For': f'192.0.2.{n}'}).status_code == 202
    assert post(client, headers={'X-Forwarded-For': '192.0.2.99'}).status_code == 429
    assert len(sent) == 3


def test_global_quota_bounds_distinct_clients_and_expires(monkeypatch):
    contact._ATTEMPTS.clear(); contact._CLIENTS.clear()
    now = [100000.0]
    monkeypatch.setattr(contact.time, 'monotonic', lambda: now[0])
    for n in range(20): contact._admit(str(n))
    with pytest.raises(main.HTTPException) as blocked: contact._admit('new-client')
    assert blocked.value.status_code == 429
    now[0] += 86401
    contact._admit('new-client')
    assert len(contact._CLIENTS) == 1 and len(contact._ATTEMPTS) == 1


def test_only_one_outbound_request_at_a_time_without_blocking_config(setup, monkeypatch):
    client, sent = setup
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr(contact, '_verify', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, client)
        try:
            assert entered.wait(3)
            assert post(client).status_code == 429
            assert client.get('/privacy/contact/config').status_code == 200
        finally: release.set()
        assert first.result().status_code == 202
    assert len(sent) == 1


@pytest.mark.parametrize('key,value', [
    ('FINIO_CONTACT_TURNSTILE_SECRET', '1x0000000000000000000000000000000AA'),
    ('FINIO_CONTACT_TURNSTILE_SITE_KEY', '1x00000000000000000000AA'),
    ('WEB_CONCURRENCY', '2'), ('UVICORN_WORKERS', '2'),
])
def test_dummy_protection_and_multiworker_configuration_are_disabled_in_production(setup, monkeypatch, key, value):
    client, sent = setup
    monkeypatch.setenv(key, value)
    assert client.get('/privacy/contact/config').json()['enabled'] is False
    assert post(client).status_code == 503 and not sent


def test_small_endpoint_body_limit_applies_before_validation(setup):
    client, sent = setup
    response = client.post('/privacy/contact', content=b'x' * (12 * 1024 + 1),
                           headers={'Origin': ORIGIN, 'Content-Type': 'application/json'})
    assert response.status_code == 413 and not sent


def test_turnstile_requires_exact_hostname_action_and_true_success(monkeypatch):
    result = {'success': True, 'hostname': 'finio.example.invalid', 'action': 'privacy-contact'}
    def handle(request):
        assert str(request.url) == 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
        assert json.loads(request.content) == {'secret': 'synthetic-secret', 'response': 'synthetic-token'}
        return httpx.Response(200, json=result)
    def client(**options):
        assert options['follow_redirects'] is False and options['trust_env'] is False
        return REAL_CLIENT(**options, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(contact.httpx, 'Client', client)
    verify = lambda: contact._verify('synthetic-token', 'synthetic-secret', 'finio.example.invalid')
    assert verify()
    for changed in [{'success': False}, {'success': 'true'}, {'hostname': 'attacker.invalid'},
                    {'action': 'different-form'}, {'action': None}]:
        original = dict(result); result.update(changed)
        assert not verify()
        result.clear(); result.update(original)


def test_delivery_is_fixed_destination_plain_text_and_redacts_recognisable_credentials(setup, monkeypatch):
    client, sent = setup
    captured = []
    def handle(request):
        assert str(request.url) == 'https://api.resend.com/emails'
        assert request.headers['authorization'] == 'Bearer synthetic-mail-credential'
        assert request.headers['idempotency-key'] == 'finio-privacy-reference123'
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'synthetic-message-id'})
    def fake_client(**options):
        assert not options['follow_redirects'] and not options['trust_env']
        return REAL_CLIENT(**options, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(contact.httpx, 'Client', fake_client)
    config = contact._configuration()
    payload = contact.PrivacyContact(**body(message='Please help. password=synthetic-private-value <b>plain text</b>'))
    # Exercise the real implementation rather than the fixture's mail stub.
    REAL_DELIVER(config, payload, 'reference123')
    assert len(captured) == 1
    message = captured[0]
    assert message['to'] == ['private-operator@example.invalid']
    assert message['reply_to'] == 'requester@example.invalid'
    assert 'html' not in message and 'attachments' not in message
    assert 'synthetic-private-value' not in message['text']
    assert '[REDACTED_SECRET]' in message['text'] and 'not verified' in message['text']


REAL_DELIVER = contact._deliver
