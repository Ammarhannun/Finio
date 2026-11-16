"""Synthetic security boundaries. No live accounts, statements or AI requests."""
import asyncio
import json
import re
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from api.deps import AuthUser, _parse_bearer, get_current_user
from modules import ai_coach, db, embeddings, llm_categoriser, pdf_processing
from modules.ai_limits import AICapacityError, ai_request_slot
from modules.ai_privacy import AIPrivacy, redact_credentials
from modules.bank_parser import StatementRejected, parse_bank_csv
from tests.test_journey_fixes import coach_api


def test_model_cache_key_distinguishes_delimiters_from_example_structure():
    from modules.categoriser import _sig
    from schemas import OverrideRequest
    left = [('A', 'Groceries'), ('B', 'Transport')]
    right = [('A=>Groceries|B', 'Transport')]
    for examples in (left, right):
        OverrideRequest(rules=[{'match': text, 'category': category}
                               for text, category in examples])
    assert _sig(left) != _sig(right)
    assert _sig(left) == _sig(list(reversed(left)))


def test_distinct_correction_sets_cannot_reuse_the_wrong_cached_model(monkeypatch):
    import pandas as pd
    from modules import categoriser
    monkeypatch.setattr(categoriser, '_USER_MODELS', {})
    monkeypatch.setattr(categoriser, 'load_training_data',
                        lambda: (pd.Series(['BASE']), pd.Series(['Other'])))
    trained = []
    def train(inputs, labels):
        model = object()
        trained.append(model)
        return model
    monkeypatch.setattr(categoriser, 'train_model', train)
    left = [('A', 'Groceries'), ('B', 'Transport')]
    right = [('A=>Groceries|B', 'Transport')]
    first = categoriser.get_model(left)
    second = categoriser.get_model(right)
    assert first is not second
    assert len(trained) == 2
    assert categoriser.get_model(list(reversed(left))) is first
    assert len(trained) == 2


@pytest.mark.parametrize('identifier', [
    'synthetic@example.invalid', '0412 345 678', '+61 412 345 678',
    'account: 12345678', 'BSB: 123-456', 'card: 4111 1111 1111 1111',
    'TFN: 123 456 789', 'GB82 WEST 1234 5698 7654 32', '12 Example Street',
])
def test_common_identifiers_are_hidden_and_only_restored_locally(identifier):
    privacy = AIPrivacy()
    hidden = privacy.text(identifier)
    assert hidden != identifier
    assert 'FINIO_PRIVATE' in hidden
    assert privacy.restore(hidden) == identifier


def test_redaction_preserves_arithmetic_dates_merchants_and_original_records():
    original = {'income': 2000.00, 'date': '2026-06-01', 'merchant': 'COLES',
                'email': 'synthetic@example.invalid', 'user_id': 'private-owner',
                'nested': {'message': 'email synthetic@example.invalid, amount $110.00'}}
    privacy = AIPrivacy()
    result = privacy.value(original)
    assert result['income'] == 2000 and result['date'] == '2026-06-01'
    assert result['merchant'] == 'COLES'
    assert 'email' not in result and 'user_id' not in result
    assert 'synthetic@example.invalid' not in json.dumps(result)
    assert original['email'] == 'synthetic@example.invalid'
    assert '$110.00' in result['nested']['message']
    assert privacy.text('$10000000') == '$10000000'


@pytest.mark.parametrize('credential', ['sk-proj-' + 'x' * 40, 'sb_secret_' + 'x' * 35,
    'Bearer synthetic-private-token', 'password: synthetic-not-real-password',
    'otp: 123456'])
def test_credentials_cannot_be_restored_or_repeated(credential):
    privacy = AIPrivacy()
    hidden = privacy.text(credential)
    assert hidden == '[REDACTED_SECRET]'
    assert privacy.restore(hidden) == hidden
    assert redact_credentials(credential) == hidden


def completion(text='', calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=calls))])


def test_coach_uses_redacted_inputs_but_calculates_against_original_matching_rows(monkeypatch):
    captured = []
    email = 'synthetic@example.invalid'
    def answer(**kwargs):
        captured.append(kwargs)
        token = re.search(r'\[FINIO_PRIVATE_\d+\]', json.dumps(kwargs['messages']))[0]
        call = SimpleNamespace(id='tool1', function=SimpleNamespace(name='category_total', arguments=json.dumps({'query': token})))
        return completion(calls=[call])
    monkeypatch.setattr(ai_coach, 'get_client', lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=answer))))
    rows = [{'date':'2026-06-01', 'merchant':email, 'amount':-110, 'flow':'expense',
             'category':'Groceries', 'user_id':'private-owner', 'balance':1234,
             'description':'Raw private example', 'key':'private-key'},
            {'date':'2026-06-02', 'merchant':email, 'amount':10, 'flow':'expense', 'category':'Groceries'}]
    result = ai_coach.coach_chat('How much did I spend at ' + email + '?',
        {'income':2000, 'spent':100, 'saved':1900, 'date_range':{'start':'2026-06-01','end':'2026-06-02'}}, transactions=rows)
    outgoing = json.dumps(captured)
    assert email not in outgoing and 'private-owner' not in outgoing
    assert 'Raw private example' not in outgoing and 'private-key' not in outgoing
    assert result['verified_facts'][-1]['total'] == 100
    assert result['verified_facts'][-1]['query'] == email
    assert captured[0]['store'] is False and captured[0]['max_tokens'] == 1024


def test_classifier_cannot_invent_merchants_and_redaction_does_not_break_identity(monkeypatch):
    captured = []
    merchants = ['STORE synthetic@example.invalid', 'STORE second@example.invalid']
    def answer(**kwargs):
        captured.append(kwargs)
        return completion(json.dumps({'results':[
            {'m':'merchant_0','c':'Groceries','conf':'high'},
            {'m':'merchant_1','c':'Transport','conf':'high'},
            {'m':'INVENTED MERCHANT','c':'Shopping','conf':'high'}]}))
    monkeypatch.setattr(llm_categoriser, 'has_llm', lambda: True)
    import openai
    monkeypatch.setattr(openai, 'OpenAI', lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=answer))))
    result = llm_categoriser.categorise_merchants(merchants, ['Groceries','Transport','Shopping'],
        context={merchants[0]:{'count':3,'avg':25,'samples':['Raw private statement example']}})
    assert result == {merchants[0]:{'category':'Groceries','confidence':'high'}, merchants[1]:{'category':'Transport','confidence':'high'}}
    outgoing = json.dumps(captured)
    assert 'example.invalid' not in outgoing and 'Raw private statement example' not in outgoing
    assert captured[0]['store'] is False


def test_embeddings_and_simple_completions_do_not_send_identifiers(monkeypatch):
    embed = Mock(return_value=SimpleNamespace(data=[SimpleNamespace(embedding=[1.0])]))
    monkeypatch.setattr(embeddings, 'has_embeddings', lambda: True)
    monkeypatch.setattr(embeddings, '_get_client', lambda: SimpleNamespace(embeddings=SimpleNamespace(create=embed)))
    assert embeddings.embed_texts(['STORE synthetic@example.invalid']) == [[1.0]]
    assert 'example.invalid' not in json.dumps(embed.call_args.kwargs)
    create = Mock(return_value=completion('A summary.'))
    monkeypatch.setattr(ai_coach, 'get_client', lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    assert ai_coach.call_llm('Summarise synthetic@example.invalid', [{'role':'user','content':'account: 12345678'}]) == 'A summary.'
    assert 'example.invalid' not in json.dumps(create.call_args.kwargs)
    assert '12345678' not in json.dumps(create.call_args.kwargs)
    assert create.call_args.kwargs['store'] is False


def test_provider_failure_diagnostics_keep_no_payload_or_credential(monkeypatch):
    create = Mock(side_effect=RuntimeError('private prompt and credential'))
    monkeypatch.setattr(ai_coach, 'get_client', lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    assert ai_coach.call_llm('system', []) is None
    assert ai_coach.last_llm_error == 'RuntimeError'


def test_pasted_credentials_are_not_persisted_in_chat(coach_api, monkeypatch):
    http, append, _ = coach_api
    monkeypatch.setattr(main, 'coach_chat', lambda *a, **k: {'text':'password: synthetic-secret', 'verified_facts':[], 'proposed_actions':[]})
    result = http.post('/coach', json={'message':'password: synthetic-secret'}).json()
    assert 'synthetic-secret' not in result['text']
    assert 'synthetic-secret' not in str(append.call_args_list)


def test_concurrent_ai_requests_are_bounded_and_capacity_is_released_on_error():
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(ai_request_slot())
        with pytest.raises(AICapacityError):
            with ai_request_slot():
                pytest.fail('A fifth request entered the provider')
    with pytest.raises(RuntimeError):
        with ai_request_slot():
            raise RuntimeError('synthetic failure')
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(ai_request_slot())


def claims():
    return {'sub':'11111111-1111-1111-1111-111111111111', 'aud':'authenticated',
            'role':'authenticated', 'iss':'https://synthetic.supabase.co/auth/v1',
            'iat':int(time.time())-10, 'exp':int(time.time())+300}


@pytest.mark.parametrize('change', ['exp','iat','sub','iss','aud','wrong_issuer','wrong_role','expired'])
def test_invalid_signed_jwt_claims_cannot_supply_an_identity(monkeypatch, change):
    secret = 'synthetic-signing-secret-for-tests-only'
    monkeypatch.setenv('SUPABASE_JWT_SECRET', secret)
    monkeypatch.setenv('SUPABASE_URL', 'https://synthetic.supabase.co')
    lookup = Mock(side_effect=AssertionError('Invalid claims must not fall through'))
    monkeypatch.setattr(db, 'get_user', lookup)
    payload = claims()
    if change in payload: del payload[change]
    elif change == 'wrong_issuer': payload['iss'] = 'https://other.supabase.co/auth/v1'
    elif change == 'wrong_role': payload['role'] = 'service_role'
    elif change == 'expired': payload['exp'] = int(time.time())-100
    with pytest.raises(ValueError, match='Invalid or expired'):
        db.get_user_id(jwt.encode(payload, secret, algorithm='HS256'))
    lookup.assert_not_called()


def test_valid_local_jwt_and_normal_server_verification_keep_working(monkeypatch):
    secret = 'synthetic-signing-secret-for-tests-only'
    monkeypatch.setenv('SUPABASE_JWT_SECRET', secret)
    monkeypatch.setenv('SUPABASE_URL', 'https://synthetic.supabase.co')
    assert db.get_user_id(jwt.encode(claims(), secret, algorithm='HS256')) == claims()['sub']
    monkeypatch.delenv('SUPABASE_JWT_SECRET')
    monkeypatch.setattr(db, 'get_user', lambda _: SimpleNamespace(id=claims()['sub']))
    assert db.get_user_id('verified-by-auth-server') == claims()['sub']


@pytest.mark.parametrize('header', ['Bearer ' + 'x'*9000, 'Bearer token with spaces', 'Bearer token\nextra'])
def test_malformed_bearer_headers_are_rejected(header):
    with pytest.raises(HTTPException) as error:
        _parse_bearer(header)
    assert error.value.status_code == 401
    assert _parse_bearer('bearer valid-token') == 'valid-token'


@pytest.mark.parametrize('key', ['sb_secret_'+'x'*30, jwt.encode({'role':'service_role'}, 'synthetic-signing-secret-for-tests-only', algorithm='HS256')])
def test_privileged_keys_cannot_be_used_as_the_web_public_key(monkeypatch,key):
    import supabase
    create = Mock(side_effect=AssertionError('Privileged client was created'))
    monkeypatch.setattr(supabase, 'create_client', create)
    monkeypatch.setenv('SUPABASE_ANON_KEY', key)
    with pytest.raises(ValueError, match='publishable or anon'):
        db.get_client()
    create.assert_not_called()


@pytest.fixture
def security_client(monkeypatch):
    main.app.dependency_overrides[get_current_user] = lambda: AuthUser('synthetic-owner','synthetic-token')
    yield TestClient(main.app, raise_server_exceptions=False)
    main.app.dependency_overrides.clear()


def test_private_input_is_absent_from_validation_errors(security_client):
    response = security_client.post('/spend-check', json={'amount':-1,'current_balance':'password: synthetic-secret',
        'merchant':'synthetic@example.invalid'})
    assert response.status_code == 422
    assert 'synthetic-secret' not in response.text and 'example.invalid' not in response.text
    assert all('input' not in error and 'ctx' not in error for error in response.json()['detail'])
    assert 'no-store' in response.headers['cache-control']
    result = security_client.post('/budgets', json={'targets':{'synthetic@example.invalid':'private invalid amount'}})
    assert result.status_code == 422
    assert 'example.invalid' not in result.text and 'private invalid amount' not in result.text


def test_security_headers_cover_unhandled_errors_and_early_rejections(security_client,monkeypatch):
    monkeypatch.setattr(main, '_require_snapshot', Mock(side_effect=RuntimeError('private payload')))
    responses = [security_client.get('/dashboard'),
        security_client.post('/coach', content=b'x', headers={'content-length':'9'*5000})]
    assert [r.status_code for r in responses] == [500,413]
    for response in responses:
        assert 'private payload' not in response.text
        assert 'no-store' in response.headers['cache-control']
        assert response.headers['x-frame-options'] == 'DENY'
        assert response.headers['x-content-type-options'] == 'nosniff'
    failure = security_client.get('/dashboard',headers={'origin':'http://localhost:5500'})
    assert failure.headers['access-control-allow-origin'] == 'http://localhost:5500'
    rejected = security_client.get('/dashboard',headers={'origin':'https://untrusted.invalid'})
    assert 'access-control-allow-origin' not in rejected.headers


def test_duplicate_authentication_headers_and_unneeded_cors_methods_are_rejected(security_client):
    duplicate = security_client.get('/dashboard',headers=[('Authorization','Bearer one'),('Authorization','Bearer two')])
    assert duplicate.status_code == 400
    forbidden = security_client.options('/profile',headers={'origin':'http://localhost:5500',
        'access-control-request-method':'DELETE'})
    assert forbidden.status_code == 400
    allowed = security_client.options('/profile',headers={'origin':'http://localhost:5500',
        'access-control-request-method':'POST','access-control-request-headers':'authorization,content-type'})
    assert allowed.status_code == 200
    assert 'access-control-allow-credentials' not in allowed.headers


def synthetic_pdf(path):
    stream = b'BT /F1 10 Tf 40 740 Td (Opening balance 1000.00) Tj 0 -20 Td (01/06/2026 COLES 110.00 890.00) Tj 0 -20 Td (02/06/2026 COLES REFUND 10.00 CR 900.00) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data = b'%PDF-1.4\n'; offsets = [0]
    for number, obj in enumerate(objects,1):
        offsets.append(len(data)); data += f'{number} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref = len(data); data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    data += f'trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(data)


def test_real_pdf_worker_preserves_transactions_refunds_and_balances(tmp_path):
    path = tmp_path/'synthetic.pdf'; synthetic_pdf(path)
    frame = parse_bank_csv(path)
    assert frame.amount.tolist() == [-110,10]
    assert frame.balance.tolist() == [890,900]


def test_pdf_timeout_stops_processing_and_preserves_the_source(tmp_path,monkeypatch):
    path = tmp_path/'synthetic.pdf'; synthetic_pdf(path)
    monkeypatch.setattr(pdf_processing,'PDF_TIMEOUT_SECONDS',0.001)
    with pytest.raises(StatementRejected,match='safety limits'):
        pdf_processing.extract_pdf(path)
    assert path.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['synthetic.pdf']


def test_pdf_worker_does_not_inherit_server_keys(tmp_path,monkeypatch):
    path = tmp_path/'synthetic.pdf'; synthetic_pdf(path)
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-private-key')
    monkeypatch.setenv('SUPABASE_ANON_KEY','synthetic-public-key')
    def run(command,**kwargs):
        assert '-I' in command
        assert 'OPENAI_API_KEY' not in kwargs['env'] and 'SUPABASE_ANON_KEY' not in kwargs['env']
        assert 'HOME' not in kwargs['env'] and 'PYTHONPATH' not in kwargs['env']
        kwargs['stdout'].write(json.dumps({'ok':True,'rows':[{'date':'2026-06-01','description':'SHOP','amount':-1}]}).encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(pdf_processing.subprocess,'run',run)
    assert len(pdf_processing.extract_pdf(path)) == 1


def test_non_pdf_header_is_rejected_before_starting_a_worker(tmp_path,monkeypatch):
    path=tmp_path/'synthetic.pdf';path.write_bytes(b'not a PDF')
    run=Mock(side_effect=AssertionError('Parser started for an invalid file'))
    monkeypatch.setattr(pdf_processing.subprocess,'run',run)
    with pytest.raises(StatementRejected,match='PDF header'):
        pdf_processing.extract_pdf(path)
    run.assert_not_called()


def test_production_worker_refuses_to_parse_without_kernel_sandbox(monkeypatch, capsys):
    import resource
    import sys
    from modules import pdf_worker, pdf_sandbox, pdf_parser
    monkeypatch.setattr(resource, 'setrlimit', Mock())
    monkeypatch.setattr(sys, 'argv', ['pdf_worker.py', '/synthetic.pdf', 'required'])
    monkeypatch.setattr(pdf_sandbox, 'restrict_pdf_worker', Mock(side_effect=pdf_sandbox.SandboxUnavailable()))
    parse=Mock(side_effect=AssertionError('Parsed an unconfined file'))
    monkeypatch.setattr(pdf_parser, 'extract_transactions', parse)
    pdf_worker.main()
    assert json.loads(capsys.readouterr().out)['ok'] is False
    parse.assert_not_called()
