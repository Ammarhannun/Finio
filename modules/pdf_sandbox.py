"""Linux kernel restrictions for the already-imported, single-thread PDF worker.

Landlock restricts file access to the input and Python runtime. Seccomp denies
network creation, execution, process inspection and new processes. Unsupported
production systems fail closed. This does not replace host/kernel maintenance.
"""
import ctypes
import errno
import os
from pathlib import Path
import platform
import sys
import sysconfig


class SandboxUnavailable(RuntimeError):
    pass


class Ruleset(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def restrict_pdf_worker(input_path):
    if not sys.platform.startswith('linux'):
        raise SandboxUnavailable('PDF sandbox requires Linux')
    if platform.machine() not in ('aarch64', 'x86_64'):
        raise SandboxUnavailable('Unsupported sandbox architecture')
    if len(list(Path('/proc/self/task').iterdir())) != 1:
        raise SandboxUnavailable('The PDF worker must be single-threaded')
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        seccomp = ctypes.CDLL('libseccomp.so.2', use_errno=True)
        seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
        seccomp.seccomp_init.restype = ctypes.c_void_p
        seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
        seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
        seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
        seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
        abi = libc.syscall(444, 0, 0, 1)
        if abi < 3:
            raise SandboxUnavailable('Landlock ABI 3 or newer is required')
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(4, 0, 0, 0, 0) != 0:
            raise SandboxUnavailable('Could not restrict process privileges')
        rules = Ruleset((1 << 15) - 1)
        fd = libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0)
        if fd < 0:
            raise SandboxUnavailable('Could not create the file policy')
        try:
            runtime = {Path(sysconfig.get_path(name)).resolve() for name in ('stdlib','platstdlib','purelib','platlib')}
            runtime.update(Path(name).resolve() for name in ('/lib','/lib64','/usr/lib') if Path(name).exists())
            # Never allow the source tree, home, temporary directory or proc.
            paths = [(path, (1 << 2) | (1 << 3)) for path in runtime]
            paths.append((Path(input_path).resolve(strict=True), 1 << 2))
            for path, rights in paths:
                opened = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    rule = PathRule(rights, opened)
                    if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) != 0:
                        raise SandboxUnavailable('Could not add a file rule')
                finally:
                    os.close(opened)
            if libc.syscall(446, fd, 0) != 0:
                raise SandboxUnavailable('Could not enforce the file policy')
        finally:
            os.close(fd)
        context = seccomp.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
        if not context:
            raise SandboxUnavailable('Could not create the syscall policy')
        try:
            # CTL_TSYNC applies the filter to all threads as defense in depth.
            if seccomp.seccomp_attr_set(context, 4, 1) != 0:
                raise SandboxUnavailable('Could not synchronize the syscall policy')
            denied = ('socket socketpair connect bind listen accept accept4 shutdown '
                'sendto recvfrom sendmsg recvmsg sendmmsg recvmmsg setsockopt getsockopt '
                'execve execveat fork vfork clone clone3 ptrace process_vm_readv '
                'process_vm_writev pidfd_open pidfd_getfd pidfd_send_signal '
                'kill tkill tgkill mount umount2 pivot_root chroot unshare setns bpf '
                'open_by_handle_at name_to_handle_at io_uring_setup userfaultfd').split()
            for name in denied:
                number = seccomp.seccomp_syscall_resolve_name(name.encode())
                if number >= 0 and seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                    raise SandboxUnavailable('Could not add a syscall rule')
            if seccomp.seccomp_load(context) != 0:
                raise SandboxUnavailable('Could not enforce the syscall policy')
        finally:
            seccomp.seccomp_release(context)
        return {'filesystem': 'landlock', 'syscalls': 'seccomp', 'landlock_abi': abi}
    except (OSError, AttributeError) as exc:
        raise SandboxUnavailable('The PDF sandbox is unavailable') from exc
