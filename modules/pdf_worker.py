"""Private parser-process entry point. Never import the web app or its secrets."""
from pathlib import Path
import json
import sys


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        if sys.platform.startswith('linux') and hasattr(resource, 'RLIMIT_DATA'):
            resource.setrlimit(resource.RLIMIT_DATA, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except ImportError:
        # Windows still receives wall-time, input and result-size limits.
        pass
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from modules.bank_parser import StatementRejected
    from modules.pdf_parser import extract_transactions
    from modules.serialization import json_safe
    from modules.pdf_sandbox import restrict_pdf_worker, SandboxUnavailable
    required = len(sys.argv) > 2 and sys.argv[2] == 'required'
    try:
        restrict_pdf_worker(sys.argv[1])
    except SandboxUnavailable:
        if required or sys.platform.startswith('linux'):
            sys.stdout.write(json.dumps({'ok': False, 'reason': 'PDF processing is unavailable because its security sandbox could not start. Upload a CSV instead.'}))
            return
    try:
        frame = extract_transactions(sys.argv[1])
        payload = {'ok': True, 'rows': json_safe(frame.to_dict('records'))}
    except StatementRejected as exc:
        payload = {'ok': False, 'reason': str(exc)}
    except Exception:
        payload = {'ok': False, 'reason': 'Could not read this PDF. Export a CSV or an unlocked text-based PDF.'}
    sys.stdout.write(json.dumps(payload, allow_nan=False))


if __name__ == '__main__':
    main()
