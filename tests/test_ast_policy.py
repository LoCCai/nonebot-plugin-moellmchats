from __future__ import annotations

import ast

import pytest

from nonebot_plugin_moellmchats import ast_policy as ast_policy_module
from nonebot_plugin_moellmchats.ast_policy import (
    PolicyDecision,
    analyze_ast_policy,
)
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolCapability,
    ToolEffect,
    ToolPolicy,
)


def _policy(*, network: bool = False, process: bool = False) -> ToolPolicy:
    capability = ToolCapability(network=network, process=process)
    return ToolPolicy(requested=capability, admin=capability)


def _analyze(
    source: str,
    *,
    source_type: str = "generated",
    network: bool = False,
    process: bool = False,
):
    return analyze_ast_policy(
        ast.parse(source),
        source_type=source_type,
        policy=_policy(network=network, process=process),
    )


def test_safe_generated_function_is_allowed() -> None:
    report = _analyze("async def add(a: int, b: int = 1):\n    return a + b\n")

    assert report.allowed is True
    assert report.decision is PolicyDecision.ALLOW
    assert report.detected_effect is ToolEffect.READ_ONLY


def test_generated_dynamic_features_and_process_calls_are_denied() -> None:
    report = _analyze(
        "import os\n"
        "from subprocess import run as launch\n"
        "@decorator\n"
        "async def unsafe(value=make_default()):\n"
        "    getattr(os, 'execv')('/bin/true', ['true'])\n"
        "    launch(['/bin/true'])\n"
    )

    codes = {item.code for item in report.findings}
    assert report.allowed is False
    assert report.decision is PolicyDecision.DENY
    assert {
        "process.import",
        "process.call",
        "syntax.decorator",
        "syntax.dynamic_default",
        "call.dynamic",
    } <= codes


def test_missing_network_capability_is_structured_blocker() -> None:
    source = (
        "async def probe():\n"
        "    response = await safe_request('https://api.example/data')\n"
        "    return response.text\n"
    )
    denied = _analyze(source)
    allowed = _analyze(
        source,
        source_type="custom_file",
        network=True,
    )

    assert denied.decision is PolicyDecision.CAPABILITY_REQUIRED
    assert denied.allowed is False
    assert allowed.decision is PolicyDecision.RISK
    assert allowed.allowed is True
    assert {item.capability for item in denied.findings} == {"network"}
    assert denied.for_handler("probe").detected_capabilities == ToolCapability(
        network=True,
        workspace=False,
    )
    assert allowed.for_handler("probe").detected_capabilities == (
        denied.for_handler("probe").detected_capabilities
    )


def test_detected_mutation_overrides_read_only_declaration() -> None:
    report = _analyze("async def save(path, value):\n    with open(path, 'w') as file:\n        file.write(value)\n")

    assert report.detected_effect is ToolEffect.MUTATING
    assert report.effective_effect(ToolEffect.READ_ONLY) is ToolEffect.MUTATING
    assert report.effective_effect(ToolEffect.MUTATING) is ToolEffect.MUTATING


def test_custom_process_import_requires_explicit_capability() -> None:
    denied = _analyze(
        "import subprocess\nasync def run():\n    return subprocess.run(['true'])\n",
        source_type="custom_file",
    )
    allowed = _analyze(
        "import subprocess\nasync def run():\n    return subprocess.run(['true'])\n",
        source_type="custom_file",
        process=True,
    )

    assert denied.decision is PolicyDecision.CAPABILITY_REQUIRED
    assert denied.allowed is False
    assert allowed.decision is PolicyDecision.RISK
    assert allowed.allowed is True
    assert allowed.for_handler("run").detected_effect is ToolEffect.MUTATING
    assert (
        allowed.for_handler("run").effective_effect(ToolEffect.READ_ONLY)
        is ToolEffect.MUTATING
    )


def test_handler_reports_propagate_only_reachable_helper_effects() -> None:
    report = _analyze(
        "from pathlib import Path\n"
        "def _leaf(path):\n"
        "    Path(path).write_text('changed')\n"
        "def _middle(path):\n"
        "    return _leaf(path)\n"
        "async def mutate(path):\n"
        "    return _middle(path)\n"
        "async def inspect(value):\n"
        "    return value\n"
    )

    assert report.module_effect is ToolEffect.READ_ONLY
    assert report.for_handler("mutate").detected_effect is ToolEffect.MUTATING
    assert report.for_handler("inspect").detected_effect is ToolEffect.READ_ONLY
    assert report.for_handler("mutate").reachable_functions == (
        "_leaf",
        "_middle",
        "mutate",
    )
    assert report.call_graph["mutate"] == ("_middle",)
    assert report.for_handler("inspect").reachable_functions == ("inspect",)


def test_custom_capability_is_evaluated_per_reachable_handler() -> None:
    source = (
        "async def _network_helper():\n"
        "    return await safe_request('https://api.example/data')\n"
        "async def fetch():\n"
        "    return await _network_helper()\n"
        "async def local():\n"
        "    return 1\n"
        "def _unused_network():\n"
        "    import requests\n"
        "    return requests.get('https://example.invalid')\n"
    )
    report = analyze_ast_policy(
        ast.parse(source),
        source_type="custom_file",
        policy=_policy(),
        handler_policies={
            "fetch": _policy(network=True),
            "local": _policy(),
        },
    )

    assert report.for_handler("fetch").allowed is True
    assert report.for_handler("fetch").decision is PolicyDecision.RISK
    assert report.for_handler("local").allowed is True
    assert all(item.capability != "network" for item in report.for_handler("local").findings)
    assert "_unused_network" not in report.for_handler("fetch").reachable_functions

    denied = analyze_ast_policy(
        ast.parse(source),
        source_type="custom_file",
        policy=_policy(),
        handler_names=("fetch", "local"),
    )
    assert denied.for_handler("fetch").decision is PolicyDecision.CAPABILITY_REQUIRED
    assert denied.for_handler("local").allowed is True


def test_loading_time_rules_are_module_findings_for_every_handler() -> None:
    report = _analyze(
        "def decorate(function):\n"
        "    return function\n"
        "def make_default():\n"
        "    return object()\n"
        "@decorate\n"
        "def _helper(value=make_default()):\n"
        "    return value\n"
        "def _also_dynamic(value=set()):\n"
        "    return value\n"
        "class LoadedAtImport:\n"
        "    pass\n"
        "async def safe():\n"
        "    return 1\n"
    )

    module_codes = {item.code for item in report.module_findings}
    handler_codes = {item.code for item in report.for_handler("safe").findings}
    assert {"syntax.class", "syntax.decorator", "syntax.dynamic_default"} <= module_codes
    assert module_codes <= handler_codes
    assert report.for_handler("safe").decision is PolicyDecision.DENY


def test_generated_process_aliases_asyncio_and_attribute_assignment_are_denied() -> None:
    report = _analyze(
        "import asyncio\n"
        "import os\n"
        "async def dangerous(holder):\n"
        "    launch = asyncio.create_subprocess_exec\n"
        "    holder.launch = os.execv\n"
        "    os.execv = launch\n"
        "    return await launch('/bin/true')\n"
    )

    codes = {item.code for item in report.for_handler("dangerous").findings}
    assert report.for_handler("dangerous").decision is PolicyDecision.DENY
    assert {
        "process.alias",
        "process.attribute_assignment",
        "process.call",
        "process.reference",
    } <= codes


def test_generated_module_dict_process_lookups_and_aliases_are_denied() -> None:
    report = _analyze(
        "import os as operating_system\n"
        "async def direct():\n"
        "    return operating_system.__dict__['system']('id')\n"
        "async def aliased():\n"
        "    namespace = operating_system.__dict__\n"
        "    launch = namespace['execv']\n"
        "    return launch('/bin/true', ['true'])\n"
        "async def dictionary_get():\n"
        "    return operating_system.__dict__.get('system')('id')\n"
        "async def vars_alias():\n"
        "    namespace = vars(operating_system)\n"
        "    return namespace['system']('id')\n"
    )

    for name in ("direct", "aliased", "dictionary_get", "vars_alias"):
        handler = report.for_handler(name)
        assert handler.allowed is False
        assert handler.decision is PolicyDecision.DENY
        assert any(item.capability == "process" for item in handler.blocking_findings)
    assert "process.dynamic_namespace" in {item.code for item in report.for_handler("aliased").findings}


def test_dotted_import_binding_does_not_hide_process_calls() -> None:
    report = _analyze(
        "import asyncio.subprocess\n"
        "import os.path\n"
        "async def os_launch():\n"
        "    return os.system('id')\n"
        "async def asyncio_launch():\n"
        "    return await asyncio.create_subprocess_shell('true')\n"
    )

    for name in ("os_launch", "asyncio_launch"):
        handler = report.for_handler(name)
        assert handler.allowed is False
        assert handler.decision is PolicyDecision.DENY
        assert "process.call" in {item.code for item in handler.findings}


def test_dynamic_process_attribute_lookups_are_denied() -> None:
    report = _analyze(
        "import os\n"
        "async def builtin_dynamic(attribute):\n"
        "    return getattr(os, attribute)\n"
        "async def dunder_dynamic(attribute):\n"
        "    return os.__getattribute__(attribute)\n"
        "async def descriptor_literal():\n"
        "    return object.__getattribute__(os, 'system')\n"
        "async def loop_literal(loop):\n"
        "    return loop.__getattribute__('subprocess_exec')\n"
    )

    for name in (
        "builtin_dynamic",
        "dunder_dynamic",
        "descriptor_literal",
        "loop_literal",
    ):
        handler = report.for_handler(name)
        assert handler.allowed is False
        assert handler.decision is PolicyDecision.DENY
        assert any(item.capability == "process" for item in handler.blocking_findings)
    assert "process.dynamic_namespace" in {item.code for item in report.for_handler("dunder_dynamic").findings}


def test_custom_dynamic_process_namespace_requires_capability() -> None:
    source = "import os\nasync def lookup(attribute):\n    return os.__getattribute__(attribute)\n"
    denied = _analyze(source, source_type="custom_file")
    allowed = _analyze(source, source_type="custom_file", process=True)

    assert denied.for_handler("lookup").decision is PolicyDecision.CAPABILITY_REQUIRED
    assert allowed.for_handler("lookup").allowed is True
    assert allowed.for_handler("lookup").decision is PolicyDecision.RISK


def test_generated_asyncio_loop_subprocess_variants_are_denied() -> None:
    report = _analyze(
        "import asyncio as aio\n"
        "async def loop_exec(protocol_factory):\n"
        "    loop = aio.get_running_loop()\n"
        "    return await loop.subprocess_exec(protocol_factory, '/bin/true')\n"
        "async def chained_shell():\n"
        "    return await aio.get_event_loop().subprocess_shell('true')\n"
        "async def dictionary_alias():\n"
        "    spawn = aio.__dict__['create_subprocess_exec']\n"
        "    return await spawn('/bin/true')\n"
    )

    for name in ("loop_exec", "chained_shell", "dictionary_alias"):
        handler = report.for_handler(name)
        assert handler.allowed is False
        assert handler.decision is PolicyDecision.DENY
        assert "process.call" in {item.code for item in handler.findings}


def test_custom_dynamic_os_exec_assignment_requires_process_capability() -> None:
    source = "import os\nasync def replace(function):\n    setattr(os, 'execv', function)\n"
    denied = _analyze(source, source_type="custom_file")
    allowed = _analyze(source, source_type="custom_file", process=True)

    assert denied.for_handler("replace").decision is PolicyDecision.CAPABILITY_REQUIRED
    assert allowed.for_handler("replace").allowed is True
    assert allowed.for_handler("replace").detected_effect is ToolEffect.MUTATING


def test_http_request_method_effect_distinguishes_reads_and_writes() -> None:
    report = _analyze(
        "import httpx\n"
        "import requests\n"
        "async def post():\n"
        "    return requests.request('POST', 'https://example.invalid')\n"
        "async def delete():\n"
        "    return httpx.request(\n"
        "        url='https://example.invalid', method='delete'\n"
        "    )\n"
        "async def patch_alias():\n"
        "    send = requests.request\n"
        "    return send('PATCH', 'https://example.invalid')\n"
        "async def dynamic(client, method):\n"
        "    return await client.request(method, 'https://example.invalid')\n"
        "async def get():\n"
        "    return requests.request('GET', 'https://example.invalid')\n"
        "async def head():\n"
        "    return httpx.request(\n"
        "        method='head', url='https://example.invalid'\n"
        "    )\n",
        source_type="custom_file",
        network=True,
    )

    for name in ("post", "delete", "patch_alias", "dynamic"):
        assert report.for_handler(name).detected_effect is ToolEffect.MUTATING
    for name in ("get", "head"):
        assert report.for_handler(name).detected_effect is ToolEffect.READ_ONLY


def test_http_unbound_request_get_and_head_are_not_misclassified() -> None:
    report = _analyze(
        "import http\n"
        "import httpx\n"
        "import requests.sessions\n"
        "async def session_get(session):\n"
        "    return requests.Session.request(\n"
        "        session, 'GET', 'https://example.invalid'\n"
        "    )\n"
        "async def async_client_head(client):\n"
        "    return httpx.AsyncClient.request(\n"
        "        client, http.HTTPMethod.HEAD, 'https://example.invalid'\n"
        "    )\n"
        "async def aliased_head(session):\n"
        "    send = requests.sessions.Session.request\n"
        "    return send(session, 'HEAD', 'https://example.invalid')\n"
        "async def session_post(session):\n"
        "    return requests.Session.request(\n"
        "        session, 'POST', 'https://example.invalid'\n"
        "    )\n"
        "def _local_request(method, url):\n"
        "    return (method, url)\n"
        "async def conditional_post(remote):\n"
        "    if remote:\n"
        "        send = requests.request\n"
        "    else:\n"
        "        send = _local_request\n"
        "    return send('POST', 'https://example.invalid')\n",
        source_type="custom_file",
        network=True,
    )

    for name in ("session_get", "async_client_head", "aliased_head"):
        assert report.for_handler(name).detected_effect is ToolEffect.READ_ONLY
    for name in ("session_post", "conditional_post"):
        assert report.for_handler(name).detected_effect is ToolEffect.MUTATING


def test_handler_alias_call_propagates_reachable_helper_effect() -> None:
    report = _analyze(
        "from pathlib import Path\n"
        "def _persist(path):\n"
        "    Path(path).write_text('changed')\n"
        "def _identity(path):\n"
        "    return path\n"
        "async def save(path, persist):\n"
        "    if persist:\n"
        "        operation = _persist\n"
        "    else:\n"
        "        operation = _identity\n"
        "    return operation(path)\n"
    )

    handler = report.for_handler("save")
    assert handler.detected_effect is ToolEffect.MUTATING
    assert handler.direct_calls == ("_identity", "_persist")
    assert handler.reachable_functions == ("_identity", "_persist", "save")


def test_file_open_and_stream_write_forms_are_conservatively_mutating() -> None:
    report = _analyze(
        "import os\n"
        "from pathlib import Path\n"
        "async def path_write(path):\n"
        "    return Path(path).open('w')\n"
        "async def os_write(path):\n"
        "    return os.open(path, os.O_WRONLY | os.O_CREAT)\n"
        "async def stream_write(file):\n"
        "    file.writelines(['x'])\n"
        "    file.truncate(0)\n"
        "    print('x', file=file)\n"
        "async def os_read(path):\n"
        "    return os.open(path, os.O_RDONLY | os.O_CLOEXEC)\n"
        "async def unknown_flags(path, flags):\n"
        "    return os.open(path, os.O_RDONLY | flags)\n"
    )

    for name in ("path_write", "os_write", "stream_write", "unknown_flags"):
        assert report.for_handler(name).detected_effect is ToolEffect.MUTATING
    assert report.for_handler("os_read").detected_effect is ToolEffect.READ_ONLY


def test_generated_tests_analysis_is_separate_from_tool_effect() -> None:
    tool_report = _analyze("async def answer():\n    return 42\n")
    tests_report = _analyze("def test_answer(file):\n    file.writelines(['fixture'])\n")

    assert tool_report.for_handler("answer").detected_effect is ToolEffect.READ_ONLY
    assert tests_report.for_handler("test_answer").detected_effect is ToolEffect.MUTATING
    assert tool_report.for_handler("answer").effective_effect(ToolEffect.READ_ONLY) is ToolEffect.READ_ONLY


def test_generated_dynamic_execution_bypass_variants_are_denied() -> None:
    samples = {
        "builtins_subscript": (
            "async def builtins_subscript():\n"
            "    return __builtins__['eval']('40 + 2')\n"
        ),
        "builtins_getitem": (
            "async def builtins_getitem():\n"
            "    importer = __builtins__.__getitem__('__import__')\n"
            "    return importer('subprocess').run(['id'])\n"
        ),
        "importlib_dunder": (
            "import importlib\n"
            "async def importlib_dunder():\n"
            "    return importlib.__import__('subprocess').run(['id'])\n"
        ),
        "importlib_module": (
            "import importlib\n"
            "async def importlib_module():\n"
            "    return importlib.import_module('pty').spawn(['/bin/true'])\n"
        ),
        "sys_modules": (
            "import sys\n"
            "async def sys_modules():\n"
            "    loader = sys.modules['importlib'].import_module\n"
            "    return loader('subprocess').run(['id'])\n"
        ),
        "pty_spawn": (
            "import pty\n"
            "async def pty_spawn():\n"
            "    return pty.spawn(['/bin/true'])\n"
        ),
    }

    for handler_name, source in samples.items():
        handler = _analyze(source).for_handler(handler_name)
        assert handler.allowed is False, handler_name
        assert handler.decision is PolicyDecision.DENY, handler_name


def test_orm_and_unknown_http_writes_are_conservatively_mutating() -> None:
    report = _analyze(
        "import urllib.request\n"
        "async def orm_write(session, row):\n"
        "    session.add(row)\n"
        "    await session.flush()\n"
        "async def prepared_send(client, request):\n"
        "    return await client.send(request)\n"
        "async def urlopen_post():\n"
        "    request = urllib.request.Request(\n"
        "        'https://example.invalid', data=b'x', method='POST'\n"
        "    )\n"
        "    return urllib.request.urlopen(request)\n"
        "async def pool_post(pool):\n"
        "    return pool.urlopen('POST', 'https://example.invalid')\n"
        "async def urlopen_get():\n"
        "    return urllib.request.urlopen('https://example.invalid')\n"
        "async def pool_get(pool):\n"
        "    return pool.urlopen('GET', 'https://example.invalid')\n",
        source_type="custom_file",
        network=True,
    )

    for name in ("orm_write", "prepared_send", "urlopen_post", "pool_post"):
        assert report.for_handler(name).detected_effect is ToolEffect.MUTATING
    for name in ("urlopen_get", "pool_get"):
        assert report.for_handler(name).detected_effect is ToolEffect.READ_ONLY


@pytest.mark.parametrize(
    "source",
    [
        "import aiohttp\nasync def probe():\n    return aiohttp.ClientSession()\n",
        "import httpx\nasync def probe():\n    return await httpx.AsyncClient().get('https://api.example')\n",
        "import requests\nasync def probe():\n    return requests.get('https://api.example')\n",
        "import urllib.request\nasync def probe():\n    return urllib.request.urlopen('https://api.example')\n",
        "import socket\nasync def probe():\n    return socket.socket()\n",
    ],
)
def test_custom_file_rejects_raw_network_clients_even_when_authorized(
    source: str,
) -> None:
    report = _analyze(
        source,
        source_type="custom_file",
        network=True,
    )

    assert report.allowed is False
    assert any(item.code == "network.raw_client" for item in report.findings)


def test_safe_request_effect_follows_fixed_or_dynamic_http_method() -> None:
    read_only = _analyze(
        "async def probe():\n"
        "    first = await safe_request('https://api.example')\n"
        "    second = await safe_request('https://api.example', method='HEAD')\n"
        "    return first.text + second.text\n",
        source_type="custom_file",
        network=True,
    )
    mutating = _analyze(
        "async def probe(method):\n"
        "    await safe_request('https://api.example', method='POST', body='x')\n"
        "    return await safe_request('https://api.example', method=method)\n",
        source_type="custom_file",
        network=True,
    )

    assert read_only.detected_effect is ToolEffect.READ_ONLY
    assert mutating.detected_effect is ToolEffect.MUTATING


def test_walrus_aliases_preserve_process_network_and_mutating_evidence() -> None:
    process = _analyze(
        "import os\nasync def probe():\n    return (runner := os.system)('id')\n",
        source_type="custom_file",
    )
    network = _analyze(
        "import requests\nasync def probe():\n"
        "    return (fetch := requests.get)('https://api.example')\n",
        source_type="custom_file",
        network=True,
    )
    mutating = _analyze(
        "from pathlib import Path\nasync def probe():\n"
        "    return (write := Path.write_text)(Path('x'), 'value')\n",
        source_type="custom_file",
    )

    assert process.allowed is False
    assert any(item.capability == "process" for item in process.findings)
    assert process.detected_effect is ToolEffect.MUTATING
    assert network.allowed is False
    assert any(item.code == "network.raw_client" for item in network.findings)
    assert mutating.detected_effect is ToolEffect.MUTATING


def test_call_graph_limit_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(ast_policy_module, "_MAX_CALL_GRAPH_DEPTH", 1)
    report = _analyze("def leaf():\n    return 1\ndef helper():\n    return leaf()\nasync def handler():\n    return helper()\n")

    handler = report.for_handler("handler")
    assert handler.allowed is False
    assert handler.decision is PolicyDecision.DENY
    assert "analysis.call_graph_limit" in {item.code for item in handler.findings}
