"""Read-only anonymous checks against the reviewed deployment, without body logs.

No login, statements, tokens or writes are required. These checks supplement
authenticated acceptance and an independent assessment; they cannot replace them.
"""
import argparse
import json
from urllib.parse import urlsplit

import httpx


def origin(value):
    url = urlsplit(value)
    if (url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/')
            or (url.scheme == 'http' and url.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise ValueError('Use HTTPS origins without credentials, paths or queries; HTTP is allowed only locally.')
    return value.rstrip('/')


def check_release(api_url, frontend_origin, *, client=None):
    api_url, frontend_origin = origin(api_url), origin(frontend_origin)
    own_client = client is None
    client = client or httpx.Client(timeout=8, follow_redirects=False)
    if client.headers.get('authorization'):
        raise ValueError('Use an anonymous client for the security preflight.')
    results = []
    def record(name, passed):
        results.append({'check': name, 'passed': bool(passed)})
    def request(method, path, **kwargs):
        # Streaming prevents an accidentally exposed financial response from
        # being downloaded. Never read or print a response body or Location.
        with client.stream(method, api_url+path, follow_redirects=False, **kwargs) as response:
            return response.status_code, response.headers
    try:
        for name, path in [('liveness', '/'), ('readiness', '/ready')]:
            status, headers = request('GET', path)
            record(name, status == 200)
            record(name+'_no_store', 'no-store' in headers.get('cache-control', '').lower())
            record(name+'_nosniff', headers.get('x-content-type-options') == 'nosniff')
            record(name+'_no_referrer', headers.get('referrer-policy') == 'no-referrer')
            if api_url.startswith('https:'):
                record(name+'_hsts', 'max-age=' in headers.get('strict-transport-security', '').lower())
        for path in ['/dashboard', '/transactions', '/data/export', '/ai/preferences']:
            status, headers = request('GET', path)
            record('unauthenticated_'+path.strip('/').replace('/', '_'), status == 401)
            record('private_cache_'+path.strip('/').replace('/', '_'),
                   'no-store' in headers.get('cache-control', '').lower())
        status, _ = request('POST', '/analyze/preview', content=b'')
        record('upload_requires_auth_before_parsing', status == 401)
        status, headers = request('OPTIONS', '/dashboard', headers={
            'Origin': frontend_origin, 'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'Authorization,Content-Type'})
        record('cors_exact_frontend', status == 200 and headers.get('access-control-allow-origin') == frontend_origin)
        record('cors_no_ambient_credentials', headers.get('access-control-allow-credentials', '').lower() != 'true')
        _, headers = request('OPTIONS', '/dashboard', headers={
            'Origin': 'https://untrusted-origin.invalid', 'Access-Control-Request-Method': 'GET'})
        record('cors_rejects_untrusted_origin', 'access-control-allow-origin' not in headers)
        status, headers = request('GET', '/docs')
        record('production_docs_disabled', status == 404)
        return {'passed': all(item['passed'] for item in results), 'checks': results}
    except httpx.HTTPError:
        record('network_and_verified_tls', False)
        return {'passed': False, 'checks': results}
    finally:
        if own_client:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', required=True)
    parser.add_argument('--frontend-origin', required=True)
    args = parser.parse_args()
    try:
        result = check_release(args.api_url, args.frontend_origin)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
