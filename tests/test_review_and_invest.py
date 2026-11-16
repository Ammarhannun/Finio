"""Regressions for the reported review-count and investment-page problems."""
from datetime import date
from unittest.mock import Mock

from modules.pipeline import analyze_stored, _restore_full_df
from modules.llm_categoriser import build_questions
from modules.invest import invest_readiness
from modules.ai_coach import build_context
from tests.test_verified_edits import client, REV


def transfer_rows():
    rows = [{'date':'2026-06-01','amount':2000,'merchant':'SALARY','flow':'income','category':'Income'}]
    for merchant in range(13):
        for occurrence in range(2 if merchant < 6 else 1):
            rows.append({'date':f'2026-06-0{2+occurrence}','amount':-10,
                         'merchant':f'PAYID PERSON {merchant}','flow':'transfer','category':'Transfers'})
    return rows


def test_nineteen_transactions_are_thirteen_questions_with_the_same_amounts():
    result = analyze_stored(transfer_rows(), period='all')
    questions = build_questions(_restore_full_df(result['transactions']), {}, limit=20000)
    assert result['review'] == {'count':19, 'merchant_count':13, 'money_out':190, 'money_in':0}
    assert len(questions) == result['review']['merchant_count']
    assert sum(q['count'] for q in questions) == result['review']['count']
    assert sum(q['total'] for q in questions) == result['review']['money_out']
    assert all(len(q['keys']) == q['count'] for q in questions)


def test_precise_review_removes_one_transaction_without_hiding_the_merchant():
    rows = transfer_rows()
    key = analyze_stored(rows,period='all')['transactions'][1]['key']
    rules = [{'tx_key':key,'flow':'transfer'}]
    result = analyze_stored(rows,period='all',overrides=rules)
    questions = build_questions(_restore_full_df(result['transactions']),{},rules,limit=20000)
    assert result['review']['count'] == sum(q['count'] for q in questions) == 18
    assert result['review']['merchant_count'] == len(questions) == 13


def test_case_variants_do_not_drop_transactions_from_a_merchant_question():
    rows=transfer_rows()[:3]
    rows[2]['merchant'] = 'payid person 0'
    result = analyze_stored(rows,period='all')
    questions = build_questions(_restore_full_df(result['transactions']),{},limit=20000)
    assert len(questions) == result['review']['merchant_count'] == 1
    assert questions[0]['count'] == result['review']['count'] == 2


def test_transactions_review_uses_the_dashboard_window_and_survives_empty_windows(client):
    http, _ = client
    for month in ['2026-01','2026-02','2026-03']:
        query='?period=monthly&month='+month
        dashboard=http.get('/dashboard'+query).json()
        response=http.get('/transactions'+query)
        assert response.status_code == 200, response.text
        tx=response.json()
        questions=[q for q in tx['pending_questions'] if q['kind']=='flow']
        assert tx['review'] == dashboard['review']
        assert len(tx['review_keys']) == sum(q['count'] for q in questions) == tx['review']['count']
        assert len(questions) == tx['review']['merchant_count']


def test_quiz_can_apply_only_to_the_rows_in_the_selected_question(client):
    http, commit = client
    questions=http.get('/transactions?period=monthly&month=2026-02').json()['pending_questions']
    question=next(q for q in questions if q['kind']=='flow')
    response=http.post('/quiz',json={'revision':REV,'merchant':question['merchant'],
                                    'tx_keys':question['keys'],'flow':'expense','category':'Other'})
    assert response.status_code == 200, response.text
    rules=commit.call_args.kwargs['changes']['overrides']
    assert rules == [{'tx_key':question['keys'][0],'flow':'expense','category':'Other'}]
    assert commit.call_args.kwargs['resliced']['review']['count'] == 0


def test_quiz_rejects_unknown_or_other_merchant_row_keys(client):
    http, commit = client
    rows=http.get('/transactions').json()['transactions']
    wrong=next(t['key'] for t in rows if t['merchant']=='SALARY')
    for key in [wrong,'0000000000000000']:
        response=http.post('/quiz',json={'revision':REV,'merchant':'PAYID ALEX','tx_keys':[key],'flow':'transfer'})
        assert response.status_code == 422
    commit.assert_not_called()


def test_invest_goal_uses_recorded_progress_instead_of_stale_or_projected_figures(client):
    http,_=client
    response=http.get('/invest?period=monthly&month=2026-02')
    assert response.status_code == 200, response.text
    data=response.json()
    recorded=data['forecast']['current_saved']
    check=next(c for c in data['invest']['readiness']['checks'] if c['id']=='savings_goal')
    assert data['goal']['current_saved'] == check['current'] == recorded
    assert check['current'] != data['forecast']['projected_total']
    assert data['review']['count'] == 1


def test_foundations_are_facts_without_a_mandatory_percentage_or_investment_verdict():
    metrics={'latest_balance':1500,'balance_date':date.today().isoformat(),'net_saved':50,'savings_rate':5}
    result=invest_readiness(metrics,{'savings_gap':500},{'current_saved':100,'projected_total':3000,'target_amount':2000})
    assert result['assessment']=='not_assessed'
    assert not result['can_invest']
    assert all(c['target'] is None for c in result['checks'][:2])
    assert result['checks'][-1]['current']==100
    assert result['checks'][-1]['passed'] is False
    assert not any('20%' in c['label'] or '10%' in c['label'] for c in result['checks'])


def test_all_merchants_remain_available_and_refunds_and_transfers_are_respected():
    rows=[{'date':'2026-06-02','amount':-100-i,'merchant':f'SHOP {i}','flow':'expense','category':'Shopping'} for i in range(9)]
    rows += [{'date':'2026-06-03','amount':20,'merchant':'SHOP 0','flow':'expense','category':'Shopping'},
             {'date':'2026-06-03','amount':-9000,'merchant':'PAYID PRIVATE','flow':'transfer','category':'Transfers'}]
    merchants=analyze_stored(rows,period='all')['averages']['top_merchants']
    assert len(merchants)==9
    assert next(m['total'] for m in merchants if m['merchant']=='SHOP 0')==80
    assert all(m['merchant']!='PAYID PRIVATE' for m in merchants)
