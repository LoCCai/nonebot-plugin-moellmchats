from __future__ import annotations

from pathlib import Path

import pytest

from nonebot_plugin_moellmchats.custom_tool_loader import load_file_tools
from nonebot_plugin_moellmchats.generated_tool_runner import generated_tool_runner
from nonebot_plugin_moellmchats.generated_tools import GeneratedToolStore
from nonebot_plugin_moellmchats.tool_artifacts import canonical_bundle_digest
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect, ToolResult


def _store(tmp_path: Path) -> GeneratedToolStore:
    store = GeneratedToolStore()
    store.root = tmp_path / "generated"
    store.drafts_dir = store.root / "drafts"
    store.versions_dir = store.root / "versions"
    store.active_file = store.root / "active.json"
    return store


def _manifest(
    *,
    bundle_id: str = "policy_bundle",
    handler: str = "answer",
    description: str = "policy test",
) -> dict:
    return {
        "bundle_id": bundle_id,
        "description": description,
        "tools": [
            {
                "name": handler,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": [],
                },
                "handler": handler,
                "permission": "user",
                "effect": "read_only",
                "timeout_seconds": 5,
                "result_limit": 100,
            }
        ],
    }


_SAFE_TESTS = """async def run_tests(tool_module):
    return "1 passed"
"""


def _publish(
    store: GeneratedToolStore,
    manifest: dict,
    source: str,
    tests_source: str = _SAFE_TESTS,
):
    draft_id, validation = store.create_draft(
        manifest,
        source,
        tests_source,
        request="policy",
        review={"approved": True},
    )
    store.mark_static_validated(draft_id)
    store.mark_sandbox_tested(draft_id, "1 passed")
    store.mark_model_reviewed(draft_id, summary="review passed")
    store.mark_awaiting_approval(draft_id)
    review = store.get_draft_review_snapshot(draft_id)
    change = store.prepare_approval(
        draft_id,
        validation.digest[:12],
        review.review_stamp,
    )
    store._commit_prepared_internal(change)
    bundle_id, digest = tuple(change.result)
    return validation, bundle_id, digest


@pytest.mark.asyncio
async def test_generated_helper_mutation_promotes_declared_read_only_spec(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    source = """from pathlib import Path

def _persist(path):
    Path(path).write_text("changed")

def _identity(path):
    return path

async def answer(path="result.txt", persist=True):
    if persist:
        operation = _persist
    else:
        operation = _identity
    operation(path)
    return "ok"
"""
    validation, bundle_id, digest = _publish(store, _manifest(), source)
    calls = []

    async def execute_artifact(
        artifact,
        arguments,
        context,
        *,
        expected_artifact_digest,
        expected_bundle_digest,
        generation,
    ):
        calls.append(
            (
                artifact,
                arguments,
                expected_artifact_digest,
                expected_bundle_digest,
                generation,
            )
        )
        return ToolResult(text="ok")

    monkeypatch.setattr(
        generated_tool_runner,
        "execute_artifact",
        execute_artifact,
    )

    handler_report = validation.tool_ast_report.for_handler("answer")
    assert handler_report.detected_effect is ToolEffect.MUTATING
    tools, _ = store.load_active_tools(generation=7)
    schema = tools["answer"]
    artifact = schema["tool_artifact"]
    assert schema["declared_effect"] == "read_only"
    assert schema["effective_effect"] == "mutating"
    assert schema["tool_spec"].effect is ToolEffect.MUTATING
    assert artifact.contract.declared_effect is ToolEffect.READ_ONLY
    assert artifact.contract.effective_effect is ToolEffect.MUTATING
    assert artifact.generation == schema["generation"] == 7
    assert artifact.artifact_digest == schema["artifact_digest"]
    assert artifact.bundle_digest == digest
    assert artifact.source == validation.source

    active_source = store.version_path(bundle_id, digest) / "tool.py"
    # Approved versions are owner-readable and immutable by mode.  The test
    # deliberately simulates an administrator changing that live path, so
    # temporarily restore the owner's write bit instead of relying on root to
    # bypass the mode (ordinary CI runs as an unprivileged user).
    active_source.chmod(0o600)
    active_source.write_text(
        "raise RuntimeError('active bundle must not be reread')\n",
        encoding="utf-8",
    )
    result = await schema["func"](path="result.txt")
    assert result.text == "ok"
    assert calls == [
        (
            artifact,
            {"path": "result.txt"},
            artifact.artifact_digest,
            digest,
            7,
        )
    ]
    assert b"active bundle must not be reread" not in calls[0][0].source


def test_generated_tests_mutation_does_not_pollute_tool_effect(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    source = """async def answer(path=""):
    return "ok"
"""
    tests_source = """def _write_fixture():
    with open("test-output.txt", "w") as file:
        file.writelines(["fixture"])

async def run_tests(tool_module):
    operation = _write_fixture
    operation()
    return "1 passed"
"""
    validation, _, _ = _publish(
        store,
        _manifest(),
        source,
        tests_source,
    )

    assert validation.tests_ast_report.detected_effect is ToolEffect.MUTATING
    assert validation.tests_ast_report.for_handler("run_tests").detected_effect is ToolEffect.MUTATING
    assert validation.tool_ast_report.for_handler("answer").detected_effect is ToolEffect.READ_ONLY
    tools, _ = store.load_active_tools()
    assert tools["answer"]["tool_spec"].effect is ToolEffect.READ_ONLY


def test_generated_blocking_finding_in_tests_rejects_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    source = "async def answer(path=''):\n    return 'ok'\n"
    tests_source = """def _unused_process_test():
    import subprocess
    return subprocess.run(["true"])

async def run_tests(tool_module):
    return "1 passed"
"""

    with pytest.raises(ValueError, match=r"tests\.py AST policy 拒绝.*进程"):
        store.create_draft(
            _manifest(),
            source,
            tests_source,
            request="blocked",
            review={"approved": True},
        )


def test_generated_module_dict_process_lookup_rejects_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    source = """import os

async def answer(path=""):
    launch = os.__dict__["system"]
    return launch("id")
"""

    with pytest.raises(ValueError, match=r"tool\.py AST policy 拒绝.*进程"):
        store.create_draft(
            _manifest(),
            source,
            _SAFE_TESTS,
            request="blocked",
            review={"approved": True},
        )


def test_generated_dotted_import_process_call_rejects_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    source = """import os.path

async def answer(path=""):
    return os.system("id")
"""

    with pytest.raises(ValueError, match=r"tool\.py AST policy 拒绝.*进程"):
        store.create_draft(
            _manifest(),
            source,
            _SAFE_TESTS,
            request="blocked",
            review={"approved": True},
        )


def test_generated_tests_dynamic_process_lookup_rejects_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    tests_source = """import os

async def run_tests(tool_module, attribute="system"):
    return os.__getattribute__(attribute)("id")
"""

    with pytest.raises(ValueError, match=r"tests\.py AST policy 拒绝.*进程"):
        store.create_draft(
            _manifest(),
            "async def answer(path=''):\n    return 'ok'\n",
            tests_source,
            request="blocked",
            review={"approved": True},
        )


@pytest.mark.parametrize(
    "source",
    [
        (
            "async def answer(path=''):\n"
            "    return __builtins__['eval']('40 + 2')\n"
        ),
        (
            "async def answer(path=''):\n"
            "    importer = __builtins__.__getitem__('__import__')\n"
            "    return importer('subprocess').run(['id'])\n"
        ),
        (
            "import importlib\n\n"
            "async def answer(path=''):\n"
            "    return importlib.__import__('subprocess').run(['id'])\n"
        ),
        (
            "import pty\n\n"
            "async def answer(path=''):\n"
            "    return pty.spawn(['/bin/true'])\n"
        ),
    ],
)
def test_generated_dynamic_execution_candidates_reject_draft(
    tmp_path: Path,
    source: str,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match=r"tool\.py AST policy 拒绝"):
        store.create_draft(
            _manifest(),
            source,
            _SAFE_TESTS,
            request="blocked",
            review={"approved": True},
        )


def test_custom_loader_promotes_orm_and_unknown_send_effects(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mutating_effects.py"
    source.write_text(
        "async def persist(session=None, row=None):\n"
        "    session.add(row)\n"
        "    await session.flush()\n"
        "    return 'ok'\n"
        "async def transmit(client=None, request=None):\n"
        "    return await client.send(request)\n"
        "TOOLS_REGISTRY = [\n"
        "  {\n"
        "    'name': 'persist', 'description': 'persist one row',\n"
        "    'func': persist,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'effect': 'read_only',\n"
        "  },\n"
        "  {\n"
        "    'name': 'transmit', 'description': 'send prepared request',\n"
        "    'func': transmit,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'effect': 'read_only',\n"
        "  },\n"
        "]\n",
        encoding="utf-8",
    )

    tools, _ = load_file_tools([source])
    assert tools["persist"]["tool_spec"].effect is ToolEffect.MUTATING
    assert tools["transmit"]["tool_spec"].effect is ToolEffect.MUTATING


def test_custom_unrelated_function_does_not_pollute_handler_effect_or_capability(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scoped.py"
    source.write_text(
        "from pathlib import Path\n"
        "def _persist(path):\n"
        "    Path(path).write_text('changed')\n"
        "async def change(path: str):\n"
        "    _persist(path)\n"
        "    return 'ok'\n"
        "async def inspect(path: str):\n"
        "    return path\n"
        "def unrelated_network():\n"
        "    import socket\n"
        "    return socket.socket()\n"
        "TOOLS_REGISTRY = [\n"
        "  {\n"
        "    'name': 'change', 'description': 'change', 'func': change,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'effect': 'read_only',\n"
        "  },\n"
        "  {\n"
        "    'name': 'inspect', 'description': 'inspect', 'func': inspect,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'effect': 'read_only',\n"
        "  },\n"
        "]\n",
        encoding="utf-8",
    )

    tools, _ = load_file_tools([source])
    assert tools["change"]["declared_effect"] == "read_only"
    assert tools["change"]["effective_effect"] == "mutating"
    assert tools["change"]["tool_spec"].effect is ToolEffect.MUTATING
    assert tools["inspect"]["tool_spec"].effect is ToolEffect.READ_ONLY


def test_custom_http_request_promotes_writes_but_not_gets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "http_effects.py"
    source.write_text(
        "import requests\n"
        "async def send():\n"
        "    return requests.request('POST', 'https://example.invalid')\n"
        "async def inspect():\n"
        "    return requests.request('GET', 'https://example.invalid')\n"
        "async def inspect_unbound(session):\n"
        "    send = requests.Session.request\n"
        "    return send(session, 'HEAD', 'https://example.invalid')\n"
        "async def send_unbound(session):\n"
        "    return requests.Session.request(\n"
        "        session, 'DELETE', 'https://example.invalid'\n"
        "    )\n"
        "TOOLS_REGISTRY = [\n"
        "  {\n"
        "    'name': 'send', 'description': 'send', 'func': send,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'effect': 'read_only',\n"
        "    'capabilities': {'network': True},\n"
        "  },\n"
        "  {\n"
        "    'name': 'inspect', 'description': 'inspect', 'func': inspect,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'effect': 'read_only',\n"
        "    'capabilities': {'network': True},\n"
        "  },\n"
        "  {\n"
        "    'name': 'inspect_unbound', 'description': 'inspect unbound',\n"
        "    'func': inspect_unbound,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'effect': 'read_only',\n"
        "    'capabilities': {'network': True},\n"
        "  },\n"
        "  {\n"
        "    'name': 'send_unbound', 'description': 'send unbound',\n"
        "    'func': send_unbound,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'effect': 'read_only',\n"
        "    'capabilities': {'network': True},\n"
        "  },\n"
        "]\n",
        encoding="utf-8",
    )

    tools, _ = load_file_tools([source])
    assert tools["send"]["declared_effect"] == "read_only"
    assert tools["send"]["effective_effect"] == "mutating"
    assert tools["send"]["tool_spec"].effect is ToolEffect.MUTATING
    assert tools["inspect"]["effective_effect"] == "read_only"
    assert tools["inspect"]["tool_spec"].effect is ToolEffect.READ_ONLY
    assert tools["inspect_unbound"]["effective_effect"] == "read_only"
    assert tools["inspect_unbound"]["tool_spec"].effect is ToolEffect.READ_ONLY
    assert tools["send_unbound"]["effective_effect"] == "mutating"
    assert tools["send_unbound"]["tool_spec"].effect is ToolEffect.MUTATING


def test_custom_process_call_promotes_declared_read_only_effect(
    tmp_path: Path,
) -> None:
    source = tmp_path / "process_effect.py"
    source.write_text(
        "import subprocess\n"
        "async def launch():\n"
        "    return subprocess.run(['/bin/true'], check=True).returncode\n"
        "TOOLS_REGISTRY = [\n"
        "  {\n"
        "    'name': 'launch', 'description': 'launch command',\n"
        "    'func': launch,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'effect': 'read_only',\n"
        "    'capabilities': {'process': True},\n"
        "  },\n"
        "]\n",
        encoding="utf-8",
    )

    tools, _ = load_file_tools([source])
    assert tools["launch"]["declared_effect"] == "read_only"
    assert tools["launch"]["effective_effect"] == "mutating"
    assert tools["launch"]["tool_spec"].effect is ToolEffect.MUTATING


def test_custom_capability_is_checked_for_each_handler_call_graph(
    tmp_path: Path,
) -> None:
    source = tmp_path / "capabilities.py"
    template = (
        "def _fetch():\n"
        "    import socket\n"
        "    return socket.socket()\n"
        "async def online():\n"
        "    return _fetch()\n"
        "async def local():\n"
        "    return 'local'\n"
        "TOOLS_REGISTRY = [\n"
        "  {\n"
        "    'name': 'online', 'description': 'online', 'func': online,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "    'capabilities': {'network': NETWORK, 'process': False},\n"
        "  },\n"
        "  {\n"
        "    'name': 'local', 'description': 'local', 'func': local,\n"
        "    'parameters': {'type': 'object', 'properties': {}},\n"
        "  },\n"
        "]\n"
    )
    source.write_text(template.replace("NETWORK", "True"), encoding="utf-8")
    tools, _ = load_file_tools([source])
    assert tools["online"]["tool_spec"].policy.effective.network
    assert not tools["local"]["tool_spec"].policy.effective.network
    assert tools["online"]["tool_spec"].policy.detected.network
    assert not tools["local"]["tool_spec"].policy.detected.network
    assert tools["online"]["detected_capabilities"]["network"] is True
    assert tools["local"]["detected_capabilities"]["network"] is False
    assert tools["online"]["tool_contract_version"] == 2
    assert tools["online"]["artifact_digest_version"] == 2

    source.write_text(template.replace("NETWORK", "False"), encoding="utf-8")
    with pytest.raises(
        ValueError,
        match=r"online AST policy 拒绝.*network capability",
    ):
        load_file_tools([source])


def test_store_digest_matches_tool_artifact_canonical_crlf_and_unicode(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    manifest = _manifest(
        bundle_id="unicode_bundle",
        description="计算日期差值",
    )
    source = "async def answer(path=''):\r\n    return '你好，世界'\r\n"
    tests_source = (
        "async def run_tests(tool_module):\r\n    assert await tool_module.answer() == '你好，世界'\r\n    return '通过'\r\n"
    )
    validation, bundle_id, digest = _publish(
        store,
        manifest,
        source,
        tests_source,
    )
    expected = canonical_bundle_digest(
        manifest,
        source.encode("utf-8"),
        tests_source.encode("utf-8"),
    )

    assert validation.digest == expected == digest
    version = store.version_path(bundle_id, digest)
    assert store.validate_bundle(version).digest == expected


@pytest.mark.parametrize("generation", [True, -1, 1.5, "1"])
def test_generated_loader_rejects_invalid_generation(
    tmp_path: Path,
    generation,
) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="generation"):
        store.load_active_tools(generation=generation)
