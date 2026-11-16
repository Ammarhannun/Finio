"""Run untrusted PDF parsing in a bounded process with a minimal environment.

Linux additionally enforces filesystem and syscall restrictions. Production
fails closed when the sandbox cannot start. Host restrictions remain necessary.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pandas as pd

from config import MAX_UPLOAD_BYTES, MAX_TRANSACTION_ROWS
from modules.bank_parser import StatementRejected

PDF_TIMEOUT_SECONDS = 20
MAX_RESULT_BYTES = 8 * 1024 * 1024
_WORKER = Path(__file__).with_name('pdf_worker.py')
_LIMIT_MESSAGE = 'The PDF could not be processed within the safety limits. Export a shorter statement or a CSV.'


def extract_pdf(path):
    path = Path(path).resolve()
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise StatementRejected('The PDF must be under 10 MB.')
    with path.open('rb') as source:
        if source.read(5) != b'%PDF-':
            raise StatementRejected('This file does not have a valid PDF header. Export a CSV or a text-based PDF.')
    # Do not forward OpenAI/Supabase credentials, HOME, PYTHONPATH or other
    # application configuration to the parser process.
    environment = {'LANG': 'C.UTF-8', 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1',
                   'NUMEXPR_NUM_THREADS': '1'}
    if os.name == 'nt' and os.getenv('SYSTEMROOT'):
        environment['SYSTEMROOT'] = os.environ['SYSTEMROOT']
    with tempfile.TemporaryFile() as output, tempfile.TemporaryDirectory(prefix='finio-pdf-') as directory:
        try:
            result = subprocess.run(
                [sys.executable, '-I', str(_WORKER), str(path),
                 'required' if os.getenv('FINIO_ENV') == 'production' else 'development'],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                cwd=directory, env=environment, timeout=PDF_TIMEOUT_SECONDS, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise StatementRejected(_LIMIT_MESSAGE) from exc
        if result.returncode != 0:
            raise StatementRejected(_LIMIT_MESSAGE)
        output.seek(0)
        encoded = output.read(MAX_RESULT_BYTES + 1)
    if len(encoded) > MAX_RESULT_BYTES:
        raise StatementRejected(_LIMIT_MESSAGE)
    try:
        payload = json.loads(encoded)
        if payload.get('ok') is not True:
            raise StatementRejected(payload.get('reason') or 'Could not read this PDF. Export a CSV instead.')
        rows = payload['rows']
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_TRANSACTION_ROWS:
            raise ValueError('Invalid transaction count')
        return pd.DataFrame(rows)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise StatementRejected('Could not read this PDF. Export a CSV instead.') from exc
