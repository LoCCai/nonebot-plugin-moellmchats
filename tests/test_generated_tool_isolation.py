from __future__ import annotations

import ctypes
import importlib
import json
import os
from pathlib import Path

import pytest

from nonebot_plugin_moellmchats.generated_tool_runner import GeneratedToolRunner

isolation = importlib.import_module(
    "nonebot_plugin_moellmchats.generated_tool_isolation"
)


def _capabilities(**overrides: bool) -> dict[str, bool]:
    value = {
        "network": False,
        "process": False,
        "workspace": True,
        "host_filesystem": False,
        "secrets": False,
    }
    value.update(overrides)
    return value


def test_launcher_parses_exactly_five_boolean_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _capabilities(host_filesystem=True)
    monkeypatch.setenv("MOELLM_RUNNER_CAPABILITIES", json.dumps(expected))

    assert isolation._parse_capabilities() == expected
    assert "MOELLM_RUNNER_CAPABILITIES" not in os.environ


@pytest.mark.parametrize(
    "value",
    [
        {},
        {**_capabilities(), "unknown": False},
        {**_capabilities(), "workspace": 1},
    ],
)
def test_launcher_rejects_missing_extra_or_non_boolean_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    value: dict,
) -> None:
    monkeypatch.setenv("MOELLM_RUNNER_CAPABILITIES", json.dumps(value))

    with pytest.raises(isolation.IsolationUnavailable, match="capabilit"):
        isolation._parse_capabilities()


def test_launcher_rejects_parent_mount_and_pid_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MOELLM_RUNNER_PARENT_MOUNT_NS",
        os.readlink("/proc/self/ns/mnt"),
    )
    monkeypatch.setenv(
        "MOELLM_RUNNER_PARENT_PID_NS",
        os.readlink("/proc/self/ns/pid"),
    )
    monkeypatch.setenv(
        "MOELLM_RUNNER_PARENT_IPC_NS",
        os.readlink("/proc/self/ns/ipc"),
    )
    monkeypatch.setenv(
        "MOELLM_RUNNER_PARENT_UTS_NS",
        os.readlink("/proc/self/ns/uts"),
    )

    with pytest.raises(
        isolation.IsolationUnavailable,
        match=r"mount namespace|PID namespace",
    ):
        isolation._require_namespaces()


def test_launcher_rejects_parent_ipc_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace_values = {
        "/proc/self/ns/mnt": "mnt:[200]",
        "/proc/self/ns/pid": "pid:[200]",
        "/proc/self/ns/ipc": "ipc:[100]",
        "/proc/self/ns/uts": "uts:[200]",
    }
    monkeypatch.setenv("MOELLM_RUNNER_PARENT_MOUNT_NS", "mnt:[100]")
    monkeypatch.setenv("MOELLM_RUNNER_PARENT_PID_NS", "pid:[100]")
    monkeypatch.setenv("MOELLM_RUNNER_PARENT_IPC_NS", "ipc:[100]")
    monkeypatch.setenv("MOELLM_RUNNER_PARENT_UTS_NS", "uts:[100]")
    monkeypatch.setattr(
        isolation.os,
        "readlink",
        lambda path: namespace_values[path],
    )
    monkeypatch.setattr(isolation.os, "getpid", lambda: 1)

    with pytest.raises(
        isolation.IsolationUnavailable,
        match="IPC namespace",
    ):
        isolation._require_namespaces()


def test_launcher_rejects_parent_uts_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace_values = {
        "/proc/self/ns/mnt": "mnt:[200]",
        "/proc/self/ns/pid": "pid:[200]",
        "/proc/self/ns/ipc": "ipc:[200]",
        "/proc/self/ns/uts": "uts:[100]",
    }
    monkeypatch.setenv("MOELLM_RUNNER_PARENT_MOUNT_NS", "mnt:[100]")
    monkeypatch.setenv("MOELLM_RUNNER_PARENT_PID_NS", "pid:[100]")
    monkeypatch.setenv("MOELLM_RUNNER_PARENT_IPC_NS", "ipc:[100]")
    monkeypatch.setenv("MOELLM_RUNNER_PARENT_UTS_NS", "uts:[100]")
    monkeypatch.setattr(
        isolation.os,
        "readlink",
        lambda path: namespace_values[path],
    )
    monkeypatch.setattr(isolation.os, "getpid", lambda: 1)

    with pytest.raises(
        isolation.IsolationUnavailable,
        match="UTS namespace",
    ):
        isolation._require_namespaces()


def test_mount_setattr_enosys_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args) -> int:
        ctypes.set_errno(38)
        return -1

    monkeypatch.setattr(isolation._LIBC, "syscall", unavailable)

    with pytest.raises(
        isolation.IsolationUnavailable,
        match=r"mount_setattr.*Errno 38",
    ):
        isolation._mount_set_readonly("/", readonly=True, recursive=True)


def test_landlock_enosys_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args) -> int:
        ctypes.set_errno(38)
        return -1

    monkeypatch.setattr(isolation._LIBC, "syscall", unavailable)

    with pytest.raises(
        isolation.IsolationUnavailable,
        match=r"landlock ABI query.*Errno 38",
    ):
        isolation._query_landlock_abi()


def test_runner_rejects_missing_launcher_before_execution(
    tmp_path: Path,
) -> None:
    runner = GeneratedToolRunner()
    runner._isolation_path = tmp_path / "missing-launcher.py"

    with pytest.raises(RuntimeError, match="isolation launcher"):
        runner._isolation_command(disable_network=False)

    assert runner.isolation_status == "unavailable:no-isolation-launcher"


def test_prepare_mounts_mounts_fresh_proc_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount_calls: list[tuple[str | None, str, str | None, int]] = []
    readonly_calls: list[tuple[str, bool, bool]] = []
    chdir_calls: list[object] = []

    monkeypatch.setattr(
        isolation,
        "_mount",
        lambda source, target, filesystem, flags: mount_calls.append(
            (source, target, filesystem, flags)
        ),
    )
    monkeypatch.setattr(
        isolation,
        "_mount_set_readonly",
        lambda path, *, readonly, recursive: readonly_calls.append(
            (path, readonly, recursive)
        ),
    )
    monkeypatch.setattr(
        isolation.os,
        "chdir",
        lambda path: chdir_calls.append(path),
    )

    workspace = Path("/runner-workspace")
    isolation._prepare_mounts(workspace, writable=True)

    assert [call for call in mount_calls if call[2] == "proc"] == [
        (
            "proc",
            "/proc",
            "proc",
            isolation._MS_NOSUID | isolation._MS_NODEV | isolation._MS_NOEXEC,
        )
    ]
    assert readonly_calls == [
        ("/", True, True),
        (os.fspath(workspace), False, False),
    ]
    assert chdir_calls == ["/", workspace]


def test_runner_environment_uses_fixed_executable_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/sensitive/application/venv/bin")

    environment = GeneratedToolRunner()._environment(
        Path("."),
        capabilities=_capabilities(process=True),
        protocol_fd=9,
    )

    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "/sensitive/application" not in environment["PATH"]
    assert environment["MOELLM_RUNNER_PARENT_UTS_NS"].startswith("uts:[")


def test_unix_socket_filter_fails_closed_without_libseccomp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args, **_kwargs):
        raise OSError("missing")

    monkeypatch.setattr(isolation.ctypes, "CDLL", unavailable)

    with pytest.raises(
        isolation.IsolationUnavailable,
        match=r"Unix-socket isolation unavailable.*libseccomp",
    ):
        isolation._load_seccomp_library()
