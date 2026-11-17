"""Behavioral regressions from the disposable-account user journey."""
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from api.deps import AuthUser, get_current_user
from modules import ai_coach, db
from modules.coach_scope import resolve_coach_scope
from modules.merchant_totals import net_merchant_totals
from modules.analytics import compute_averages
from modules.pipeline import analyze_stored
import main

ROWS = [
    {'date':f'2026-{month:02d}-01','amount':amount,'merchant':merchant,'category':category,'flow':flow}
    for month in (8,9,10) for amount,merchant,category,flow in [
        (1000,'SALARY','Income','income'),(-85,'ZARA','Shopping','expense'),(35,'ZARA REFUND','Shopping','expense')]]


def test_refunds_join_only_an_exact_existing_purchase_merchant():
    rows = [{'date':'2026-08-01','description':name,'amount':amount,'flow':'expense','category':'Shopping'}
            for name, amount in [('ZARA',-85),('ZARA REFUND',35),('REFUND FROM ZARA',10),
                                 ('ZARA HOME',-20),('REFUND UNKNOWN',5),('REFUND SHOP',-12)]]
    df = pd.DataFrame(rows)
    totals = net_merchant_totals(df)
    assert dict((row['merchant'],row['total']) for row in totals) == {
        'ZARA':40, 'ZARA HOME':20, 'REFUND SHOP':12, 'REFUND UNKNOWN':-5}
    assert sum(row['total'] for row in totals) == -df.amount.sum()
    assert compute_averages(df)['top_merchants'] == totals
    assert df.description.tolist()[1] == 'ZARA REFUND'  # no mutation of matching keys


def test_net_credits_stay_visible_and_reconcile():
    df = pd.DataFrame([{'description':'STORE','amount':-10,'flow':'expense'},
                       {'description':'STORE REFUND','amount':30,'flow':'expense'}])
    assert net_merchant_totals(df) == [{'merchant':'STORE','total':-20}]


@pytest.mark.parametrize('message,period,month', [
    ('Across all uploaded months, what did I spend?', 'all', None),
    ('What did I spend all time?', 'all', None),
    ('How much did I ever spend at Zara?', 'all', None),
    ('How much in September 2026?', 'monthly', '2026-09'),
    ('What about 2026-08?', 'monthly', '2026-08'),
    ('How much at Zara?', 'monthly', '2026-10'),
])
def test_explicit_read_scope_overrides_selection(message,period,month):
    result=resolve_coach_scope(message,'monthly','2026-10',['2026-08','2026-09','2026-10'])
    assert (result['period'],result['month']) == (period,month)


def test_ambiguous_or_unavailable_months_are_not_silently_answered():
    assert 'error' in resolve_coach_scope('September?',available_months=['2025-09','2026-09'])
    assert 'error' in resolve_coach_scope('January 2026?',available_months=['2026-10'])
    assert 'error' in resolve_coach_scope('August and September 2026?',available_months=['2026-08','2026-09'])


@pytest.fixture
def coach_api(monkeypatch):
    data=analyze_stored(ROWS,period='monthly',period_anchor='2026-10-01')
    data.update(revision='11111111-1111-1111-1111-111111111111', month='2026-10')
    for name,value in {'get_client':object(),'get_all_transactions':ROWS,'get_transactions':ROWS,
        'get_overrides':[],'get_budget_targets':{},'get_user_profile':{},'get_chat_history':[],
        'get_chat_title':'Existing chat','edit_state':{'revision':data['revision']}}.items():
        monkeypatch.setattr(db,name,Mock(return_value=value))
    append=Mock();monkeypatch.setattr(db,'append_chat',append)
    monkeypatch.setattr(main,'_require_snapshot',lambda user:(object(),data))
    monkeypatch.setattr(main,'_rate_limit',lambda *args:None)
    main.app.dependency_overrides[get_current_user]=lambda:AuthUser('test','fake')
    main._VIEW_CACHE.clear()
    yield TestClient(main.app),append,data
    main.app.dependency_overrides.clear();main._VIEW_CACHE.clear()


def test_coach_route_supplies_correct_rows_and_facts_for_all_history(coach_api,monkeypatch):
    http,_,_=coach_api
    def answer(message,context,**kwargs):
        total=ai_coach.run_tool('category_total',{'query':'Zara'},kwargs['transactions'],context)
        return {'text':str(total['total']),'verified_facts':[{'kind':'category_total',**total,'date_range':context['date_range']}],'proposed_actions':[]}
    monkeypatch.setattr(main,'coach_chat',answer)
    base={'period':'monthly','month':'2026-10'}
    all_history=http.post('/coach',json={**base,'message':'Across all uploaded months, how much at Zara?'}).json()
    selected=http.post('/coach',json={**base,'message':'How much at Zara?'}).json()
    assert all_history['text']=='150.0'
    assert selected['text']=='50.0'
    assert all_history['verified_facts'][0]['date_range']['start']=='2026-08-01'


def test_coach_route_supplies_confirmed_goal_and_whole_history_pace(coach_api,monkeypatch):
    http,_,data=coach_api
    data['goal']={'target_amount':6000,'target_date':'2027-12-01'}
    captured={}
    def answer(message,context,**kwargs):
        captured.update(context)
        return {'text':'checked','verified_facts':[],'proposed_actions':[]}
    monkeypatch.setattr(main,'coach_chat',answer)
    assert http.post('/coach',json={'message':'Am I on track to save?','period':'monthly','month':'2026-10'}).json()['text']=='checked'
    assert captured['confirmed_goal']==data['goal']
    assert captured['forecast_months_used']>=1
    assert captured['goal_forecast']['target_amount']==6000


def test_coach_greeting_does_not_recap_finances_without_ai(monkeypatch):
    monkeypatch.setattr(ai_coach,'get_client',lambda:None)
    result=ai_coach.coach_chat('hi',{'patterns':['Food is your biggest spend.']})
    assert result['text'].startswith('Hi!')
    assert 'Food is your biggest spend' not in result['text']
    assert result['source']=='fallback'
    assert result['mode_notice']


def test_savings_pace_requires_confirmed_goal_and_full_month(monkeypatch):
    provider=Mock()
    monkeypatch.setattr(ai_coach,'get_client',lambda:provider)
    base={'currency':'AUD','saved':1292.86,'date_range':{'start':'2026-06-01','end':'2026-06-11'},
          'goal_forecast':{'on_track':True,'projected_total':6900,'monthly_rate':450}}
    no_goal=ai_coach.coach_chat('Am I on track to save?',base)
    assert "can't tell" in no_goal['text']
    assert '2026-06-01 to 2026-06-11' in no_goal['text']
    goal={**base,'confirmed_goal':{'target_amount':6000,'target_date':'2027-12-01'}}
    no_full_month=ai_coach.coach_chat('Am I on track to save?',goal)
    assert 'no complete calendar month' in no_full_month['text']
    on_pace=ai_coach.coach_chat('Am I on track to save?',{**goal,'forecast_months_used':3})
    assert 'on pace' in on_pace['text'] and '6,000.00' in on_pace['text']
    assert on_pace['source']=='calculated'
    provider.chat.completions.create.assert_not_called()


def test_proposals_survive_history_and_expire_without_upgrading_revision(coach_api,monkeypatch):
    http,append,data=coach_api
    action={'kind':'budget','category':'Shopping','amount':350}
    monkeypatch.setattr(main,'coach_chat',lambda *a,**k:{'text':'Review below.','verified_facts':[], 'proposed_actions':[action]})
    response=http.post('/coach',json={'message':'Set my Shopping budget to 350','period':'all'})
    assert response.status_code==200
    stored=append.call_args.kwargs['verified_facts']
    assert stored[0]['action']['revision']==data['revision']
    monkeypatch.setattr(db,'get_chat_history',lambda *a,**k:[{'role':'assistant','message':'Review below.','verified_facts':stored}])
    current=http.get('/coach/history').json()['history'][0]
    assert current['proposed_actions'][0]['expired'] is False
    monkeypatch.setattr(db,'edit_state',lambda *a,**k:{'revision':'22222222-2222-2222-2222-222222222222'})
    old=http.get('/coach/history').json()['history'][0]
    assert old['proposed_actions'][0]['expired'] is True
    assert old['proposed_actions'][0]['revision']==data['revision']
    assert old['verified_facts']==[]


def completion(content='',calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content,tool_calls=calls))])


def test_action_request_forces_validated_tool_and_preserves_proposal_on_llm_failure(monkeypatch):
    call=SimpleNamespace(id='tool1',function=SimpleNamespace(name='propose_budget',arguments='{"category":"Shopping","amount":350}'))
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=[completion(calls=[call]),RuntimeError('network')]))))
    monkeypatch.setattr(ai_coach,'get_client',lambda:client)
    result=ai_coach.coach_chat('Set my Shopping budget to 350',{'spent':50,'income':100,'saved':50},transactions=[])
    assert result['proposed_actions'][0]['amount']==350
    assert 'Nothing changes until' in result['text']
    first=client.chat.completions.create.call_args_list[0].kwargs
    assert first['tool_choice']['function']['name']=='propose_budget'


def test_coach_cannot_promise_an_apply_card_without_a_structured_action(monkeypatch):
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=completion("I've proposed a change. Click Apply.")))))
    monkeypatch.setattr(ai_coach,'get_client',lambda:client)
    result=ai_coach.coach_chat('What can you do?',{'spent':50,'income':100,'saved':50})
    assert 'could not create' in result['text']
    assert result['proposed_actions']==[]


def test_refund_total_cannot_skip_calculation_and_invent_a_lower_amount(monkeypatch):
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(
        return_value=completion('Your spending was $100 less a $10 refund, so $90.')))))
    monkeypatch.setattr(ai_coach,'get_client',lambda:client)
    result=ai_coach.coach_chat('How much did I spend on groceries, after refunds?',
        {'spent':100,'income':2000,'saved':1900},transactions=ROWS)
    assert '$90' not in result['text']
    assert 'could not calculate' in result['text']
    assert client.chat.completions.create.call_args.kwargs['tool_choice']['function']['name']=='category_total'


@pytest.mark.parametrize('prompt', ['How much should I spend on groceries?',
    'Help me reduce spending', 'Explain what net spending means'])
def test_general_budgeting_advice_keeps_its_explanation(monkeypatch,prompt):
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(
        return_value=completion('Start with essentials and review your own priorities.')))))
    monkeypatch.setattr(ai_coach,'get_client',lambda:client)
    result=ai_coach.coach_chat(prompt,{'spent':100,'income':2000,'saved':1900},transactions=ROWS)
    assert result['text']=='Start with essentials and review your own priorities.'
    assert client.chat.completions.create.call_args.kwargs['tool_choice']=='auto'


def test_balance_facts_keep_manual_source_and_date_separate_from_statement():
    context={'available_balance':{'amount':9500,'as_of':'2026-10-29','source':'saved','is_current':True},
             'date_range':{'start':'2026-10-01','end':'2026-10-28'},'saved':2034.96}
    facts=ai_coach.calculated_facts(context,'What is my bank balance?')
    assert facts==[{'kind':'balance','amount':9500,'as_of':'2026-10-29','source':'saved',
                   'is_current':True,'provisional':None,'currency':'AUD'}]


def test_generated_and_cached_coach_copy_has_no_dash_punctuation():
    text='Income is **100**\u2014spending is 50 -- saved is 50.'
    cleaned=ai_coach._plain(text)
    assert '\u2014' not in cleaned and '--' not in cleaned
    assert '**100**' in cleaned
    assert '\u2014' not in ai_coach.strip_disclaimer({'text':text})['text']


def test_short_followup_uses_the_last_requested_action_without_autosaving():
    history=[{'role':'user','content':'Set my Shopping budget to 350'},
             {'role':'assistant','content':'Review the proposed change below.'}]
    assert ai_coach.requested_action_tool('Actually make it 400',history)=='propose_budget'
    assert ai_coach.requested_action_tool('What is my budget?',history) is None
    assert 'could not create' in ai_coach.proposal_reply('I’ve prepared your proposal. You can confirm it.',[])


@pytest.mark.parametrize('code,expected', [('P0001','Nothing was deleted'),(None,'Could not confirm deletion')])
def test_failed_deletion_never_reports_success_or_exposes_database_details(monkeypatch,code,expected):
    monkeypatch.setattr(db,'security_status',lambda _: {'session_active':True,'mfa_satisfied':True,'recent_auth':True})
    error=RuntimeError('private database message');error.code=code
    client=Mock();client.rpc.return_value.execute.side_effect=error
    monkeypatch.setattr(db,'get_client',lambda *a:client)
    main.app.dependency_overrides[main.upload_user]=lambda:AuthUser('synthetic','fake')
    try:
        result=TestClient(main.app).post('/data/delete',json={'confirmation':'DELETE'})
        assert result.status_code==503
        assert expected in result.json()['detail']
        assert 'private database' not in result.text
    finally:
        main.app.dependency_overrides.clear()
