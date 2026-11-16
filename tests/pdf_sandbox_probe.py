"""Run inside the release Linux image, with no real files or credentials mounted.

docker run --rm --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=64m
  -v <this-file>:/verification/probe.py:ro IMAGE python /verification/probe.py
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

sys.path.insert(0, '/app')
from modules.pdf_sandbox import restrict_pdf_worker
from modules.pdf_processing import extract_pdf


def fixture(path):
    stream = b'BT /F1 10 Tf 40 740 Td (Opening balance 1000.00) Tj 0 -20 Td (01/06/2026 COLES 110.00 890.00) Tj 0 -20 Td (02/06/2026 COLES REFUND 10.00 CR 900.00) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data = b'%PDF-1.4\n'; offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data)); data += f'{number} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref = len(data); data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    data += f'trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(data)


def main():
    assert os.getuid() != 0
    source = Path('/tmp/statement.pdf'); fixture(source)
    frame = extract_pdf(source)
    assert frame.amount.tolist() == [-110, 10]
    assert frame.balance.tolist() == [890, 900]
    assert not any(name.startswith(('OPENAI_', 'SUPABASE_')) for name in os.environ)
    secret = Path('/tmp/sibling-canary'); secret.write_text('synthetic only')
    link = Path('/tmp/input-link'); link.symlink_to(secret)
    # Preload libraries and syscall numbers before restricting file access.
    libc = ctypes.CDLL(None, use_errno=True)
    seccomp = ctypes.CDLL('libseccomp.so.2')
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    numbers = {name:seccomp.seccomp_syscall_resolve_name(name.encode())
               for name in ['ptrace','process_vm_readv','io_uring_setup','execve','clone','pidfd_open']}
    metadata = restrict_pdf_worker(source)
    assert source.read_bytes().startswith(b'%PDF-')
    passed = ['nonroot', 'real-pdf-refund-balance', 'minimal-worker-environment', 'input-read']
    def denied(name, operation):
        try:
            result = operation()
            if hasattr(result, 'close'): result.close()
        except PermissionError:
            passed.append(name);return
        raise AssertionError('Sandbox permitted '+name)
    for label, path in [('sibling',secret), ('symlink',link), ('source','/app/main.py'),
                        ('proc-environment','/proc/self/environ'), ('parent-memory','/proc/1/mem')]:
        denied(label+'-read', lambda path=path: Path(path).read_bytes())
    denied('sibling-write', lambda: secret.write_text('overwrite'))
    denied('input-write', lambda: source.write_bytes(b'overwrite'))
    denied('new-file', lambda: Path('/tmp/new-file').write_text('new'))
    for label, family in [('ipv4',socket.AF_INET),('ipv6',socket.AF_INET6),('unix',socket.AF_UNIX)]:
        denied(label+'-socket', lambda family=family: socket.socket(family, socket.SOCK_STREAM))
    denied('subprocess', lambda: subprocess.run(['/bin/true'], check=True))
    for name, number in numbers.items():
        if number < 0: continue
        ctypes.set_errno(0)
        result = libc.syscall(number, 0, 0, 0, 0, 0, 0)
        assert result == -1 and ctypes.get_errno() == errno.EPERM, name
        passed.append(name+'-syscall')
    print(json.dumps({'passed':passed, 'sandbox':metadata}))


if __name__ == '__main__':
    main()
