"""The authenticated test tool must reject deceptive targets before using tokens."""
from unittest.mock import Mock

import pytest

from scripts import hosted_acceptance


@pytest.mark.parametrize('target',[
    'http://127.0.0.1:8000@evil.invalid',
    'http://localhost:8000@evil.invalid',
    'http://localhost.evil.invalid',
    'http://remote.invalid',
    'https://user:synthetic-secret@remote.invalid',
    'https://remote.invalid/?token=synthetic-secret',
    'https://remote.invalid/unexpected-path',
])
def test_bad_targets_fail_before_creating_authenticated_clients(monkeypatch,target):
    client=Mock(side_effect=AssertionError('Authenticated client created for a bad target'))
    monkeypatch.setattr(hosted_acceptance.httpx,'Client',client)
    monkeypatch.setenv('FINIO_TEST_TOKEN_A','synthetic-private-token-a')
    monkeypatch.setenv('FINIO_TEST_TOKEN_B','synthetic-private-token-b')
    with pytest.raises(ValueError) as error:
        hosted_acceptance.run(target)
    client.assert_not_called()
    assert 'synthetic-secret' not in str(error.value)
