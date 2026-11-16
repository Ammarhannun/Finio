"""Synthetic regression cases discovered in the October platform audit."""
import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from modules import db
from modules.bank_parser import parse_bank_csv
from modules.data_processor import add_flags, compute_metrics
from modules.spend_check import check_purchase


@pytest.mark.parametrize('path', ['/analyze', '/analyze/preview', '/analyze/', '/analyze/preview/'])
@pytest.mark.parametrize('header', [None, b'Basic invalid', b'Bearer invalid'])
def test_upload_authentication_never_receives_an_untrusted_body(monkeypatch, path, header):
    from api.upload_auth import UploadAuthMiddleware
    monkeypatch.setattr(db, 'is_configured', lambda: True)
    monkeypatch.setattr(db, 'get_user_id', Mock(side_effect=ValueError('Invalid or expired token')))
    read = Mock(side_effect=AssertionError('body read before authentication'))
    called, sent = [], []
    async def app(*args): called.append(True)
    async def receive(): return read()
    async def send(message): sent.append(message)
    headers = [(b'authorization', header)] if header else []
    asyncio.run(UploadAuthMiddleware(app)(
        {'type': 'http', 'method': 'POST', 'path': path, 'headers': headers}, receive, send))
    assert sent[0]['status'] == 401
    assert not called
    read.assert_not_called()


def test_authenticated_upload_reuses_verified_identity(monkeypatch):
    from api.upload_auth import UploadAuthMiddleware
    from api.deps import get_current_user
    from starlette.requests import Request
    monkeypatch.setattr(db, 'is_configured', lambda: True)
    lookup = Mock(return_value='verified-user')
    monkeypatch.setattr(db, 'get_user_id', lookup)
    async def app(scope, receive, send):
        dependency = get_current_user(Request(scope), 'Bearer token')
        assert next(dependency).user_id == 'verified-user'
        dependency.close()
    asyncio.run(UploadAuthMiddleware(app)(
        {'type': 'http', 'method': 'POST', 'path': '/analyze',
         'headers': [(b'authorization', b'Bearer token')]}, None, None))
    lookup.assert_called_once_with('token')


@pytest.mark.parametrize('bad', ['NaN', 'NA', 'null', 'NULL', 'n/a', 'not-a-number', 'inf'])
def test_nonblank_bad_debit_never_silently_becomes_zero(tmp_path, bad):
    file = tmp_path / 's.csv'
    file.write_text(f'Date,Description,Debit,Credit\n2026-01-01,SHOP,{bad},\n')
    with pytest.raises(ValueError):
        parse_bank_csv(file)


def test_blank_debit_credit_cells_remain_valid(tmp_path):
    file = tmp_path / 's.csv'
    file.write_text('Date,Description,Debit,Credit\n2026-01-01,SHOP,20,\n2026-01-02,PAY,,100\n')
    assert parse_bank_csv(file)['amount'].tolist() == [-20, 100]


def test_old_balance_cannot_borrow_a_new_transaction_date(tmp_path):
    file = tmp_path / 's.csv'
    file.write_text(f'Date,Description,Amount,Balance\n2026-01-01,SALARY,1000,1000\n{date.today()},SHOP,-1,\n')
    metrics = compute_metrics(add_flags(parse_bank_csv(file)))
    assert metrics['balance_date'] == '2026-01-01'
    assert check_purchase(None, metrics, 100)['verdict'] == 'unknown'


def test_latest_dated_balance_survives_save_reload(tmp_path):
    from modules.pipeline import run_full_pipeline, analyze_stored
    file = tmp_path / 's.csv'
    file.write_text('Date,Description,Amount,Balance\n2026-01-01,SALARY,1000,1000\n2026-02-01,COLES,-20,\n')
    result = run_full_pipeline(file, period='all')
    restored = analyze_stored(db._tx_rows('u', result['all_transactions']), period='all')
    assert restored['metrics']['balance_date'] == result['metrics']['balance_date'] == '2026-01-01'


def test_provider_failure_details_do_not_reach_coach_response(monkeypatch):
    from modules import ai_coach
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError('provider-private-details')
    monkeypatch.setattr(ai_coach, 'get_client', lambda: client)
    monkeypatch.setattr(ai_coach, 'fallback_coach_response', lambda *args: 'Try again.')
    response = ai_coach.coach_chat('Hello', {})
    assert response['source'] == 'fallback'
    assert 'provider-private-details' not in str(response)


def test_low_confidence_label_cannot_trigger_a_naive_bayes_guess(monkeypatch):
    from modules import categoriser
    monkeypatch.setattr(categoriser, 'get_model', Mock(side_effect=AssertionError('must abstain')))
    frame = pd.DataFrame({'description': ['OPAQUE MERCHANT'], 'amount': [-25],
                          'flow': ['expense'], 'is_transfer': [False]})
    result = categoriser.categorise_data(frame, llm_cache={
        'OPAQUE MERCHANT': {'category': 'Health', 'confidence': 'low'}})
    assert result['category'].tolist() == ['Other']


def test_transfer_category_agrees_before_and_after_persistence(tmp_path):
    from modules.pipeline import run_full_pipeline, analyze_stored
    file = tmp_path / 's.csv'
    file.write_text('Date,Description,Amount\n2026-01-01,COLES,-100\n')
    rules = [{'match': 'COLES', 'category': 'Transfers'}]
    result = run_full_pipeline(file, overrides=rules)
    restored = analyze_stored(db._tx_rows('u', result['all_transactions']), overrides=rules)
    assert result['metrics']['total_spent'] == restored['metrics']['total_spent'] == 0
    assert result['all_transactions'][0]['flow'] == 'transfer'


def test_pdf_debit_balance_keeps_its_negative_sign():
    from modules.pdf_parser import _parse_line, _find_opening_balance, _apply_signs
    opening = _find_opening_balance(['Opening balance 100.00 DR'])
    row = _parse_line('01/01/2026 SHOP 20.00 120.00 DR')
    assert opening == -100
    assert row['balance'] == -120
    assert _apply_signs([row], opening)[0]['amount'] == -20


def test_pdf_running_balance_mismatch_is_refused():
    from modules.pdf_parser import _parse_line, _apply_signs
    with pytest.raises(ValueError, match='reconcile'):
        _apply_signs([_parse_line('01/01/2026 SHOP 20.00 900.00')], 1000)


@pytest.mark.parametrize('tool,args', [
    ('propose_budget', {'category': 'Groceries', 'amount': float('nan')}),
    ('propose_budget', {'category': 'Groceries', 'amount': float('inf')}),
    ('propose_goal', {'amount': float('inf')}),
    ('propose_goal', {'amount': -1}),
    ('propose_goal', {'target_date': '2026-99-99'}),
    ('spend_check', {'amount': -10}),
    ('spend_check', {'amount': 10, 'days_ahead': -1}),
    ('category_total', []),
])
def test_model_tool_arguments_are_untrusted(tool, args):
    import json
    from modules.ai_coach import run_tool
    result = run_tool(tool, args, [], {})
    assert result['ok'] is False
    json.dumps(result, allow_nan=False)


def test_net_tool_total_explains_refunds_without_subtracting_twice():
    from modules.ai_coach import run_tool
    rows = [{'merchant': 'COLES', 'amount': -110, 'flow': 'expense'},
            {'merchant': 'COLES REFUND', 'amount': 10, 'flow': 'expense'}]
    result = run_tool('category_total', {'query': 'COLES'}, rows, {})
    assert (result['gross_purchases'], result['refunds'], result['total']) == (110, 10, 100)
    assert result['total_is_net_of_refunds'] is True


def test_insight_write_cannot_race_another_change(monkeypatch):
    from api.deps import get_current_user, mutation_guard
    from fastapi import HTTPException
    from starlette.requests import Request
    monkeypatch.setattr(db, 'is_configured', lambda: True)
    monkeypatch.setattr(db, 'get_user_id', lambda token: 'same-user')
    request = Request({'type':'http', 'method':'GET', 'path':'/insight', 'headers':[], 'query_string':b''})
    dependency = get_current_user(request, 'Bearer token')
    next(dependency)
    try:
        with pytest.raises(HTTPException) as error:
            with mutation_guard('same-user'): pass
        assert error.value.status_code == 409
    finally:
        dependency.close()


def _text_pdf(path, lines):
    """Small real PDF fixture, independent of any optional PDF writer."""
    stream = 'BT /F1 12 Tf 40 760 Td ' + ' '.join(
        f'({line}) Tj 0 -18 Td' for line in lines) + ' ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream'.encode()]
    data = bytearray(b'%PDF-1.4\n'); offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data)); data.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref = len(data)
    data.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets: data.extend(f'{offset:010} 00000 n \n'.encode())
    data.extend(f'trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    path.write_bytes(data)


def test_real_text_pdf_extracts_reconciled_transactions(tmp_path):
    file = tmp_path / 's.pdf'
    _text_pdf(file, ['Opening balance 1000.00', '01/01/2026 COLES 20.00 980.00',
                     '02/01/2026 SALARY 100.00 CR 1080.00'])
    rows = parse_bank_csv(file)
    assert rows['amount'].tolist() == [-20, 100]
    assert rows['balance'].tolist() == [980, 1080]


def test_real_pdf_with_unparsed_dated_row_is_not_partially_accepted(tmp_path):
    file = tmp_path / 's.pdf'
    _text_pdf(file, ['Opening balance 1000.00', '01/01/2026 COLES 20.00 980.00',
                     '02/01/2026 UNREADABLE AMOUNT'])
    with pytest.raises(ValueError, match='dated PDF row'):
        parse_bank_csv(file)


def test_pdf_letter_date_and_address_do_not_hide_the_balance_column(tmp_path):
    file = tmp_path / 'bank-summary.pdf'
    _text_pdf(file, ['UNIT 65 80 EXAMPLE STREET', '11 June 2026',
                     'Date Transaction details Amount Balance',
                     '10 Jun 2026 SHOP -$20.00 $980.00',
                     '11 Jun 2026 SALARY $100.00 $1080.00'])
    rows = parse_bank_csv(file)
    assert rows['amount'].tolist() == [-20, 100]
    assert rows['balance'].tolist() == [980, 1080]
    metrics = compute_metrics(add_flags(rows))
    assert metrics['latest_balance'] == 1080
    assert metrics['balance_date'] == '2026-06-11'


def test_a_bare_date_inside_the_pdf_transaction_table_still_fails(tmp_path):
    file = tmp_path / 'incomplete-row.pdf'
    _text_pdf(file, ['11 June 2026', 'Date Transaction details Amount Balance',
                     '10 Jun 2026', '11 Jun 2026 SHOP -$20.00 $980.00'])
    with pytest.raises(ValueError, match='dated PDF row'):
        parse_bank_csv(file)


def test_small_ambiguous_payment_is_offered_for_review(tmp_path):
    from modules.pipeline import run_full_pipeline
    file = tmp_path / 's.csv'
    file.write_text('Date,Description,Amount\n2026-01-01,SALARY,3000\n2026-01-02,PAYID TO DINNER,-90\n')
    result = run_full_pipeline(file)
    assert any(q['merchant'] == 'PAYID TO DINNER' and q['kind'] == 'flow'
               for q in result['pending_questions'])


def test_view_cache_is_bounded_by_payload_size(monkeypatch):
    import main, time, json
    monkeypatch.setattr(main, '_VIEW_CACHE_MAX_BYTES', 1000)
    main._VIEW_CACHE.clear()
    try:
        for i in range(5):
            value = {'payload': 'x' * 600}
            main._VIEW_CACHE[str(i)] = (time.time(), value, len(json.dumps(value).encode()))
        main._cache_evict()
        assert len(main._VIEW_CACHE) == 1
        assert sum(v[2] for v in main._VIEW_CACHE.values()) <= 1000
        assert '4' in main._VIEW_CACHE
    finally:
        main._VIEW_CACHE.clear()


def test_binary_csv_content_is_checked_beyond_the_first_block(tmp_path):
    file = tmp_path / 's.csv'
    file.write_bytes(b'Date,Description,Amount\n' + b' ' * 9000 + b'\x00')
    with pytest.raises(ValueError, match='binary'):
        parse_bank_csv(file)
