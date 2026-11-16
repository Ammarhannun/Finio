"""One scheduled health check with private TLS email alerts on state changes.

This does not install a scheduler. Keep config and state outside the repository.
SMTP credentials are read from FINIO_ALERT_SMTP_PASSWORD, never command arguments.
"""
import argparse
from contextlib import contextmanager
from email.message import EmailMessage
import fcntl
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import tempfile

from scripts.check_service import check_service


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', value):
        raise ValueError('Use a single email address without display names.')
    return value


def validate(config):
    for key in ('smtp_host', 'smtp_username'):
        value = config.get(key)
        if not isinstance(value, str) or not value or any(c.isspace() for c in value):
            raise ValueError('Set the SMTP host and username.')
    mode = config.get('smtp_mode')
    if (mode, config.get('smtp_port')) not in (('ssl', 465), ('starttls', 587)):
        raise ValueError('Use SMTP SSL on port 465 or mandatory STARTTLS on port 587.')
    address(config.get('from_email'))
    address(config.get('to_email'))
    return config


def notify(config, result, password):
    message = EmailMessage()
    message['From'] = config['from_email']
    message['To'] = config['to_email']
    message['Subject'] = 'Finio service recovered' if result['healthy'] else 'Finio service check failed'
    message.set_content('Finio availability check.\n'
        f"Liveness: {result['checks']['liveness']}\nReadiness: {result['checks']['readiness']}\n"
        'No customer data, response bodies or credentials are included.\n'
        'Review the private host dashboard and incident procedure if action is needed.\n')
    context = ssl.create_default_context()
    if config['smtp_mode'] == 'ssl':
        connection = smtplib.SMTP_SSL(config['smtp_host'], 465, timeout=10, context=context)
    else:
        connection = smtplib.SMTP(config['smtp_host'], 587, timeout=10)
    with connection as smtp:
        if config['smtp_mode'] == 'starttls':
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(config['smtp_username'], password)
        # send_message reports refusals even when some recipients were accepted.
        if smtp.send_message(message):
            raise RuntimeError('Alert recipient was refused')


def write_state(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.finio-monitor-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def exclusive(path):
    fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def run(config, state_file, *, probe=check_service, send=notify):
    validate(config)
    password = os.getenv('FINIO_ALERT_SMTP_PASSWORD', '')
    if not password:
        raise ValueError('Configure SMTP credentials in the protected scheduler environment.')
    state_file = Path(state_file)
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with exclusive(state_file):
        # Keep probes ordered too: an older overlapping check must not replace
        # a newer result or send a false recovery alert.
        result = probe(config['base_url'])  # Rejects unsafe origins and redirects.
        previous = json.loads(state_file.read_text()) if state_file.exists() else None
        changed = (previous is not None and previous['healthy'] != result['healthy'])
        alert = changed or (previous is None and not result['healthy'])
        if alert:
            send(config, result, password)
        # Failed delivery never advances the state, so the next check retries.
        write_state(state_file, result)
    return {'healthy': result['healthy'], 'checks': result['checks'], 'alert_sent': alert}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.config.stat().st_mode & 0o077:
            raise ValueError('Keep the private config readable only by its owner (chmod 600).')
        config = json.loads(args.config.read_text())
        result = run(config, args.config.with_suffix('.state.json'))
    except Exception:
        # SMTP errors can contain recipients or provider diagnostics. Do not log them.
        print(json.dumps({'configured_or_delivered': False,
                          'message': 'Monitoring failed. Check the protected config, network and SMTP setup.'}))
        return 2
    print(json.dumps(result))
    return 0 if result['healthy'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
