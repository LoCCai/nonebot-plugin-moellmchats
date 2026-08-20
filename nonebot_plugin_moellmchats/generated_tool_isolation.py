"""Linux isolation launcher for the stdlib-only Generated Tool worker.

This file deliberately imports no project or third-party modules.  It starts as
PID 1 in a fresh PID and mount namespace created by ``unshare``.  Before the
worker source is opened it makes the host mount tree recursively read-only,
optionally restores the private workspace bind mount as writable, and applies a
Landlock read allow-list for tools without ``host_filesystem`` capability.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import runpy
import stat
import sys
import sysconfig
from typing import Final

_CAPABILITY_FIELDS: Final = frozenset(
    {"network", "process", "workspace", "host_filesystem", "secrets"}
)

_AT_FDCWD = -100
_AT_RECURSIVE = 0x8000
_MS_BIND = 4096
_MS_REC = 16384
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MOUNT_ATTR_RDONLY = 0x00000001
_PR_SET_NO_NEW_PRIVS = 38

_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_SCMP_CMP_NE = 1
_SCMP_CMP_EQ = 4
_SCMP_CMP_MASKED_EQ = 7
_AF_UNIX = 1
_AF_VSOCK = 40
_SOCK_STREAM = 1
_SOCK_TYPE_MASK = 0xF
_SANDBOX_HOSTNAME = b"moellm-sandbox"
_SANDBOX_DOMAINNAME = b"localdomain"
_KEYRING_SYSCALLS = ("add_key", "request_key", "keyctl")
_XATTR_READ_SYSCALLS = (
    "getxattr",
    "lgetxattr",
    "fgetxattr",
    "listxattr",
    "llistxattr",
    "flistxattr",
)

_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
_LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
_LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
_LANDLOCK_ABI1_RIGHTS = (
    _LANDLOCK_ACCESS_FS_EXECUTE
    | _LANDLOCK_ACCESS_FS_WRITE_FILE
    | _LANDLOCK_ACCESS_FS_READ_FILE
    | _LANDLOCK_ACCESS_FS_READ_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_FILE
    | _LANDLOCK_ACCESS_FS_MAKE_CHAR
    | _LANDLOCK_ACCESS_FS_MAKE_DIR
    | _LANDLOCK_ACCESS_FS_MAKE_REG
    | _LANDLOCK_ACCESS_FS_MAKE_SOCK
    | _LANDLOCK_ACCESS_FS_MAKE_FIFO
    | _LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | _LANDLOCK_ACCESS_FS_MAKE_SYM
)
_LANDLOCK_RUNTIME_DIRECTORY_RIGHTS = (
    _LANDLOCK_ACCESS_FS_EXECUTE
    | _LANDLOCK_ACCESS_FS_READ_FILE
    | _LANDLOCK_ACCESS_FS_READ_DIR
)
_LANDLOCK_RUNTIME_FILE_RIGHTS = (
    _LANDLOCK_ACCESS_FS_EXECUTE | _LANDLOCK_ACCESS_FS_READ_FILE
)


class IsolationUnavailable(RuntimeError):
    """A mandatory kernel isolation primitive is unavailable or misconfigured."""


class _MountAttr(ctypes.Structure):
    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


class _ScmpArgCmp(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint32),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.mount.argtypes = [
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_void_p,
]
_LIBC.mount.restype = ctypes.c_int
_LIBC.prctl.argtypes = [
    ctypes.c_int,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
]
_LIBC.prctl.restype = ctypes.c_int
_LIBC.sethostname.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
_LIBC.sethostname.restype = ctypes.c_int
_LIBC.setdomainname.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
_LIBC.setdomainname.restype = ctypes.c_int
_LIBC.syscall.restype = ctypes.c_long


def _syscall_numbers() -> tuple[int, int, int, int]:
    machine = platform.machine().lower()
    if machine not in {
        "x86_64",
        "amd64",
        "aarch64",
        "arm64",
        "riscv64",
    }:
        raise IsolationUnavailable(
            f"unsupported Linux syscall architecture: {machine or 'unknown'}"
        )
    # These APIs use the asm-generic numbers on all supported architectures.
    return 442, 444, 445, 446


def _raise_errno(action: str) -> None:
    error_number = ctypes.get_errno()
    detail = os.strerror(error_number) if error_number else "unknown error"
    raise IsolationUnavailable(
        f"{action} failed: [Errno {error_number}] {detail}"
    )


def _mount(
    source: str | None,
    target: str,
    filesystem: str | None,
    flags: int,
) -> None:
    result = _LIBC.mount(
        os.fsencode(source) if source is not None else None,
        os.fsencode(target),
        os.fsencode(filesystem) if filesystem is not None else None,
        flags,
        None,
    )
    if result != 0:
        _raise_errno(f"mount({target})")


def _mount_set_readonly(path: str, *, readonly: bool, recursive: bool) -> None:
    mount_setattr, _, _, _ = _syscall_numbers()
    attributes = _MountAttr(
        attr_set=_MOUNT_ATTR_RDONLY if readonly else 0,
        attr_clr=0 if readonly else _MOUNT_ATTR_RDONLY,
        propagation=0,
        userns_fd=0,
    )
    flags = _AT_RECURSIVE if recursive else 0
    result = _LIBC.syscall(
        mount_setattr,
        _AT_FDCWD,
        os.fsencode(path),
        flags,
        ctypes.byref(attributes),
        ctypes.sizeof(attributes),
    )
    if result != 0:
        _raise_errno(f"mount_setattr({path}, readonly={readonly})")


def _parse_capabilities() -> dict[str, bool]:
    raw = os.environ.pop("MOELLM_RUNNER_CAPABILITIES", "")
    try:
        capabilities = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise IsolationUnavailable("runner capabilities are not valid JSON") from error
    if not isinstance(capabilities, dict) or set(capabilities) != _CAPABILITY_FIELDS:
        raise IsolationUnavailable("runner capabilities must contain exactly five fields")
    if not all(type(value) is bool for value in capabilities.values()):
        raise IsolationUnavailable("runner capabilities must be explicit booleans")
    return capabilities


def _require_namespaces() -> None:
    parent_mount = os.environ.pop("MOELLM_RUNNER_PARENT_MOUNT_NS", "")
    parent_pid = os.environ.pop("MOELLM_RUNNER_PARENT_PID_NS", "")
    parent_ipc = os.environ.pop("MOELLM_RUNNER_PARENT_IPC_NS", "")
    parent_uts = os.environ.pop("MOELLM_RUNNER_PARENT_UTS_NS", "")
    try:
        current_mount = os.readlink("/proc/self/ns/mnt")
        current_pid = os.readlink("/proc/self/ns/pid")
        current_ipc = os.readlink("/proc/self/ns/ipc")
        current_uts = os.readlink("/proc/self/ns/uts")
    except OSError as error:
        raise IsolationUnavailable(f"cannot inspect isolation namespaces: {error}") from error
    if not parent_mount or current_mount == parent_mount:
        raise IsolationUnavailable("dedicated mount namespace is unavailable")
    if not parent_pid or current_pid == parent_pid or os.getpid() != 1:
        raise IsolationUnavailable("PID namespace init boundary is unavailable")
    if not parent_ipc or current_ipc == parent_ipc:
        raise IsolationUnavailable("dedicated IPC namespace is unavailable")
    if not parent_uts or current_uts == parent_uts:
        raise IsolationUnavailable("dedicated UTS namespace is unavailable")


def _set_isolated_uts_identity() -> None:
    if _LIBC.sethostname(_SANDBOX_HOSTNAME, len(_SANDBOX_HOSTNAME)) != 0:
        _raise_errno("sethostname")
    if _LIBC.setdomainname(_SANDBOX_DOMAINNAME, len(_SANDBOX_DOMAINNAME)) != 0:
        _raise_errno("setdomainname")


def _query_landlock_abi() -> int:
    _, create_ruleset, _, _ = _syscall_numbers()
    ctypes.set_errno(0)
    abi = _LIBC.syscall(
        create_ruleset,
        0,
        0,
        _LANDLOCK_CREATE_RULESET_VERSION,
    )
    if abi < 1:
        _raise_errno("landlock ABI query")
    return int(abi)


def _open_path(path: Path, *, required: bool) -> tuple[int, bool] | None:
    flags = getattr(os, "O_PATH", 0o10000000) | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if required:
            raise IsolationUnavailable(f"required Landlock path is absent: {path}") from None
        return None
    except OSError as error:
        if required:
            raise IsolationUnavailable(
                f"cannot open required Landlock path {path}: {error}"
            ) from error
        return None
    mode = os.fstat(descriptor).st_mode
    if stat.S_ISDIR(mode):
        return descriptor, True
    if stat.S_ISREG(mode) or stat.S_ISCHR(mode):
        return descriptor, False
    os.close(descriptor)
    if required:
        raise IsolationUnavailable(f"unsupported required Landlock path: {path}")
    return None


def _add_landlock_rule(
    ruleset_fd: int,
    path: Path,
    rights: int,
    *,
    required: bool,
) -> None:
    opened = _open_path(path, required=required)
    if opened is None:
        return
    descriptor, is_directory = opened
    if not is_directory:
        rights &= (
            _LANDLOCK_ACCESS_FS_EXECUTE
            | _LANDLOCK_ACCESS_FS_READ_FILE
            | _LANDLOCK_ACCESS_FS_WRITE_FILE
        )
    try:
        _, _, add_rule, _ = _syscall_numbers()
        rule = _LandlockPathBeneathAttr(
            allowed_access=rights,
            parent_fd=descriptor,
            reserved=0,
        )
        result = _LIBC.syscall(
            add_rule,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(rule),
            0,
        )
        if result != 0:
            _raise_errno(f"landlock rule({path})")
    finally:
        os.close(descriptor)


def _runtime_directories() -> tuple[Path, ...]:
    candidates: set[Path] = set()
    for value in sys.path:
        if value:
            candidates.add(Path(value).absolute())
    paths = sysconfig.get_paths()
    for name in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = paths.get(name)
        if value:
            candidates.add(Path(value).absolute())
    for value in (
        "/lib",
        "/lib64",
        "/usr/lib",
        "/usr/lib64",
        "/usr/share/zoneinfo",
        "/etc/ssl/certs",
    ):
        candidates.add(Path(value))
    return tuple(sorted(candidates, key=os.fspath))


def _process_directories() -> tuple[Path, ...]:
    """Return the fixed executable roots granted by ``process=true``.

    The worker PATH is fixed to the same three roots.  In particular, this
    list must never be derived from the parent process environment: doing so
    could turn an inherited virtualenv or application directory into an
    accidental host-filesystem read grant.
    """

    return (Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin"))


def _runtime_files(worker_path: Path) -> tuple[tuple[Path, int, bool], ...]:
    read_only = _LANDLOCK_ACCESS_FS_READ_FILE
    executable = _LANDLOCK_RUNTIME_FILE_RIGHTS
    read_write = _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_WRITE_FILE
    return (
        (worker_path, read_only, True),
        (Path(sys.executable), executable, True),
        (Path("/etc/ld.so.cache"), read_only, False),
        (Path("/etc/localtime"), read_only, False),
        (Path("/etc/resolv.conf"), read_only, False),
        (Path("/etc/hosts"), read_only, False),
        (Path("/etc/nsswitch.conf"), read_only, False),
        (Path("/etc/gai.conf"), read_only, False),
        (Path("/etc/services"), read_only, False),
        (Path("/proc/sys/kernel/domainname"), read_only, True),
        (Path("/dev/null"), read_write, True),
        (Path("/dev/urandom"), read_only, False),
        (Path("/dev/random"), read_only, False),
    )


def _install_landlock(
    workspace: Path,
    worker_path: Path,
    *,
    writable: bool,
    allow_process: bool,
) -> None:
    _query_landlock_abi()
    _, create_ruleset, _, restrict_self = _syscall_numbers()
    ruleset = _LandlockRulesetAttr(handled_access_fs=_LANDLOCK_ABI1_RIGHTS)
    ruleset_fd = _LIBC.syscall(
        create_ruleset,
        ctypes.byref(ruleset),
        ctypes.sizeof(ruleset),
        0,
    )
    if ruleset_fd < 0:
        _raise_errno("landlock ruleset creation")
    try:
        workspace_rights = (
            _LANDLOCK_ABI1_RIGHTS
            if writable
            else _LANDLOCK_RUNTIME_DIRECTORY_RIGHTS
        )
        _add_landlock_rule(
            ruleset_fd,
            workspace,
            workspace_rights,
            required=True,
        )
        for path in _runtime_directories():
            _add_landlock_rule(
                ruleset_fd,
                path,
                _LANDLOCK_RUNTIME_DIRECTORY_RIGHTS,
                required=False,
            )
        if allow_process:
            process_rights = (
                _LANDLOCK_ACCESS_FS_EXECUTE | _LANDLOCK_ACCESS_FS_READ_FILE
            )
            for path in _process_directories():
                _add_landlock_rule(
                    ruleset_fd,
                    path,
                    process_rights,
                    required=False,
                )
        for path in (Path("/proc/self"), Path("/proc/thread-self")):
            _add_landlock_rule(
                ruleset_fd,
                path,
                _LANDLOCK_RUNTIME_DIRECTORY_RIGHTS,
                required=False,
            )
        for path, rights, required in _runtime_files(worker_path):
            _add_landlock_rule(
                ruleset_fd,
                path,
                rights,
                required=required,
            )
        if _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            _raise_errno("prctl(PR_SET_NO_NEW_PRIVS)")
        if _LIBC.syscall(restrict_self, ruleset_fd, 0) != 0:
            _raise_errno("landlock_restrict_self")
    finally:
        os.close(ruleset_fd)


def _load_seccomp_library():
    errors: list[str] = []
    for name in ("libseccomp.so.2", "libseccomp.so"):
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError as error:
            errors.append(str(error))
    raise IsolationUnavailable(
        "Unix-socket isolation unavailable: libseccomp could not be loaded: "
        + "; ".join(errors)
    )


def _seccomp_error(action: str, result: int) -> IsolationUnavailable:
    error_number = -result if result < 0 else ctypes.get_errno()
    detail = os.strerror(error_number) if error_number else "unknown error"
    return IsolationUnavailable(
        f"Unix-socket isolation unavailable: {action}: "
        f"[Errno {error_number}] {detail}"
    )


def _install_unix_socket_filter(
    *,
    network: bool,
    host_filesystem: bool,
) -> None:
    """Install the mandatory generated-tool syscall policy.

    Linux keyrings are shared independently of mount, network, PID and IPC
    namespaces.  Their three entry points are therefore denied for every
    capability combination so a worker cannot read an inherited session
    keyring.

    With ``network=false`` every ``socket(2)`` family is denied.  A network
    namespace alone is not a complete no-network boundary for families such
    as VSOCK.  With network access but no host-filesystem access, AF_UNIX and
    AF_VSOCK are denied because both can provide host-side channels outside
    the intended IP network boundary.

    Restricted workers retain only ``socketpair(AF_UNIX, SOCK_STREAM)`` for
    Python/asyncio wakeup pipes.  Datagram pairs can be reconnected to host
    pathname or abstract sockets, so all other domains and base types are
    rejected.  ``io_uring_setup`` is also denied for restricted workers so an
    untrusted tool cannot submit IORING_OP_SOCKET around these rules.
    Descendants inherit the filter across fork and exec.
    """

    library = _load_seccomp_library()
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_ScmpArgCmp),
    ]
    library.seccomp_rule_add_array.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int

    context = library.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise IsolationUnavailable(
            "Unix-socket isolation unavailable: seccomp_init failed"
        )
    try:
        deny_action = _SCMP_ACT_ERRNO | errno.EPERM
        for syscall_name in _KEYRING_SYSCALLS:
            syscall_number = library.seccomp_syscall_resolve_name(
                syscall_name.encode("ascii")
            )
            if syscall_number < 0:
                raise IsolationUnavailable(
                    "Unix-socket isolation unavailable: "
                    f"{syscall_name} syscall is unresolved"
                )
            result = library.seccomp_rule_add(
                context,
                deny_action,
                syscall_number,
                0,
            )
            if result < 0:
                raise _seccomp_error(
                    f"cannot deny {syscall_name}",
                    result,
                )

        # Landlock ABI 1 deliberately does not mediate xattr reads.  Without
        # this syscall layer, a tool that knows a host pathname can recover a
        # user.* attribute even though opening the same file is denied.
        if not host_filesystem:
            for syscall_name in _XATTR_READ_SYSCALLS:
                syscall_number = library.seccomp_syscall_resolve_name(
                    syscall_name.encode("ascii")
                )
                if syscall_number < 0:
                    raise IsolationUnavailable(
                        "Unix-socket isolation unavailable: "
                        f"{syscall_name} syscall is unresolved"
                    )
                result = library.seccomp_rule_add(
                    context,
                    deny_action,
                    syscall_number,
                    0,
                )
                if result < 0:
                    raise _seccomp_error(
                        f"cannot deny {syscall_name}",
                        result,
                    )

        restricted_sockets = not (network and host_filesystem)
        if restricted_sockets:
            socket_syscall = library.seccomp_syscall_resolve_name(b"socket")
            if socket_syscall < 0:
                raise IsolationUnavailable(
                    "Unix-socket isolation unavailable: "
                    "socket syscall is unresolved"
                )
            if not network:
                result = library.seccomp_rule_add(
                    context,
                    deny_action,
                    socket_syscall,
                    0,
                )
                if result < 0:
                    raise _seccomp_error("cannot deny socket", result)
            else:
                for family, family_name in (
                    (_AF_UNIX, "AF_UNIX"),
                    (_AF_VSOCK, "AF_VSOCK"),
                ):
                    comparison = _ScmpArgCmp(
                        arg=0,
                        op=_SCMP_CMP_EQ,
                        datum_a=family,
                        datum_b=0,
                    )
                    result = library.seccomp_rule_add_array(
                        context,
                        deny_action,
                        socket_syscall,
                        1,
                        ctypes.byref(comparison),
                    )
                    if result < 0:
                        raise _seccomp_error(
                            f"cannot deny socket({family_name})",
                            result,
                        )

            socketpair_syscall = library.seccomp_syscall_resolve_name(
                b"socketpair"
            )
            if socketpair_syscall < 0:
                raise IsolationUnavailable(
                    "Unix-socket isolation unavailable: "
                    "socketpair syscall is unresolved"
                )
            non_unix_comparison = _ScmpArgCmp(
                arg=0,
                op=_SCMP_CMP_NE,
                datum_a=_AF_UNIX,
                datum_b=0,
            )
            result = library.seccomp_rule_add_array(
                context,
                deny_action,
                socketpair_syscall,
                1,
                ctypes.byref(non_unix_comparison),
            )
            if result < 0:
                raise _seccomp_error(
                    "cannot restrict socketpair domain",
                    result,
                )
            for socket_type in range(_SOCK_TYPE_MASK + 1):
                if socket_type == _SOCK_STREAM:
                    continue
                comparisons = (_ScmpArgCmp * 2)(
                    _ScmpArgCmp(
                        arg=0,
                        op=_SCMP_CMP_EQ,
                        datum_a=_AF_UNIX,
                        datum_b=0,
                    ),
                    _ScmpArgCmp(
                        arg=1,
                        op=_SCMP_CMP_MASKED_EQ,
                        datum_a=_SOCK_TYPE_MASK,
                        datum_b=socket_type,
                    ),
                )
                result = library.seccomp_rule_add_array(
                    context,
                    deny_action,
                    socketpair_syscall,
                    len(comparisons),
                    comparisons,
                )
                if result < 0:
                    raise _seccomp_error(
                        "cannot restrict socketpair(AF_UNIX) base type "
                        f"{socket_type}",
                        result,
                    )

            io_uring_syscall = library.seccomp_syscall_resolve_name(
                b"io_uring_setup"
            )
            if io_uring_syscall < 0:
                raise IsolationUnavailable(
                    "Unix-socket isolation unavailable: "
                    "io_uring_setup syscall is unresolved"
                )
            result = library.seccomp_rule_add(
                context,
                deny_action,
                io_uring_syscall,
                0,
            )
            if result < 0:
                raise _seccomp_error("cannot deny io_uring_setup", result)

        if _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            _raise_errno("prctl(PR_SET_NO_NEW_PRIVS)")
        result = library.seccomp_load(context)
        if result < 0:
            raise _seccomp_error("seccomp_load failed", result)
    finally:
        library.seccomp_release(context)


def _prepare_mounts(workspace: Path, *, writable: bool) -> None:
    # Make the namespace root and workspace distinct mounts before recursively
    # changing VFS mount attributes.  A fresh procfs belongs to this PID
    # namespace, so host processes are not exposed through the inherited mount.
    _mount("/", "/", None, _MS_BIND | _MS_REC)
    _mount(os.fspath(workspace), os.fspath(workspace), None, _MS_BIND | _MS_REC)
    _mount("proc", "/proc", "proc", _MS_NOSUID | _MS_NODEV | _MS_NOEXEC)
    _mount_set_readonly("/", readonly=True, recursive=True)
    if writable:
        _mount_set_readonly(os.fspath(workspace), readonly=False, recursive=False)

    # The inherited cwd still refers to the pre-bind mount.  Force a new path
    # lookup so relative '.' resolves through the capability-controlled bind.
    os.chdir("/")
    os.chdir(workspace)


def _run_worker(worker_path: Path) -> int:
    sys.argv = [os.fspath(worker_path)]
    try:
        runpy.run_path(os.fspath(worker_path), run_name="__main__")
    except SystemExit as error:
        code = error.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


def main() -> int:
    try:
        if not sys.platform.startswith("linux"):
            raise IsolationUnavailable("Linux isolation is required")
        if len(sys.argv) != 2:
            raise IsolationUnavailable("isolation launcher requires one worker path")
        worker_path = Path(sys.argv[1]).absolute()
        if not worker_path.is_file() or worker_path.is_symlink():
            raise IsolationUnavailable("worker path must be a regular non-symlink file")
        capabilities = _parse_capabilities()
        _require_namespaces()
        _set_isolated_uts_identity()
        _query_landlock_abi()
        workspace = Path.cwd().absolute()
        _prepare_mounts(workspace, writable=capabilities["workspace"])
        if not capabilities["host_filesystem"]:
            _install_landlock(
                workspace,
                worker_path,
                writable=capabilities["workspace"],
                allow_process=capabilities["process"],
            )
        _install_unix_socket_filter(
            network=capabilities["network"],
            host_filesystem=capabilities["host_filesystem"],
        )
        return _run_worker(worker_path)
    except BaseException as error:
        sys.stderr.write(
            f"Generated Tool isolation unavailable: {type(error).__name__}: {error}\n"
        )
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
