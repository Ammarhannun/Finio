"""Fail closed around the shared backup stream, using invented bytes only."""
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from scripts import manual_backup as backup

PROJECT = 'abcdefghijklmnopqrst'
RECIPIENT = 'age1' + 'q' * 58  # Syntax fixture only; real checksums are checked by age.


@pytest.fixture
def encryptor(tmp_path):
    # The real age encryption/tamper/restore path is covered by recovery_drill.
    tool = tmp_path / 'test-encryptor'
    tool.write_text('#!' + sys.executable + '\nimport sys\nsys.stdout.buffer.write(b"TEST-ONLY:" + sys.stdin.buffer.read())\n')
    tool.chmod(0o700)
    return tool


def test_completed_stream_is_private_and_exclusive(tmp_path, encryptor):
    output = tmp_path / 'synthetic.dump.age'
    command = [sys.executable, '-c', 'print("invented financial fixture")']
    result = backup.encrypt_stream(command, encryptor, RECIPIENT, output)
    assert output.read_bytes() == b'TEST-ONLY:invented financial fixture\n'
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert result['encrypted_bytes'] == output.stat().st_size
    assert len(result['sha256']) == 64
    assert result['restore_verified'] is False
    with pytest.raises(ValueError):
        backup.encrypt_stream(command, encryptor, RECIPIENT, output)
    assert not list(tmp_path.glob('*.partial'))


@pytest.mark.parametrize('failure', ['producer', 'encryptor', 'timeout'])
def test_failure_never_publishes_or_keeps_partial(tmp_path, encryptor, capfd, failure):
    output = tmp_path / 'failed.dump.age'
    code = 'import sys; print("invented private payload"); sys.stderr.write("private provider failure"); sys.exit(1)'
    if failure == 'encryptor':
        encryptor.write_text('#!' + sys.executable + '\nimport sys\nsys.stderr.write("private encryption failure")\nsys.exit(1)\n')
        code = 'print("invented payload")'
    if failure == 'timeout':
        code = 'import time; time.sleep(10)'
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        backup.encrypt_stream([sys.executable, '-c', code], encryptor, RECIPIENT, output, timeout=0.3)
    assert not output.exists()
    assert not list(tmp_path.glob('*.partial'))
    assert 'private' not in ''.join(capfd.readouterr())


def test_refuses_unsafe_paths_and_symlinks(tmp_path):
    public = tmp_path / 'public'
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        backup.backup_destination(public / 'test.dump.age')
    with pytest.raises(ValueError):
        backup.backup_destination(tmp_path / 'plaintext.sql')
    target = tmp_path / 'already.dump.age'
    target.write_bytes(b'existing')
    link = tmp_path / 'link.dump.age'
    link.symlink_to(target)
    with pytest.raises(ValueError):
        backup.backup_destination(link)
    assert target.read_bytes() == b'existing'
    with pytest.raises(ValueError):
        backup.backup_destination(backup.ROOT / 'test.dump.age')


def test_recipient_refuses_private_identity_and_multiple_keys(tmp_path):
    recipient = tmp_path / 'recipient.txt'
    for content in ['AGE-SECRET-KEY-1PRIVATE', RECIPIENT + '\n' + RECIPIENT, 'ssh-ed25519 anything', 'x' * 300]:
        recipient.write_text(content)
        with pytest.raises(ValueError):
            backup.public_recipient(recipient)
    recipient.write_text(RECIPIENT + '\n')
    assert backup.public_recipient(recipient) == RECIPIENT


@pytest.mark.parametrize('host', ['https://db.example', 'db.example', 'localhost', 'user:password@host',
    'aws.pooler.supabase.com:6543', '-option.pooler.supabase.com', 'host.pooler.supabase.com/?secret'])
def test_refuses_connection_urls_ports_and_untrusted_hosts(host):
    with pytest.raises(ValueError):
        backup.connection(PROJECT, host)


def test_client_uses_verified_tls_and_read_only_database():
    direct_host, direct_user = backup.connection(PROJECT, None)
    assert (direct_host, direct_user) == (f'db.{PROJECT}.supabase.co', 'postgres')
    host, user = backup.connection(PROJECT, 'aws-0-ap-southeast-2.pooler.supabase.com')
    command = backup.dump_command(['docker'], 'synthetic-client', host, user, Path('/public/server.pem'))
    assert user == f'postgres.{PROJECT}'
    assert 'PGSSLMODE=verify-full' in command
    assert 'PGOPTIONS=-c default_transaction_read_only=on' in command
    assert command[command.index('--port') + 1] == '5432'
    assert 'PGPASSWORD' in command
    assert '--read-only' in command and '--cap-drop=ALL' in command
    assert '--pull=never' in command and '@sha256:' in backup.IMAGE
    assert not any(value.startswith('PGPASSWORD=') for value in command)
    assert not any('identity' in value or '.env' in value for value in command)


def test_private_password_is_not_an_argument_or_retained_environment(tmp_path, encryptor, monkeypatch):
    recipient = tmp_path / 'public.txt'
    recipient.write_text(RECIPIENT)
    certificate = tmp_path / 'public.pem'
    certificate.write_text('-----BEGIN CERTIFICATE-----\nsynthetic public certificate\n')
    monkeypatch.setattr(backup.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(backup.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(backup.getpass, 'getpass', lambda _: 'invented-password')
    monkeypatch.setenv('SUPABASE_SERVICE_KEY', 'do-not-inherit')
    captured = {}
    def stream(command, age, recipient, destination, **options):
        captured.update(command=command, environment=dict(options['env']), reference=options['env'])
        return {'restore_verified': False}
    monkeypatch.setattr(backup, 'encrypt_stream', stream)
    assert backup.main(['--project-ref', PROJECT, '--server-ca', str(certificate),
        '--recipient-file', str(recipient), '--age', str(encryptor),
        '--output', str(tmp_path / 'test.dump.age')]) == 0
    assert 'invented-password' not in ' '.join(captured['command'])
    assert captured['environment']['PGPASSWORD'] == 'invented-password'
    assert 'SUPABASE_SERVICE_KEY' not in captured['environment']
    assert 'PGPASSWORD' not in captured['reference']
