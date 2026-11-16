"""Provider transmission requires current, live, account-owned consent."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import pytest
from fastapi.testclient import TestClient

import main
from api.deps import AuthUser
from modules import ai_coach, db, embeddings, llm_categoriser
from modules.ai_limits import ai_request_slot
from modules.ai_policy import (CONSENT_VERSION, AISharingDisabled, bind_user,
                               preference_status, provider_allowed, request_policy)

OWNER = AuthUser('synthetic-owner', 'synthetic-token')


def account(preference, owner=OWNER.user_id):
    return SimpleNamespace(id=owner, user_metadata={'finio_ai': preference})


@pytest.mark.parametrize('value', [None, {}, True, [], 'true', {'enabled': 'true', 'version': CONSENT_VERSION},
    {'enabled': 1, 'version': CONSENT_VERSION}, {'enabled': True},
    {'enabled': True, 'version': 'old'}, {'enabled': False, 'version': CONSENT_VERSION}])
def test_absent_or_malformed_consent_never_enables_provider(monkeypatch, value):
    monkeypatch.setattr(db, 'get_user', lambda _: account(value))
    with request_policy():
        bind_user(OWNER)
        assert not provider_allowed()
        with pytest.raises(AISharingDisabled):
            with ai_request_slot():
                pytest.fail('Provider slot entered without consent')


@pytest.mark.parametrize('failure', ['network', 'wrong-owner', 'no-account'])
def test_unverifiable_preference_fails_closed(monkeypatch, failure):
    loader = (Mock(side_effect=RuntimeError('private details')) if failure == 'network' else
              Mock(return_value=account({'enabled': True, 'version': CONSENT_VERSION}, 'other')
                   if failure == 'wrong-owner' else None))
    monkeypatch.setattr(db, 'get_user', loader)
    with request_policy():
        bind_user(OWNER)
        assert preference_status()['available'] is False
        assert not provider_allowed()


def test_live_preference_is_cached_per_request_and_rechecked_on_next_request(monkeypatch):
    value = {'enabled': True, 'version': CONSENT_VERSION}
    read = Mock(side_effect=lambda _: account(value.copy()))
    monkeypatch.setattr(db, 'get_user', read)
    with request_policy():
        bind_user(OWNER)
        assert provider_allowed()
        value['enabled'] = False
        assert provider_allowed()  # Changes affect new requests, not a running one.
        assert read.call_count == 1
    with request_policy():
        bind_user(OWNER)
        assert not provider_allowed()
    assert read.call_count == 2


def test_parallel_requests_and_threadpool_dependencies_cannot_share_consent(monkeypatch):
    monkeypatch.setattr(db, 'get_user', lambda token: account(
        {'enabled': token == 'on', 'version': CONSENT_VERSION}, token))
    async def one(token):
        with request_policy():
            assert not provider_allowed()  # An unbound request is denied.
        with request_policy():
            await anyio.to_thread.run_sync(bind_user, AuthUser(token, token))
            await asyncio.sleep(0)
            return await anyio.to_thread.run_sync(provider_allowed)
    async def run():
        return await asyncio.gather(one('on'), one('off'), one('on'), one('off'))
    assert asyncio.run(run()) == [True, False, True, False]


def test_provider_slots_block_every_sdk_path_even_if_availability_is_overridden(monkeypatch):
    import openai
    sdk = Mock()
    monkeypatch.setattr(ai_coach, 'get_client', lambda: sdk)
    monkeypatch.setattr(llm_categoriser, 'has_llm', lambda: True)
    monkeypatch.setattr(embeddings, 'has_embeddings', lambda: True)
    monkeypatch.setattr(embeddings, '_get_client', lambda: sdk)
    monkeypatch.setattr(openai, 'OpenAI', lambda **_: sdk)
    with request_policy():
        assert ai_coach.call_llm('synthetic', [{'role': 'user', 'content': 'hello'}]) is None
        assert ai_coach.coach_chat('Hello', {})['source'] == 'fallback'
        assert llm_categoriser.categorise_merchants(['SHOP'], ['Shopping']) is None
        assert embeddings.embed_texts(['SHOP']) is None
    sdk.chat.completions.create.assert_not_called()
    sdk.embeddings.create.assert_not_called()


def test_explicit_current_consent_allows_a_mock_provider_request(monkeypatch):
    sdk = Mock()
    sdk.embeddings.create.return_value.data = [SimpleNamespace(embedding=[0.1, 0.2])]
    monkeypatch.setattr(db, 'get_user', lambda _: account({'enabled': True, 'version': CONSENT_VERSION}))
    monkeypatch.delenv('FINIO_DISABLE_LLM', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-key')
    monkeypatch.setattr(embeddings, '_get_client', lambda: sdk)
    with request_policy():
        bind_user(OWNER)
        assert embeddings.embed_texts(['SHOP']) == [[0.1, 0.2]]
    sdk.embeddings.create.assert_called_once()


@pytest.mark.parametrize('preference,status,enabled', [
    ({}, 200, False), ({'enabled': True, 'version': CONSENT_VERSION}, 200, True), (None, 503, None)])
def test_real_http_dependency_reads_live_account_preferences(monkeypatch, preference, status, enabled):
    monkeypatch.setattr(db, 'is_configured', lambda: True)
    monkeypatch.setattr(db, 'get_user_id', lambda _: OWNER.user_id)
    read = Mock(return_value=account(preference)) if preference is not None else Mock(side_effect=RuntimeError('secret'))
    monkeypatch.setattr(db, 'get_user', read)
    assert TestClient(main.app).get('/ai/preferences').status_code == 401
    response = TestClient(main.app).get('/ai/preferences', headers={'Authorization': 'Bearer synthetic-token'})
    assert response.status_code == status
    if enabled is not None:
        assert response.json() == {'enabled': enabled, 'available': True, 'consent_version': CONSENT_VERSION}
    assert 'secret' not in response.text
    read.assert_called_once_with('synthetic-token')


def test_reclassification_without_consent_refuses_before_reading_financial_rows(monkeypatch):
    read = Mock(side_effect=AssertionError('Read financial rows'))
    monkeypatch.setattr(main, '_require_snapshot', read)
    with request_policy(), pytest.raises(main.HTTPException) as error:
        main.reclassify(OWNER)
    assert error.value.status_code == 403
    read.assert_not_called()


def test_offline_insight_uses_all_history_without_returning_cached_ai_text(monkeypatch):
    monkeypatch.setattr(main, '_require_snapshot', lambda _: ('client', {'context': {'slice': True}}))
    monkeypatch.setattr(main, '_alltime_insight_context', lambda _: {'all': True})
    monkeypatch.setattr(main, 'generate_insight', lambda facts: facts)
    cached = Mock(side_effect=AssertionError('Read AI cache'))
    monkeypatch.setattr(db, 'get_cached_insight', cached)
    main._RATE.clear()
    with request_policy():
        assert main.insight(user=OWNER) == {'all': True}
    cached.assert_not_called()
