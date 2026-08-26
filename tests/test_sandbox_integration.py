from __future__ import annotations

import asyncio
import ctypes
import errno
import importlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest

from nonebot_plugin_moellmchats.generated_tool_runner import GeneratedToolRunner
from nonebot_plugin_moellmchats.tool_artifacts import (
    ToolArtifact,
    ToolContractSnapshot,
    canonical_bundle_digest,
    source_sha256,
)
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolCapability,
    ToolEffect,
    ToolPolicy,
    ToolSpec,
)

_WORKER_PATH = (
    Path(__file__).parents[1]
    / "nonebot_plugin_moellmchats"
    / "generated_tool_worker.py"
)
_KEY_SPEC_SESSION_KEYRING = -3
_KEYCTL_REVOKE = 3
_KEYCTL_READ = 11
_KEYCTL_UNLINK = 9


def _run_probe(command: list[str], *, label: str) -> None:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"sandbox prerequisite failed ({label}) rc={result.returncode}\n"
        f"stdout={result.stdout[-1000:]}\nstderr={result.stderr[-1000:]}"
    )


def _fd_count() -> int:
    """Count live descriptors after /proc's own directory handle is closed."""

    count = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            os.fstat(int(name))
        except OSError:
            continue
        count += 1
    return count


def _keyring_syscall_numbers() -> tuple[int, int, int]:
    machine = os.uname().machine.lower()
    numbers = {
        "x86_64": (248, 249, 250),
        "amd64": (248, 249, 250),
        "aarch64": (217, 218, 219),
        "arm64": (217, 218, 219),
        "riscv64": (217, 218, 219),
    }.get(machine)
    assert numbers is not None, f"unsupported keyring syscall architecture: {machine}"
    return numbers


@pytest.fixture(scope="module", autouse=True)
def _require_real_sandbox_host() -> None:
    """Fail, never skip, when CI cannot exercise the real Linux sandbox."""

    assert sys.platform.startswith("linux"), "sandbox job requires Linux"
    assert os.geteuid() == 0, "sandbox job must run pytest as root"

    unshare = shutil.which("unshare")
    assert unshare is not None, "sandbox job requires util-linux unshare"
    _run_probe(
        [unshare, "--net", "--ipc", "--uts", "--fork", "/bin/true"],
        label="network, IPC and UTS namespaces",
    )

    uid_probe = (
        "import os; "
        "assert os.geteuid() == 0; "
        "os.setgroups([]); os.setgid(65534); os.setuid(65534); "
        "assert os.geteuid() == 65534 and os.getegid() == 65534"
    )
    _run_probe([sys.executable, "-c", uid_probe], label="UID/GID drop")

    seccomp_probe = (
        "import importlib.util; "
        f"p={str(_WORKER_PATH)!r}; "
        "s=importlib.util.spec_from_file_location('sandbox_worker_probe', p); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "m._install_process_filter()"
    )
    _run_probe([sys.executable, "-c", seccomp_probe], label="libseccomp filter")


def _artifact(
    source: bytes,
    *,
    network: bool = True,
    process: bool = False,
    workspace: bool = True,
    host_filesystem: bool = False,
    secrets: bool = False,
    generation: int = 17,
) -> ToolArtifact:
    async def placeholder() -> str:
        return "placeholder"

    capability = ToolCapability(
        network=network,
        process=process,
        workspace=workspace,
        host_filesystem=host_filesystem,
        secrets=secrets,
    )
    policy = ToolPolicy(requested=capability, admin=capability)
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    spec = ToolSpec(
        name="sandbox_probe",
        description="exercise the real sandbox",
        parameters=parameters,
        handler=placeholder,
        policy=policy,
    )
    return ToolArtifact(
        tool_name=spec.name,
        handler_name="sandbox_probe",
        source=source,
        source_hash=source_sha256(source),
        schema={
            "name": spec.name,
            "description": spec.description,
            "parameters": parameters,
        },
        spec=spec,
        contract=ToolContractSnapshot.from_spec(spec),
        source_type="custom_file",
        generation=generation,
        filename="sandbox_probe.py",
    )


def _generated_artifact(source: bytes, *, generation: int = 18) -> ToolArtifact:
    async def placeholder() -> str:
        return "placeholder"

    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    policy = ToolPolicy.generated()
    spec = ToolSpec(
        name="sandbox_probe",
        description="exercise the generated runtime guard",
        parameters=parameters,
        handler=placeholder,
        permission="superuser",
        timeout_seconds=30,
        result_limit=6000,
        policy=policy,
    )
    tests_source = b"async def run_tests(tool_module):\n    return 'ok'\n"
    manifest = {
        "bundle_id": "sandbox_generated_guard",
        "description": "exercise the generated runtime guard",
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": parameters,
                "handler": spec.name,
                "permission": "user",
                "effect": "read_only",
                "timeout_seconds": 30,
                "result_limit": 6000,
                "dependencies": [],
            }
        ],
    }
    bundle_digest = canonical_bundle_digest(manifest, source, tests_source)
    return ToolArtifact(
        tool_name=spec.name,
        handler_name=spec.name,
        source=source,
        source_hash=source_sha256(source),
        schema={
            "name": spec.name,
            "description": spec.description,
            "parameters": parameters,
        },
        spec=spec,
        contract=ToolContractSnapshot.from_spec(
            spec,
            requested_permission="user",
            declared_effect=ToolEffect.READ_ONLY,
        ),
        source_type="generated",
        generation=generation,
        filename="tool.py",
        tests_source=tests_source,
        bundle_manifest=manifest,
        bundle_id="sandbox_generated_guard",
        bundle_digest=bundle_digest,
    )


async def _execute(
    source: bytes,
    *,
    runner: GeneratedToolRunner | None = None,
    network: bool = True,
    process: bool = False,
    workspace: bool = True,
    host_filesystem: bool = False,
    secrets: bool = False,
):
    artifact = _artifact(
        source,
        network=network,
        process=process,
        workspace=workspace,
        host_filesystem=host_filesystem,
        secrets=secrets,
    )
    current_runner = runner or GeneratedToolRunner()
    return await current_runner.execute_artifact(
        artifact,
        {},
        {},
        expected_artifact_digest=artifact.artifact_digest,
        expected_bundle_digest=None,
        generation=artifact.generation,
    )


async def _execute_generated(source: bytes):
    artifact = _generated_artifact(source)
    return await GeneratedToolRunner().execute_artifact(
        artifact,
        {},
        {},
        expected_artifact_digest=artifact.artifact_digest,
        expected_bundle_digest=artifact.bundle_digest,
        generation=artifact.generation,
    )


def _set_runner_config(monkeypatch: pytest.MonkeyPatch, **overrides: int) -> None:
    runner_module = importlib.import_module(
        "nonebot_plugin_moellmchats.generated_tool_runner"
    )
    original_get = runner_module.config_parser.get_config

    def configured(key: str, default=None):
        if key in overrides:
            return overrides[key]
        return original_get(key, default)

    monkeypatch.setattr(runner_module.config_parser, "get_config", configured)


@pytest.mark.asyncio
async def test_sandbox_artifact_fd3_and_uid_drop() -> None:
    result = await _execute(
        b"import ctypes\n"
        b"import os\n"
        b"import stat\n\n"
        b"async def sandbox_probe():\n"
        b"    os.write(1, b'{\\\"protocol_version\\\":999}')\n"
        b"    os.write(2, b'untrusted stderr')\n"
        b"    fd3_is_pipe = stat.S_ISFIFO(os.fstat(3).st_mode)\n"
        b"    no_new_privs = ctypes.CDLL(None).prctl(39, 0, 0, 0, 0)\n"
        b"    return (\n"
        b"        f'uid={os.geteuid()} gid={os.getegid()} pid={os.getpid()} '\n"
        b"        f'fd3_is_pipe={fd3_is_pipe} no_new_privs={no_new_privs}'\n"
        b"    )\n"
    )
    assert result.text == (
        "uid=65534 gid=65534 pid=1 fd3_is_pipe=True no_new_privs=1"
    )


@pytest.mark.asyncio
async def test_sandbox_fd3_preserves_bounded_structured_tool_result() -> None:
    result = await _execute(
        b"async def sandbox_probe():\n"
        b"    return {\n"
        b"        'text': 'weather',\n"
        b"        'images': ['image:one'],\n"
        b"        'files': [{\n"
        b"            'locator': 'result:forecast',\n"
        b"            'name': 'forecast.json',\n"
        b"            'media_type': 'application/json',\n"
        b"        }],\n"
        b"        'structured': {'temperature': 26, 'rain': True},\n"
        b"        'citations': [{\n"
        b"            'title': 'Forecast source',\n"
        b"            'url': 'https://example.com/forecast',\n"
        b"        }],\n"
        b"        'metadata': {'worker': 'fd3'},\n"
        b"    }\n"
    )

    assert result.text == "weather"
    assert result.images == ("image:one",)
    assert result.files[0].locator == "result:forecast"
    assert result.structured == {"rain": True, "temperature": 26}
    assert result.citations[0].url == "https://example.com/forecast"
    assert result.metadata == {"worker": "fd3"}
    assert "[结构化工具结果]" in result.render()
    with pytest.raises(TypeError):
        result.structured["rain"] = False  # type: ignore[index]


@pytest.mark.asyncio
async def test_sandbox_uses_private_fixed_uts_identity() -> None:
    parent_uts_namespace = os.readlink("/proc/self/ns/uts")
    source = (
        "import ctypes\n"
        "import os\n\n"
        "async def sandbox_probe():\n"
        "    libc = ctypes.CDLL(None, use_errno=True)\n"
        "    libc.getdomainname.argtypes = [ctypes.c_void_p, ctypes.c_size_t]\n"
        "    libc.getdomainname.restype = ctypes.c_int\n"
        "    domainname = ctypes.create_string_buffer(256)\n"
        "    assert libc.getdomainname(domainname, len(domainname)) == 0\n"
        f"    isolated = os.readlink('/proc/self/ns/uts') != {parent_uts_namespace!r}\n"
        "    return (\n"
        "        f'isolated={isolated}:hostname={os.uname().nodename}:'\n"
        "        f'domainname={domainname.value.decode()}'\n"
        "    )\n"
    ).encode()

    result = await _execute(source)

    assert result.text == (
        "isolated=True:hostname=moellm-sandbox:domainname=localdomain"
    )


@pytest.mark.asyncio
async def test_sandbox_workspace_is_the_only_writable_mount() -> None:
    token = f"{os.getpid()}-{time.time_ns()}"
    forbidden = [
        Path("/dev/shm") / f"moellm-{token}",
        Path("/var/tmp") / f"moellm-{token}",
    ]
    source = (
        "from pathlib import Path\n\n"
        "async def sandbox_probe(_workspace):\n"
        "    workspace = Path(_workspace)\n"
        "    (workspace / 'allowed').write_text('ok')\n"
        f"    forbidden = {list(map(os.fspath, forbidden))!r}\n"
        "    escaped = []\n"
        "    for item in forbidden:\n"
        "        try:\n"
        "            Path(item).write_text('escape')\n"
        "        except OSError:\n"
        "            escaped.append(False)\n"
        "        else:\n"
        "            escaped.append(True)\n"
        "    return 'workspace=' + (workspace / 'allowed').read_text() + ':' \\\n"
        "        + ','.join(map(str, escaped))\n"
    ).encode()
    try:
        # Bypass Landlock read filtering so this specifically proves the mount
        # tree is read-only outside the writable workspace bind.
        result = await _execute(source, host_filesystem=True)
    finally:
        for path in forbidden:
            path.unlink(missing_ok=True)

    assert result.text == "workspace=ok:False,False"
    assert not any(path.exists() for path in forbidden)


@pytest.mark.asyncio
async def test_sandbox_host_filesystem_false_blocks_sensitive_reads() -> None:
    config_probe = Path("/var/tmp") / (
        f"moellm-plugin-config-{os.getpid()}-{time.time_ns()}.json"
    )
    config_probe.write_text('{"secret":"must-not-leak"}', encoding="utf-8")
    config_probe.chmod(0o644)
    source = (
        "from pathlib import Path\n\n"
        "async def sandbox_probe():\n"
        "    paths = [\n"
        "        '/etc/passwd',\n"
        "        '/proc/self/root/etc/passwd',\n"
        "        '/proc/thread-self/root/etc/passwd',\n"
        f"        {os.fspath(config_probe)!r},\n"
        "    ]\n"
        "    readable = []\n"
        "    for item in paths:\n"
        "        try:\n"
        "            Path(item).read_text()\n"
        "        except OSError:\n"
        "            readable.append(False)\n"
        "        else:\n"
        "            readable.append(True)\n"
        "    return ','.join(map(str, readable))\n"
    ).encode()
    try:
        result = await _execute(source)
    finally:
        config_probe.unlink(missing_ok=True)

    assert result.text == "False,False,False,False"


@pytest.mark.asyncio
async def test_sandbox_host_filesystem_false_blocks_host_xattrs() -> None:
    probe = Path("/var/tmp") / (
        f"moellm-plugin-xattr-{os.getpid()}-{time.time_ns()}"
    )
    attribute = "user.moellm_probe"
    secret = f"host-xattr-secret-{os.getpid()}-{time.time_ns()}".encode()
    try:
        probe.write_bytes(b"public")
        os.setxattr(probe, attribute, secret)
        assert os.getxattr(probe, attribute) == secret
        source = (
            "import errno\n"
            "import os\n\n"
            "async def sandbox_probe():\n"
            "    try:\n"
            f"        os.getxattr({os.fspath(probe)!r}, {attribute!r})\n"
            "    except OSError as error:\n"
            "        return f'xattr_errno={error.errno}:eperm={errno.EPERM}'\n"
            "    return 'xattr-readable'\n"
        ).encode()

        result = await _execute(
            source,
            network=True,
            host_filesystem=False,
        )
    finally:
        try:
            if probe.exists():
                os.removexattr(probe, attribute)
        finally:
            probe.unlink(missing_ok=True)

    assert result.text == f"xattr_errno={errno.EPERM}:eperm={errno.EPERM}"


@pytest.mark.asyncio
async def test_custom_host_filesystem_capability_is_explicit() -> None:
    result = await _execute(
        b"from pathlib import Path\n\n"
        b"async def sandbox_probe():\n"
        b"    return str(Path('/etc/passwd').read_text().startswith('root:'))\n",
        host_filesystem=True,
    )

    assert result.text == "True"


@pytest.mark.asyncio
async def test_secrets_capability_does_not_inject_host_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOELLM_HOST_SECRET_PROBE", "must-not-leak")

    result = await _execute(
        b"import os\n\n"
        b"async def sandbox_probe():\n"
        b"    return str(os.getenv('MOELLM_HOST_SECRET_PROBE') is None)\n",
        secrets=True,
    )

    assert result.text == "True"


@pytest.mark.asyncio
async def test_sandbox_generated_guard_allows_declared_stdlib() -> None:
    result = await _execute_generated(
        b"import json\n"
        b"\n"
        b"async def sandbox_probe():\n"
        b"    return json.dumps({'value': 1.5}, sort_keys=True)\n"
    )
    assert result.text == '{"value": 1.5}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        (
            b"async def sandbox_probe():\n"
            b"    return __builtins__['eval']('40 + 2')\n"
        ),
        (
            b"import importlib\n\n"
            b"async def sandbox_probe():\n"
            b"    return importlib.import_module('pty').spawn(['/bin/true'])\n"
        ),
    ],
)
async def test_sandbox_generated_guard_blocks_dynamic_execution(
    source: bytes,
) -> None:
    with pytest.raises(RuntimeError, match=r"KeyError|generated import is denied"):
        await _execute_generated(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_fd", [1, 2], ids=["stdout", "stderr"])
async def test_sandbox_log_stream_flood_is_bounded_and_cleaned_up(
    monkeypatch: pytest.MonkeyPatch,
    stream_fd: int,
) -> None:
    _set_runner_config(
        monkeypatch,
        generated_tool_output_bytes=256,
        generated_tool_timeout_seconds=3,
    )
    runner = GeneratedToolRunner()
    before_pids = _worker_pids()
    before_fds = _fd_count()
    source = (
        "import os\n\n"
        "async def sandbox_probe():\n"
        f"    os.write({stream_fd}, b'x' * 1024)\n"
        "    return 'unexpected'\n"
    ).encode()
    with pytest.raises(RuntimeError, match="runner 输出超过限制"):
        await _execute(source, runner=runner)
    await _wait_for_worker_cleanup(before_pids)
    assert _fd_count() <= before_fds
    assert runner._semaphore._value == 1


@pytest.mark.asyncio
async def test_sandbox_fd3_protocol_flood_is_bounded_and_cleaned_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(
        monkeypatch,
        generated_tool_output_bytes=256,
        generated_tool_timeout_seconds=3,
    )
    runner = GeneratedToolRunner()
    before_pids = _worker_pids()
    before_fds = _fd_count()
    with pytest.raises(RuntimeError, match="runner 输出超过限制"):
        await _execute(
            b"import os\n\n"
            b"async def sandbox_probe():\n"
            b"    os.write(3, b'x' * 1024)\n"
            b"    return 'unexpected'\n",
            runner=runner,
        )
    await _wait_for_worker_cleanup(before_pids)
    assert _fd_count() <= before_fds
    assert runner._semaphore._value == 1


@pytest.mark.asyncio
async def test_sandbox_applies_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(
        monkeypatch,
        generated_tool_cpu_seconds=3,
        generated_tool_memory_mb=192,
        generated_tool_max_processes=5,
        generated_tool_workspace_mb=7,
    )
    result = await _execute(
        b"import resource\n\n"
        b"async def sandbox_probe():\n"
        b"    names = (\n"
        b"        'RLIMIT_CPU',\n"
        b"        'RLIMIT_AS',\n"
        b"        'RLIMIT_NPROC',\n"
        b"        'RLIMIT_FSIZE',\n"
        b"        'RLIMIT_NOFILE',\n"
        b"    )\n"
        b"    return repr({\n"
        b"        name: resource.getrlimit(getattr(resource, name))\n"
        b"        for name in names\n"
        b"    })\n",
        process=True,
    )
    assert result.text == repr(
        {
            "RLIMIT_CPU": (3, 4),
            "RLIMIT_AS": (192 * 1024 * 1024, 192 * 1024 * 1024),
            "RLIMIT_NPROC": (5, 5),
            "RLIMIT_FSIZE": (7 * 1024 * 1024, 7 * 1024 * 1024),
            "RLIMIT_NOFILE": (64, 64),
        }
    )


@pytest.mark.asyncio
async def test_sandbox_enforces_memory_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(
        monkeypatch,
        generated_tool_memory_mb=128,
        generated_tool_timeout_seconds=5,
    )
    with pytest.raises(RuntimeError, match="MemoryError"):
        await _execute(
            b"async def sandbox_probe():\n"
            b"    blocks = []\n"
            b"    while True:\n"
            b"        blocks.append(b'x' * 16_000_000)\n"
        )


@pytest.mark.asyncio
async def test_sandbox_enforces_cpu_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(
        monkeypatch,
        generated_tool_cpu_seconds=1,
        generated_tool_timeout_seconds=5,
    )
    started = time.monotonic()
    with pytest.raises(RuntimeError) as error:
        await _execute(
            b"async def sandbox_probe():\n"
            b"    while True:\n"
            b"        pass\n"
        )
    assert "超时" not in str(error.value)
    assert time.monotonic() - started < 4


@pytest.mark.asyncio
async def test_sandbox_enforces_file_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(
        monkeypatch,
        generated_tool_workspace_mb=1,
        generated_tool_workspace_max_file_bytes=4 * 1024 * 1024,
    )
    with pytest.raises(RuntimeError, match=r"File too large|Errno 27"):
        await _execute(
            b"from pathlib import Path\n\n"
            b"async def sandbox_probe(_workspace):\n"
            b"    (Path(_workspace) / 'large').write_bytes(b'x' * 2_000_000)\n"
            b"    return 'unexpected'\n"
        )


@pytest.mark.asyncio
async def test_sandbox_enforces_open_file_limit() -> None:
    result = await _execute(
        b"import errno\n"
        b"import os\n\n"
        b"async def sandbox_probe():\n"
        b"    opened = []\n"
        b"    failure = None\n"
        b"    try:\n"
        b"        while True:\n"
        b"            opened.append(os.open('/dev/null', os.O_RDONLY))\n"
        b"    except OSError as error:\n"
        b"        failure = error.errno\n"
        b"    finally:\n"
        b"        for fd in opened:\n"
        b"            os.close(fd)\n"
        b"    return f'opened={len(opened)}:errno={failure}:emfile={errno.EMFILE}'\n"
    )
    prefix, errno_value, emfile_value = result.text.split(":")
    opened = int(prefix.removeprefix("opened="))
    failure_errno = int(errno_value.removeprefix("errno="))
    expected_errno = int(emfile_value.removeprefix("emfile="))
    assert 1 <= opened < 64
    assert failure_errno == expected_errno


@pytest.mark.asyncio
async def test_sandbox_enforces_process_count_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(monkeypatch, generated_tool_max_processes=1)
    with pytest.raises(
        RuntimeError,
        match=r"BlockingIOError|Resource temporarily unavailable|Errno 11",
    ):
        await _execute(
            b"import subprocess\n\n"
            b"async def sandbox_probe():\n"
            b"    subprocess.run(['/bin/true'], check=True)\n"
            b"    return 'unexpected'\n",
            process=True,
        )


@pytest.mark.asyncio
async def test_sandbox_process_true_executes_fixed_system_binary_only() -> None:
    result = await _execute(
        b"from pathlib import Path\n"
        b"import subprocess\n\n"
        b"async def sandbox_probe():\n"
        b"    subprocess.run(['/bin/true'], check=True)\n"
        b"    try:\n"
        b"        Path('/etc/passwd').read_text()\n"
        b"    except OSError:\n"
        b"        host_read = False\n"
        b"    else:\n"
        b"        host_read = True\n"
        b"    return f'exec=True:host_read={host_read}'\n",
        process=True,
        host_filesystem=False,
    )

    assert result.text == "exec=True:host_read=False"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("network", "host_filesystem", "expected"),
    [
        pytest.param(True, False, "blocked", id="host-filesystem-false"),
        pytest.param(False, True, "blocked", id="network-false"),
        pytest.param(True, True, "connected", id="both-explicit"),
    ],
)
async def test_sandbox_parent_unix_socket_requires_both_capabilities(
    network: bool,
    host_filesystem: bool,
    expected: str,
) -> None:
    socket_path = Path("/var/tmp") / (
        f"moellm-parent-{os.getpid()}-{time.time_ns()}.sock"
    )
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(os.fspath(socket_path))
        socket_path.chmod(0o777)
        server.listen(1)
        source = (
            "import errno\n"
            "import socket\n\n"
            "async def sandbox_probe():\n"
            "    try:\n"
            "        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "    except OSError as error:\n"
            "        return f'socket_errno={error.errno}:eperm={errno.EPERM}'\n"
            "    try:\n"
            f"        client.connect({os.fspath(socket_path)!r})\n"
            "    except OSError as error:\n"
            "        return f'connect_errno={error.errno}:eperm={errno.EPERM}'\n"
            "    finally:\n"
            "        client.close()\n"
            "    return 'connected'\n"
        ).encode()

        result = await _execute(
            source,
            network=network,
            host_filesystem=host_filesystem,
        )
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)

    if expected == "connected":
        assert result.text == "connected"
    else:
        assert result.text == f"socket_errno={errno.EPERM}:eperm={errno.EPERM}"


@pytest.mark.asyncio
async def test_sandbox_host_filesystem_false_blocks_af_vsock() -> None:
    result = await _execute(
        b"import errno\n"
        b"import socket\n\n"
        b"async def sandbox_probe():\n"
        b"    try:\n"
        b"        client = socket.socket(40, socket.SOCK_STREAM)\n"
        b"    except OSError as error:\n"
        b"        return f'socket_errno={error.errno}:eperm={errno.EPERM}'\n"
        b"    client.close()\n"
        b"    return 'socket-created'\n",
        network=True,
        host_filesystem=False,
    )

    assert result.text == f"socket_errno={errno.EPERM}:eperm={errno.EPERM}"


@pytest.mark.asyncio
async def test_sandbox_blocks_non_unix_socketpair() -> None:
    result = await _execute(
        b"import errno\n"
        b"import socket\n\n"
        b"async def sandbox_probe():\n"
        b"    try:\n"
        b"        left, right = socket.socketpair(30, socket.SOCK_STREAM)\n"
        b"    except OSError as error:\n"
        b"        return f'socketpair_errno={error.errno}:eperm={errno.EPERM}'\n"
        b"    left.close()\n"
        b"    right.close()\n"
        b"    return 'socketpair-created'\n",
        network=False,
        host_filesystem=False,
    )

    assert result.text == (
        f"socketpair_errno={errno.EPERM}:eperm={errno.EPERM}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("address_kind", ["filesystem", "abstract"])
async def test_sandbox_blocks_unix_datagram_socketpair_reconnect_bypass(
    address_kind: str,
) -> None:
    token = f"moellm-parent-dgram-{os.getpid()}-{time.time_ns()}"
    socket_path = Path("/var/tmp") / f"{token}.sock"
    address = os.fspath(socket_path) if address_kind == "filesystem" else f"\0{token}"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        server.bind(address)
        if address_kind == "filesystem":
            socket_path.chmod(0o777)
        server.settimeout(0.1)
        source = (
            "import errno\n"
            "import socket\n\n"
            "async def sandbox_probe():\n"
            "    try:\n"
            "        left, right = socket.socketpair(\n"
            "            socket.AF_UNIX, socket.SOCK_DGRAM\n"
            "        )\n"
            "    except OSError as error:\n"
            "        return f'socketpair_errno={error.errno}:eperm={errno.EPERM}'\n"
            "    try:\n"
            f"        left.connect({address!r})\n"
            "        left.send(b'probe')\n"
            "    except OSError as error:\n"
            "        return f'reconnect_errno={error.errno}:eperm={errno.EPERM}'\n"
            "    finally:\n"
            "        left.close()\n"
            "        right.close()\n"
            "    return 'connected'\n"
        ).encode()

        result = await _execute(
            source,
            network=True,
            process=True,
            host_filesystem=False,
        )
        with pytest.raises(TimeoutError):
            server.recv(64)
    finally:
        server.close()
        if address_kind == "filesystem":
            socket_path.unlink(missing_ok=True)

    assert result.text == (
        f"socketpair_errno={errno.EPERM}:eperm={errno.EPERM}"
    )


@pytest.mark.asyncio
async def test_sandbox_network_namespace_blocks_parent_loopback() -> None:
    async def accepted(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(accepted, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    parent_namespace = os.readlink("/proc/self/ns/net")
    source = (
        "import errno\n"
        "import os\n"
        "import socket\n\n"
        "async def sandbox_probe():\n"
        f"    isolated = os.readlink('/proc/self/ns/net') != {parent_namespace!r}\n"
        "    try:\n"
        "        sock = socket.socket()\n"
        "    except OSError as error:\n"
        "        return (\n"
        "            f'isolated={isolated}:socket_errno={error.errno}:'\n"
        "            f'eperm={errno.EPERM}'\n"
        "        )\n"
        "    sock.settimeout(0.5)\n"
        "    try:\n"
        f"        sock.connect(('127.0.0.1', {port}))\n"
        "    except OSError:\n"
        "        return f'isolated={isolated}:blocked'\n"
        "    finally:\n"
        "        sock.close()\n"
        "    return f'isolated={isolated}:connected'\n"
    ).encode()
    try:
        result = await _execute(source, network=False)
    finally:
        server.close()
        await server.wait_closed()
    assert result.text == (
        f"isolated=True:socket_errno={errno.EPERM}:eperm={errno.EPERM}"
    )


@pytest.mark.asyncio
async def test_sandbox_ipc_namespace_blocks_parent_sysv_shared_memory() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
    libc.shmget.restype = ctypes.c_int
    libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    libc.shmat.restype = ctypes.c_void_p
    libc.shmdt.argtypes = [ctypes.c_void_p]
    libc.shmdt.restype = ctypes.c_int
    libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    libc.shmctl.restype = ctypes.c_int

    secret = f"host-secret-{os.getpid()}-{time.time_ns()}".encode()
    shmid = libc.shmget(0, len(secret) + 1, 0o1000 | 0o666)
    assert shmid >= 0, os.strerror(ctypes.get_errno())
    failed_pointer = ctypes.c_void_p(-1).value
    try:
        address = libc.shmat(shmid, None, 0)
        assert address != failed_pointer, os.strerror(ctypes.get_errno())
        try:
            ctypes.memmove(address, secret + b"\0", len(secret) + 1)
        finally:
            assert libc.shmdt(address) == 0

        source = (
            "import ctypes\n"
            "import os\n\n"
            "async def sandbox_probe():\n"
            "    libc = ctypes.CDLL(None, use_errno=True)\n"
            "    libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]\n"
            "    libc.shmat.restype = ctypes.c_void_p\n"
            f"    address = libc.shmat({shmid}, None, 0)\n"
            "    failed = ctypes.c_void_p(-1).value\n"
            "    if address == failed:\n"
            "        return f'blocked=True:errno={ctypes.get_errno()}'\n"
            f"    value = ctypes.string_at(address, {len(secret)}).decode()\n"
            "    libc.shmdt(address)\n"
            "    return 'blocked=False:value=' + value\n"
        ).encode()

        result = await _execute(
            source,
            network=False,
            process=False,
            host_filesystem=False,
        )
    finally:
        assert libc.shmctl(shmid, 0, None) == 0

    assert result.text.startswith("blocked=True:errno=")


@pytest.mark.asyncio
async def test_sandbox_blocks_inherited_session_keyring_for_all_capabilities() -> None:
    add_key_syscall, _, keyctl_syscall = _keyring_syscall_numbers()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    description = f"moellm-{os.getpid()}-{time.time_ns()}".encode()
    payload = f"host-keyring-secret-{os.getpid()}-{time.time_ns()}".encode()
    ctypes.set_errno(0)
    key_id = libc.syscall(
        ctypes.c_long(add_key_syscall),
        ctypes.c_char_p(b"user"),
        ctypes.c_char_p(description),
        ctypes.c_char_p(payload),
        ctypes.c_size_t(len(payload)),
        ctypes.c_long(_KEY_SPEC_SESSION_KEYRING),
    )
    assert key_id >= 0, os.strerror(ctypes.get_errno())

    revoke_result = -1
    revoke_errno = 0
    unlink_result = -1
    unlink_errno = 0
    try:
        source = (
            "import ctypes\n"
            "import errno\n\n"
            "async def sandbox_probe():\n"
            "    libc = ctypes.CDLL(None, use_errno=True)\n"
            "    libc.syscall.restype = ctypes.c_long\n"
            "    buffer = ctypes.create_string_buffer(4096)\n"
            "    ctypes.set_errno(0)\n"
            "    result = libc.syscall(\n"
            f"        ctypes.c_long({keyctl_syscall}),\n"
            f"        ctypes.c_long({_KEYCTL_READ}),\n"
            f"        ctypes.c_long({key_id}),\n"
            "        ctypes.byref(buffer),\n"
            "        ctypes.c_size_t(len(buffer)),\n"
            "    )\n"
            "    return (\n"
            "        f'result={result}:errno={ctypes.get_errno()}:'\n"
            "        f'eperm={errno.EPERM}'\n"
            "    )\n"
        ).encode()
        for network, host_filesystem in (
            (False, False),
            (False, True),
            (True, False),
            (True, True),
        ):
            result = await _execute(
                source,
                network=network,
                host_filesystem=host_filesystem,
            )
            assert result.text == f"result=-1:errno={errno.EPERM}:eperm={errno.EPERM}", (
                network,
                host_filesystem,
                result.text,
            )
    finally:
        ctypes.set_errno(0)
        revoke_result = libc.syscall(
            ctypes.c_long(keyctl_syscall),
            ctypes.c_long(_KEYCTL_REVOKE),
            ctypes.c_long(key_id),
            ctypes.c_long(0),
            ctypes.c_long(0),
            ctypes.c_long(0),
        )
        revoke_errno = ctypes.get_errno()
        ctypes.set_errno(0)
        unlink_result = libc.syscall(
            ctypes.c_long(keyctl_syscall),
            ctypes.c_long(_KEYCTL_UNLINK),
            ctypes.c_long(key_id),
            ctypes.c_long(_KEY_SPEC_SESSION_KEYRING),
            ctypes.c_long(0),
            ctypes.c_long(0),
        )
        unlink_errno = ctypes.get_errno()

    assert revoke_result == 0, os.strerror(revoke_errno)
    assert unlink_result == 0, os.strerror(unlink_errno)


@pytest.mark.asyncio
async def test_sandbox_process_false_denies_spawn_and_exec() -> None:
    result = await _execute(
        b"import os\n"
        b"import subprocess\n\n"
        b"async def sandbox_probe():\n"
        b"    try:\n"
        b"        subprocess.run(['/bin/true'], check=True)\n"
        b"        spawn_errno = 0\n"
        b"    except OSError as error:\n"
        b"        spawn_errno = error.errno\n"
        b"    try:\n"
        b"        os.execv('/bin/true', ['true'])\n"
        b"        exec_errno = 0\n"
        b"    except OSError as error:\n"
        b"        exec_errno = error.errno\n"
        b"    return f'spawn={spawn_errno}:exec={exec_errno}'\n",
        process=False,
    )
    assert result.text == "spawn=1:exec=1"


@pytest.mark.asyncio
async def test_sandbox_timeout_kills_worker_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(monkeypatch, generated_tool_timeout_seconds=1)
    runner = GeneratedToolRunner()
    before_pids = _worker_pids()
    before_fds = _fd_count()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="超时"):
        await _execute(
            b"async def sandbox_probe():\n"
            b"    while True:\n"
            b"        pass\n",
            runner=runner,
            network=False,
        )
    assert time.monotonic() - started < 5
    await _wait_for_worker_cleanup(before_pids)
    assert _fd_count() <= before_fds
    assert runner._semaphore._value == 1


def _worker_pids() -> set[int]:
    result: set[int] = set()
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            command = (item / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b"generated_tool_worker.py" in command:
            result.add(int(item.name))
    return result


async def _wait_for_worker_cleanup(before: set[int], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while _worker_pids() - before:
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.02)
    assert _worker_pids() <= before


@pytest.mark.asyncio
async def test_sandbox_cancellation_kills_worker_and_closes_fds() -> None:
    before = _worker_pids()
    before_fds = _fd_count()
    runner = GeneratedToolRunner()
    task = asyncio.create_task(
        _execute(
            b"import asyncio\n\n"
            b"async def sandbox_probe():\n"
            b"    await asyncio.sleep(60)\n",
            runner=runner,
            network=False,
        )
    )
    await asyncio.sleep(0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for_worker_cleanup(before)
    assert _fd_count() <= before_fds
    assert runner._semaphore._value == 1


@pytest.mark.asyncio
async def test_sandbox_pid_namespace_cleans_detached_descendant_before_final_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = GeneratedToolRunner()
    original_scan = runner._scan_workspace
    final_scan_observed = False
    token = f"mlm{os.getpid():x}{time.time_ns():x}"[-15:]

    def matching_process_states() -> list[str]:
        states = []
        for item in Path("/proc").iterdir():
            if not item.name.isdigit():
                continue
            try:
                if (item / "comm").read_text().strip() != token:
                    continue
                states.append((item / "stat").read_text().split()[2])
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        return states

    def asserting_scan(path: Path, limits) -> tuple[int, int]:
        nonlocal final_scan_observed
        marker = path / "descendant.token"
        if marker.exists():
            assert marker.read_text() == token
            assert matching_process_states() == []
            final_scan_observed = True
        return original_scan(path, limits)

    async def delayed_watcher(*_args, **_kwargs) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(runner, "_scan_workspace", asserting_scan)
    monkeypatch.setattr(runner, "_watch_workspace", delayed_watcher)
    result = await _execute(
        (
            "import ctypes\n"
            "import os\n"
            "from pathlib import Path\n"
            "import time\n\n"
            "async def sandbox_probe(_workspace):\n"
            "    ready_read, ready_write = os.pipe()\n"
            "    child = os.fork()\n"
            "    if child == 0:\n"
            "        os.close(ready_read)\n"
            "        os.setsid()\n"
            f"        token = {token!r}.encode()\n"
            "        result = ctypes.CDLL(None).prctl(\n"
            "            15, ctypes.c_char_p(token), 0, 0, 0\n"
            "        )\n"
            "        if result != 0:\n"
            "            os._exit(91)\n"
            "        os.write(ready_write, b'1')\n"
            "        os.close(ready_write)\n"
            "        time.sleep(60)\n"
            "        os._exit(0)\n"
            "    os.close(ready_write)\n"
            "    if os.read(ready_read, 1) != b'1':\n"
            "        raise RuntimeError('detached child setup failed')\n"
            "    os.close(ready_read)\n"
            f"    token = {token!r}\n"
            "    Path(_workspace, 'descendant.token').write_text(token)\n"
            "    return token\n"
        ).encode(),
        runner=runner,
        process=True,
    )

    assert result.text == token
    assert final_scan_observed is True
    assert matching_process_states() == []


@pytest.mark.asyncio
async def test_sandbox_workspace_false_is_read_only_and_not_injected() -> None:
    result = await _execute(
        b"from pathlib import Path\n\n"
        b"async def sandbox_probe(_workspace=None):\n"
        b"    try:\n"
        b"        Path('write-probe').write_text('unexpected')\n"
        b"        writable = True\n"
        b"    except OSError:\n"
        b"        writable = False\n"
        b"    return f'injected={_workspace is not None}:writable={writable}'\n",
        workspace=False,
    )
    assert result.text == "injected=False:writable=False"


@pytest.mark.asyncio
async def test_sandbox_workspace_total_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(
        monkeypatch,
        generated_tool_workspace_mb=1,
        generated_tool_workspace_max_file_bytes=800_000,
    )
    with pytest.raises(RuntimeError, match="总容量"):
        await _execute(
            b"from pathlib import Path\n\n"
            b"async def sandbox_probe(_workspace):\n"
            b"    root = Path(_workspace)\n"
            b"    (root / 'one').write_bytes(b'x' * 700000)\n"
            b"    (root / 'two').write_bytes(b'x' * 700000)\n"
            b"    return 'unexpected'\n"
        )


@pytest.mark.asyncio
async def test_sandbox_workspace_single_file_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(
        monkeypatch,
        generated_tool_workspace_max_file_bytes=32,
    )
    with pytest.raises(RuntimeError, match="单文件"):
        await _execute(
            b"from pathlib import Path\n\n"
            b"async def sandbox_probe(_workspace):\n"
            b"    (Path(_workspace) / 'large').write_bytes(b'x' * 33)\n"
            b"    return 'unexpected'\n"
        )


@pytest.mark.asyncio
async def test_sandbox_workspace_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(monkeypatch, generated_tool_workspace_max_depth=1)
    with pytest.raises(RuntimeError, match="层级"):
        await _execute(
            b"from pathlib import Path\n\n"
            b"async def sandbox_probe(_workspace):\n"
            b"    (Path(_workspace) / 'one' / 'two').mkdir(parents=True)\n"
            b"    return 'unexpected'\n"
        )


@pytest.mark.asyncio
async def test_sandbox_workspace_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(monkeypatch, generated_tool_workspace_max_files=10)
    with pytest.raises(RuntimeError, match="符号链接"):
        await _execute(
            b"import os\n"
            b"from pathlib import Path\n\n"
            b"async def sandbox_probe(_workspace):\n"
            b"    root = Path(_workspace)\n"
            b"    (root / 'target').write_text('x')\n"
            b"    os.symlink('target', root / 'link')\n"
            b"    return 'unexpected'\n"
        )


@pytest.mark.asyncio
async def test_sandbox_workspace_file_count_uses_final_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_runner_config(monkeypatch, generated_tool_workspace_max_files=2)
    runner = GeneratedToolRunner()

    async def delayed_watcher(*_args, **_kwargs) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(runner, "_watch_workspace", delayed_watcher)
    with pytest.raises(RuntimeError, match="条目数量"):
        await _execute(
            b"from pathlib import Path\n\n"
            b"async def sandbox_probe(_workspace):\n"
            b"    root = Path(_workspace)\n"
            b"    for index in range(3):\n"
            b"        (root / str(index)).write_text('x')\n"
            b"    return 'unexpected'\n",
            runner=runner,
        )
