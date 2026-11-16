"""Explicit opt-in hosted checks with two EMPTY disposable staging accounts.

Tokens stay in environment variables and are never printed. This writes synthetic
statements, exercises edits/undo/export/isolation, then deletes that test data.
Email delivery, sign-in UI and recovery require a separate browser/inbox check.
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.hosted_security import origin


def run(base_url):
    # Validate before reading tokens or constructing authenticated clients.
    # A prefix check accepts localhost-looking userinfo pointing at another host.
    base_url = origin(base_url)
    tokens = [os.environ.get('FINIO_TEST_TOKEN_A'), os.environ.get('FINIO_TEST_TOKEN_B')]
    if not all(tokens):
        raise RuntimeError('Set FINIO_TEST_TOKEN_A and FINIO_TEST_TOKEN_B privately in your environment.')
    try:
        subjects = [json.loads(base64.urlsafe_b64decode(t.split('.')[1] + '==='))['sub'] for t in tokens]
    except Exception:
        raise RuntimeError('Both test tokens must be valid signed-in account access tokens.') from None
    if subjects[0] == subjects[1]:
        raise RuntimeError('Use two distinct disposable accounts.')
    clients = [httpx.Client(base_url=base_url, headers={'Authorization': 'Bearer ' + t},
                           timeout=120, follow_redirects=False) for t in tokens]

    def call(client, method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        if response.status_code != expected:
            # Do not print bodies, credentials, headers or account identifiers.
            raise RuntimeError(f'{method} {path.split("?")[0]} returned {response.status_code}; expected {expected}.')
        return response.json()

    try:
        call(clients[0], 'GET', '/ready')
        for client in clients:
            exported = call(client, 'GET', '/data/export')['data']
            if any(exported.get(table) for table in ('transactions', 'snapshots', 'budgets', 'goals', 'streaks', 'chat_history', 'spend_checks', 'merchant_embeddings')):
                raise RuntimeError('A test account already has saved data. Stopped before uploading; use empty disposable accounts.')
        # Alphabetic uppercase IDs survive merchant case and store-number cleanup.
        marker_alphabet = str.maketrans('0123456789', 'GHIJKLMNOP')
        markers = ['FINIO TEST ' + uuid4().hex.upper().translate(marker_alphabet) for _ in clients]
        for client, marker in zip(clients, markers):
            csv = f'Date,Description,Amount,Balance\n2026-06-01,SALARY {marker},2000,2000\n2026-06-02,COLES {marker},-110,1890\n2026-06-03,COLES REFUND {marker},10,1900\n'.encode()
            upload = {'file': ('synthetic-acceptance.csv', csv, 'text/csv')}
            preview = call(client, 'POST', '/analyze/preview', files=upload)
            call(client, 'POST', '/analyze', files=upload,
                 data={'revision': preview.get('revision') or '', 'confirm_replace': 'false'})
        for index, client in enumerate(clients):
            tx = call(client, 'GET', '/transactions')
            assert len(tx['transactions']) == 3
            assert all(markers[1-index] not in t['merchant'] for t in tx['transactions'])
            assert any(markers[index] in t['merchant'] for t in tx['transactions'])
            dash = call(client, 'GET', '/dashboard?period=monthly&month=2026-06')
            assert dash['metrics']['total_spent'] == 100
            assert dash['metrics']['total_income'] == 2000
            before = dash['revision']
            saved = call(client, 'POST', '/budgets', json={'revision': before, 'targets': {'Groceries': 400}})
            assert saved['revision'] != before and saved['can_undo']
            call(client, 'POST', '/budgets', expected=409, json={'revision': before, 'targets': {'Groceries': 999}})
            reloaded = call(client, 'GET', '/budgets')
            assert reloaded['targets']['Groceries'] == 400
            call(client, 'POST', '/changes/undo', json={'revision': saved['revision']})
            assert 'Groceries' not in call(client, 'GET', '/budgets')['targets']
            assert len(call(client, 'GET', '/data/export')['data']['transactions']) == 3
        call(clients[0], 'POST', '/data/delete', json={'confirmation': 'DELETE'})
        assert len(call(clients[1], 'GET', '/transactions')['transactions']) == 3
        call(clients[1], 'POST', '/data/delete', json={'confirmation': 'DELETE'})
        for client in clients:
            exported = call(client, 'GET', '/data/export')['data']
            assert all(not exported.get(table) for table in ('transactions', 'snapshots', 'budgets', 'goals', 'streaks', 'chat_history', 'spend_checks', 'merchant_embeddings'))
            call(client, 'GET', '/profile')  # signed-in auth account is retained
        print('PASS: hosted upload, exact refund totals, persistence, two-account isolation, stale save, undo, export and deletion scope.')
        print('Still requires browser/inbox checks: signup, sign-out, recovery email, new password and session expiry.')
    finally:
        for client in clients:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True, help='Staging API URL')
    parser.add_argument('--confirm-disposable-test-accounts', action='store_true',
                        help='Explicitly authorize synthetic writes and data deletion for these two empty staging accounts')
    args = parser.parse_args()
    if not args.confirm_disposable_test_accounts:
        parser.error('Refusing writes without --confirm-disposable-test-accounts.')
    try:
        origin(args.base_url)
    except ValueError as error:
        parser.error(str(error))
    try:
        run(args.base_url)
    except (RuntimeError, AssertionError, httpx.HTTPError) as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else f'FAILED: {type(exc).__name__}. Test data may remain in the disposable accounts.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
