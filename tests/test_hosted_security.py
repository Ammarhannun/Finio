import httpx
import pytest

from scripts.hosted_security import check_release, origin


@pytest.mark.parametrize('value', ['http://example.invalid', 'https://user:secret@example.invalid',
    'https://example.invalid?secret=hidden', 'https://example.invalid/path', 'file:///tmp/test'])
def test_probe_targets_reject_credentials_queries_and_insecure_remote_origins(value):
    with pytest.raises(ValueError):origin(value)


class BodyMustNotBeRead(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError('Attempted to download a response body')


def transport(*, public_data=False, wildcard_cors=False, redirect=False, missing_headers=False):
    def handle(request):
        assert 'authorization' not in request.headers
        headers = {} if missing_headers else {'cache-control':'no-store', 'x-content-type-options':'nosniff',
            'referrer-policy':'no-referrer', 'strict-transport-security':'max-age=31536000'}
        status = 200 if request.url.path in ('/', '/ready') else 401
        if request.url.path == '/docs':status = 404
        if public_data and request.url.path == '/data/export':status=200
        if redirect:
            status=302;headers['Location']='https://untrusted.invalid/private?secret=synthetic'
        elif request.method == 'OPTIONS':
            status=200
            if wildcard_cors:headers['access-control-allow-origin']='*'
            elif request.headers['origin']=='https://frontend.invalid':headers['access-control-allow-origin']='https://frontend.invalid'
        return httpx.Response(status,headers=headers,stream=BodyMustNotBeRead())
    return httpx.MockTransport(handle)


def test_preflight_checks_headers_auth_and_cors_without_reading_any_body():
    with httpx.Client(transport=transport()) as client:
        result=check_release('https://api.invalid','https://frontend.invalid',client=client)
    assert result['passed'] and len(result['checks']) == 23


@pytest.mark.parametrize('fault',['public_data','wildcard_cors','redirect','missing_headers'])
def test_regressions_and_redirects_fail_without_following_or_leaking_details(fault):
    with httpx.Client(transport=transport(**{fault:True})) as client:
        result=check_release('https://api.invalid','https://frontend.invalid',client=client)
    assert not result['passed']
    assert 'secret' not in str(result) and 'Location' not in str(result)


def test_probe_refuses_authenticated_clients():
    with httpx.Client(headers={'Authorization':'Bearer synthetic'},transport=transport()) as client:
        with pytest.raises(ValueError):check_release('https://api.invalid','https://frontend.invalid',client=client)
