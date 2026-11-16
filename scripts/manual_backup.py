"""Manually stream a read-only Supabase dump into age encryption.

No plaintext dump is written. Run from a trusted operator computer, never the
web service. This command neither schedules backups nor restores a database.
"""
import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'pgvector/pgvector@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8'


def backup_destination(value):
    """Require an existing private directory outside source control."""
    path = Path(value).expanduser()
    parent = path.parent.resolve(strict=True)
    directory = parent.stat()
    if parent == ROOT or ROOT in parent.parents:
        raise ValueError('Backups must be outside the source repository.')
    if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077:
        raise ValueError('The backup directory must belong to you and have mode 700.')
    path = parent / path.name
    if not path.name.endswith('.dump.age') or path.exists() or path.is_symlink():
        raise ValueError('Choose a new filename ending in .dump.age. Existing files are never replaced.')
    return path


def public_recipient(value):
    path = Path(value).expanduser().resolve(strict=True)
    if path.stat().st_size > 256:
        raise ValueError('Use a file containing only one public age recipient.')
    recipient = path.read_text().strip()
    if not re.fullmatch(r'age1[023456789acdefghjklmnpqrstuvwxyz]{58}', recipient):
        raise ValueError('Use a file containing only one public age recipient, never a private key.')
    return recipient


def connection(project, host):
    if not re.fullmatch(r'[a-z]{20}', project):
        raise ValueError('Use the project reference from Supabase, not a connection URL.')
    host = host or f'db.{project}.supabase.co'
    if host == f'db.{project}.supabase.co':
        return host, 'postgres'
    if (len(host) <= 253 and host.endswith('.pooler.supabase.com')
            and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', label)
                    for label in host.split('.'))):
        return host, f'postgres.{project}'
    raise ValueError('Use the direct project host or the session pooler hostname from Connect.')


def docker_command(config=None, host=None):
    command = ['docker']
    if config:
        command += ['--config', str(Path(config).expanduser().resolve(strict=True))]
    if host:
        command += ['--host', host]
    return command


def dump_command(docker, name, host, user, certificate):
    # Only the public CA is mounted. No source tree, key, env file or Docker
    # socket is available inside the client. No password appears in argv.
    return docker + ['run', '--rm', '--pull=never', '--name', name,
        '--user', '65534:65534', '--read-only', '--cap-drop=ALL',
        '--security-opt=no-new-privileges', '--memory=256m', '--pids-limit=64',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
        '--mount', f'type=bind,source={certificate},target=/tmp/server-ca.pem,readonly',
        '-e', 'PGPASSWORD', '-e', 'PGSSLMODE=verify-full',
        '-e', 'PGSSLROOTCERT=/tmp/server-ca.pem', '-e', 'PGCONNECT_TIMEOUT=10',
        '-e', 'PGOPTIONS=-c default_transaction_read_only=on',
        '--entrypoint', 'pg_dump', IMAGE, '--host', host, '--port', '5432',
        '--username', user, '--dbname', 'postgres', '--format=custom',
        '--no-owner', '--no-password', '--lock-wait-timeout=10000']


def encrypt_stream(command, age, recipient, destination, *, env=None, timeout=600):
    """Shared by the manual command and isolated restore drill.

    Stderr is discarded because database failures can contain private values.
    Publish a complete encrypted file only after both processes succeed.
    """
    destination = backup_destination(destination)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.finio-backup-', suffix='.partial', dir=destination.parent)
    temporary = Path(temporary_name)
    dump = encrypt = None
    started = time.monotonic()
    try:
        with os.fdopen(descriptor, 'wb') as output:
            dump = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            encrypt = subprocess.Popen([str(age), '-r', recipient], stdin=dump.stdout,
                stdout=output, stderr=subprocess.DEVNULL)
            dump.stdout.close()
            encrypted_status = encrypt.wait(timeout=timeout)
            dump_status = dump.wait(timeout=max(0.01, timeout - (time.monotonic() - started)))
            if encrypted_status or dump_status:
                raise RuntimeError('Backup failed. No complete backup was published.')
            output.flush()
            os.fsync(output.fileno())
        # An atomic, exclusive publish also protects against a concurrent run
        # choosing the same destination. Never overwrite or follow a symlink.
        os.link(temporary, destination)
    finally:
        for process in (encrypt, dump):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        temporary.unlink(missing_ok=True)
    with destination.open('rb') as encrypted:
        checksum = hashlib.file_digest(encrypted, 'sha256').hexdigest()
    return {'encrypted_bytes': destination.stat().st_size, 'sha256': checksum,
        'created_utc': datetime.now(timezone.utc).isoformat(), 'restore_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-ref', required=True)
    parser.add_argument('--db-host', help='Omit for direct connection; use a session pooler host for IPv4. Port is always 5432.')
    parser.add_argument('--server-ca', required=True, help='Public PEM server root certificate downloaded from Supabase.')
    parser.add_argument('--recipient-file', required=True, help='One public age recipient, not the private identity.')
    parser.add_argument('--age', required=True, help='Path to a trusted official age executable.')
    parser.add_argument('--output', required=True, help='New .dump.age file in an existing private directory outside the repo.')
    parser.add_argument('--docker-config')
    parser.add_argument('--docker-host')
    args = parser.parse_args(argv)
    docker = None
    name = 'finio-backup-' + uuid4().hex[:12]
    def cancel(_signum, _frame):
        raise KeyboardInterrupt
    previous_termination_handler = signal.signal(signal.SIGTERM, cancel)
    try:
        destination = backup_destination(args.output)
        recipient = public_recipient(args.recipient_file)
        host, user = connection(args.project_ref, args.db_host)
        certificate = Path(args.server_ca).expanduser().resolve(strict=True)
        if (',' in str(certificate) or certificate.stat().st_size > 131072
                or b'-----BEGIN CERTIFICATE-----' not in certificate.read_bytes()):
            raise ValueError('Use the public PEM server root certificate downloaded from Supabase.')
        age = Path(args.age).expanduser().resolve(strict=True)
        if not age.is_file() or not os.access(age, os.X_OK):
            raise ValueError('Provide a trusted official age executable.')
        # Validate the recipient checksum before prompting for credentials or
        # connecting to the database. Nothing sensitive enters this process.
        subprocess.run([str(age), '-r', recipient], input=b'', stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=True)
        docker = docker_command(args.docker_config, args.docker_host)
        subprocess.run(docker + ['image', 'inspect', IMAGE], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=True)
        if not sys.stdin.isatty():
            raise ValueError('Run interactively in your terminal so the password stays hidden.')
        password = getpass.getpass('Supabase DATABASE password (hidden, not your login or API key): ')
        if not password or '\n' in password or '\r' in password:
            raise ValueError('Enter the database password privately in the terminal.')
        # Docker needs the password briefly in the client environment. Local
        # Docker administrators can inspect it, so use a trusted operator host.
        environment = {key: value for key, value in os.environ.items()
            if key in ('PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONFIG', 'DOCKER_CONTEXT', 'TMPDIR')}
        environment['PGPASSWORD'] = password
        password = None
        try:
            result = encrypt_stream(dump_command(docker, name, host, user, certificate),
                age, recipient, destination, env=environment)
        finally:
            environment.pop('PGPASSWORD', None)
        print(json.dumps({'passed': True, 'file': str(destination), **result,
            'note': 'Encrypted dump created. Copy it off-host and verify an isolated restore.'}))
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt, EOFError):
        print('Backup failed or was cancelled. No complete new backup was published. Check the connection, CA, tool versions and permissions privately.', file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_termination_handler)
        if docker:
            # The Docker CLI may be interrupted while its container survives.
            # Remove only this run's uniquely named temporary client.
            try:
                subprocess.run(docker + ['rm', '-f', name], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=15)
            except (OSError, subprocess.SubprocessError):
                print('Check Docker for this temporary client: ' + name, file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())
