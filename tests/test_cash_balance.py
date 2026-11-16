"""Current cash provenance, monthly comparison, persistence and purchase checks."""
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

import main
from modules import db
from modules.cash_balance import balance_view, spending_metrics, MONTH_DAYS
from modules.spend_check import check_purchase
from modules.ai_coach import fallback_coach_response, run_tool
from tests.test_verified_edits import client, REV


TODAY = date.today().isoformat()


def data(amount=2000, as_of=TODAY):
    return {'cash_balance': {'amount': amount, 'as_of': as_of},
            'metrics': {'latest_balance': 10, 'balance_date': '2020-01-01', 'net_saved': 99999},
            'averages': {'months_used': 3, 'monthly': {'spent': 2000}},
            'history_review': {'count': 0}}


@pytest.mark.parametrize('amount,status', [(2000,'comfortable'), (3000,'comfortable'),
                                          (1999.99,'tight'), (460,'tight'), (459,'low'), (0,'low'), (-5,'low')])
def test_monthly_status_is_personal_cash_coverage_not_income_minus_spending(amount, status):
    view = balance_view(data(amount))
    assert view['status'] == status
    assert view['amount'] == amount
    assert view['monthly_spending'] == 2000


def test_old_future_missing_and_partial_history_never_look_comfortable():
    for day in [(date.today()-timedelta(days=1)).isoformat(), (date.today()+timedelta(days=1)).isoformat(), None]:
        assert balance_view(data(99999,day))['status'] == 'stale'
    value = data(); value.pop('cash_balance'); value['metrics'].pop('latest_balance')
    assert balance_view(value)['amount'] is None
    assert balance_view(value)['status'] == 'missing'
    value = data(); value['averages']['months_used'] = 0
    assert balance_view(value)['status'] == 'unknown'
    value = data(); value['history_review']['count'] = 2
    assert balance_view(value)['status'] == 'review'
    assert balance_view(value)['provisional']


def test_latest_statement_is_a_dated_fallback_and_never_a_fictional_zero():
    value=data(); value['cash_balance']=None
    view=balance_view(value)
    assert view['amount']==10 and view['source']=='statement' and not view['is_current']
    value['metrics']['latest_balance']=None
    assert balance_view(value)['amount'] is None


def test_recovered_statement_balance_survives_missing_transaction_balances_and_an_older_manual_entry():
    value = data(500, '2026-05-01')
    value['metrics']['latest_balance'] = None
    value['statement_balance'] = {'amount': 1080, 'as_of': '2026-06-11'}
    view = balance_view(value)
    assert view['amount'] == 1080 and view['source'] == 'statement'
    assert view['as_of'] == '2026-06-11' and view['status'] == 'stale'
    value['cash_balance'] = {'amount': 2000, 'as_of': TODAY}
    assert balance_view(value)['amount'] == 2000
    value['statement_balance'] = {'amount': 1080, 'as_of': TODAY}
    assert balance_view(value)['source'] == 'saved'


def test_last_statement_point_comes_from_full_history_even_in_an_empty_window():
    from modules.pipeline import analyze_stored
    rows=[{'date':'2026-06-10','amount':-20,'merchant':'SHOP','balance':980},
          {'date':'2026-06-11','amount':100,'merchant':'SALARY','balance':1080}]
    for options in [{}, {'period':'custom','period_start':'2020-01-01','period_end':'2020-01-02'}]:
        result=analyze_stored(rows,**options)
        assert result['statement_balance'] == {'amount':1080,'as_of':'2026-06-11'}


def test_same_saved_cash_and_full_history_spending_are_used_across_windows():
    value=data(10000)
    metrics=spending_metrics({'daily_burn_rate':0,'net_saved':99999},value)
    result=check_purchase(None,metrics,100,30)
    assert result['current_net']==10000 and result['balance_source']=='saved'
    assert result['projected_balance']==round(10000-100-2000/MONTH_DAYS*30,2)
    value['cash_balance']['as_of']=(date.today()-timedelta(days=1)).isoformat()
    assert check_purchase(None,spending_metrics({},value),1)['verdict']=='unknown'
    value=data();value['averages']['months_used']=0
    assert check_purchase(None,spending_metrics({},value),1)['verdict']=='unknown'


def test_unreviewed_transfers_prevent_a_green_purchase_estimate():
    value=data(100000);value['history_review']['count']=1
    result=check_purchase(None,spending_metrics({},value),1)
    assert result['verdict']=='yellow' and result['provisional']


def test_balance_save_requires_revision_and_old_tabs_cannot_overwrite(client):
    http,commit=client
    assert http.post('/balance',json={'amount':1250}).status_code==428
    assert http.post('/balance',json={'amount':1250,'revision':'00000000-0000-0000-0000-000000000000'}).status_code==409
    commit.assert_not_called()
    result=http.post('/balance',json={'amount':1250.25,'as_of':TODAY,'revision':REV})
    assert result.status_code==200,result.text
    assert commit.call_args.kwargs['changes']=={'cash_balance':{'amount':1250.25,'as_of':TODAY}}
    assert commit.call_args.kwargs['label']=='Current balance'


@pytest.mark.parametrize('amount', [None, 0, -30])
def test_clear_zero_and_overdrawn_balance_are_explicit_values(client, amount):
    http,commit=client
    result=http.post('/balance',json={'amount':amount,'revision':REV})
    assert result.status_code==200,result.text
    stored=commit.call_args.kwargs['changes']['cash_balance']
    assert stored is None if amount is None else stored['amount']==amount


@pytest.mark.parametrize('payload', [{'amount':100000001}, {'amount':'NaN'}, {},
                                   {'amount':100,'as_of':(date.today()+timedelta(days=1)).isoformat()}])
def test_invalid_balance_cannot_reach_the_database(client, payload):
    http,commit=client
    assert http.post('/balance',json={**payload,'revision':REV}).status_code==422
    commit.assert_not_called()


def test_dashboard_balance_is_independent_of_selected_statement_month(client):
    http,_=client
    stored=db.load_dashboard.return_value
    stored.update(data(4500))
    for query in ['', '?period=daily', '?period=monthly&month=2026-01', '?period=monthly&month=2020-01']:
        result=http.get('/dashboard'+query)
        assert result.status_code==200,result.text
        assert result.json()['balance_view']['amount']==4500
        assert result.json()['balance_view']['as_of']==TODAY


def test_spend_check_prefers_saved_cash_but_respects_an_explicit_single_check_override(client, monkeypatch):
    http,_=client
    db.load_dashboard.return_value.update(data(10000))
    monkeypatch.setattr(db,'save_spend_check',Mock())
    for body,expected,source in [({'amount':100},10000,'saved'), ({'amount':100,'current_balance':200},200,'entered')]:
        result=http.post('/spend-check',json=body)
        assert result.status_code==200,result.text
        assert result.json()['current_net']==expected
        assert result.json()['balance_source']==source
    assert db.load_dashboard.return_value['cash_balance']['amount']==10000


def test_recomputed_edits_preserve_the_saved_balance_in_the_rpc_payload(monkeypatch):
    snapshot={'summary_json':{'cash_balance':{'amount':1234,'as_of':TODAY},
                             'statement_balance':{'amount':1080,'as_of':'2026-06-11'}}}
    monkeypatch.setattr(db,'get_latest_snapshot',lambda *args:deepcopy(snapshot))
    client=Mock();client.rpc.return_value.execute.return_value.data={}
    db.commit_edit(client,'owner',REV,resliced={'metrics':{'net_saved':500}})
    assert client.rpc.call_args.args[1]['p_summary']['cash_balance']==snapshot['summary_json']['cash_balance']
    assert client.rpc.call_args.args[1]['p_summary']['statement_balance']==snapshot['summary_json']['statement_balance']


def test_uploaded_replacement_carries_the_saved_balance_forward(client, monkeypatch):
    http,_=client
    from api.deps import AuthUser
    monkeypatch.setattr('api.upload_auth.authenticate',lambda *args:AuthUser('synthetic','fake'))
    db.get_latest_snapshot.return_value['summary_json']['cash_balance']={'amount':4321,'as_of':TODAY}
    db.get_latest_snapshot.return_value['summary_json']['statement_balance']={'amount':1080,'as_of':'2026-06-11'}
    persist=Mock(return_value={});monkeypatch.setattr(db,'persist_analysis',persist)
    monkeypatch.setattr('modules.embeddings.embed_texts',lambda *args:None)
    result=http.post('/analyze',data={'revision':REV,'confirm_replace':'true'},
                     files={'file':('statement.csv',b'Date,Description,Amount\n2026-06-01,Salary,2000\n2026-06-30,Shop,-100\n','text/csv')})
    assert result.status_code==200,result.text
    assert persist.call_args.args[1]['cash_balance']=={'amount':4321,'as_of':TODAY}
    assert persist.call_args.args[1]['statement_balance']=={'amount':1080,'as_of':'2026-06-11'}
    assert result.json()['balance_view']['amount']==4321


def test_coach_uses_same_balance_and_never_answers_bank_balance_with_statement_surplus():
    view=balance_view(data(1234))
    context={'available_balance':view,'currency':'AUD','saved':99999,'spent':800,'income':1000}
    answer=fallback_coach_response('How much money do I have in my bank?',context)
    assert '1,234.00' in answer and '99999' not in answer
    metrics=spending_metrics({},data(10000))
    context.update(metrics)
    result=run_tool('spend_check',{'amount':100,'days_ahead':30},[],context)
    assert result['current_net']==10000 and result['balance_source']=='saved'
