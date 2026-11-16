"""Availability alerts carry no financial data and never hide failed delivery."""
from unittest.mock import Mock
import json
import ssl

import pytest
from scripts import monitor_service as monitor

CONFIG = {'base_url':'https://synthetic.invalid', 'smtp_host':'smtp.example.invalid',
          'smtp_username':'synthetic', 'smtp_mode':'ssl', 'smtp_port':465,
          'from_email':'monitor@example.invalid', 'to_email':'owner@example.invalid'}
GOOD = {'healthy':True, 'checks':{'liveness':200, 'readiness':200}}
BAD = {'healthy':False, 'checks':{'liveness':200, 'readiness':503}}


def test_notify_only_on_failure_or_recovery_and_retry_failed_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv('FINIO_ALERT_SMTP_PASSWORD','synthetic-only')
    state=tmp_path/'monitor.json';send=Mock()
    for value, alerted in [(GOOD,False),(BAD,True),(BAD,False),(GOOD,True),(GOOD,False)]:
        result=monitor.run(CONFIG,state,probe=lambda _:value,send=send)
        assert result['alert_sent'] is alerted
    assert send.call_count==2
    send.side_effect=RuntimeError('private recipient/provider error')
    with pytest.raises(RuntimeError):
        monitor.run(CONFIG,state,probe=lambda _:BAD,send=send)
    assert json.loads(state.read_text())==GOOD
    send.side_effect=None
    assert monitor.run(CONFIG,state,probe=lambda _:BAD,send=send)['alert_sent']
    assert state.stat().st_mode & 0o077 == 0


def test_first_unhealthy_check_alerts_and_missing_credentials_do_not_contact_a_provider(tmp_path,monkeypatch):
    monkeypatch.delenv('FINIO_ALERT_SMTP_PASSWORD',raising=False)
    send=Mock();probe=Mock(return_value=BAD)
    with pytest.raises(ValueError):
        monitor.run(CONFIG,tmp_path/'state',probe=probe,send=send)
    send.assert_not_called();probe.assert_not_called()
    monkeypatch.setenv('FINIO_ALERT_SMTP_PASSWORD','synthetic-only')
    assert monitor.run(CONFIG,tmp_path/'state',probe=probe,send=send)['alert_sent']


@pytest.mark.parametrize('change',[{'smtp_mode':'plain','smtp_port':25},
    {'to_email':'victim@example.invalid\nBcc:other@example.invalid'},
    {'from_email':'one@example.invalid,two@example.invalid'}, {'smtp_host':''}])
def test_insecure_transport_and_header_injection_are_rejected(change):
    with pytest.raises(ValueError):monitor.validate({**CONFIG,**change})


@pytest.mark.parametrize('mode,port',[('ssl',465),('starttls',587)])
def test_delivery_requires_verified_tls_and_contains_only_status_metadata(monkeypatch,mode,port):
    smtp=Mock();smtp.__enter__=Mock(return_value=smtp);smtp.__exit__=Mock(return_value=False)
    smtp.send_message.return_value={}
    factory=Mock(return_value=smtp)
    monkeypatch.setattr(monitor.smtplib,'SMTP_SSL' if mode=='ssl' else 'SMTP',factory)
    monitor.notify({**CONFIG,'smtp_mode':mode,'smtp_port':port},BAD,'synthetic-only')
    context=factory.call_args.kwargs.get('context') if mode=='ssl' else smtp.starttls.call_args.kwargs['context']
    assert context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED
    message=smtp.send_message.call_args.args[0]
    assert message['To']==CONFIG['to_email']
    assert '503' in message.get_content() and 'synthetic-only' not in message.as_string()
    assert 'synthetic.invalid' not in message.as_string()
    smtp.login.assert_called_once_with('synthetic','synthetic-only')
