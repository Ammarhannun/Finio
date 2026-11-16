"""Pattern freshness is actual source coverage, not the current/selected date."""
from unittest.mock import Mock

import pytest

from modules import db
from tests.test_verified_edits import client


@pytest.mark.parametrize('query', ['', '?period=all', '?period=monthly&month=2026-01',
                                  '?period=monthly&month=2026-03'])
def test_dashboard_reports_latest_uploaded_row_even_for_older_or_empty_periods(client, query):
    http, commit = client
    response = http.get('/dashboard' + query)
    assert response.status_code == 200, response.text
    assert response.json()['data_freshness'] == {
        'transactions_through': '2026-02-02', 'source': 'uploaded_statements'}
    commit.assert_not_called()


def test_refresh_discovers_new_upload_coverage_and_never_changes_the_snapshot(client, monkeypatch):
    http, commit = client
    before = http.get('/dashboard?period=all').json()
    rows = db.get_all_transactions(None, None)
    monkeypatch.setattr(db, 'get_all_transactions', Mock(return_value=rows + [
        {'date': '2026-02-10', 'merchant': 'LATER SHOP', 'amount': -20, 'category': 'Shopping', 'flow': 'expense'}]))
    after = http.get('/dashboard?period=all').json()
    assert before['data_freshness']['transactions_through'] == '2026-02-02'
    assert after['data_freshness']['transactions_through'] == '2026-02-10'
    assert after['revision'] == before['revision']
    commit.assert_not_called()


def test_missing_source_rows_do_not_claim_the_snapshot_or_calendar_date(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr(db, 'get_all_transactions', Mock(return_value=[]))
    response = http.get('/dashboard')
    assert response.status_code == 200, response.text
    assert response.json()['data_freshness']['transactions_through'] is None
