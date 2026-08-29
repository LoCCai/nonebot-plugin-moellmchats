from __future__ import annotations

from pathlib import Path

import pytest

from nonebot_plugin_moellmchats.custom_tool_loader import load_file_tools
from nonebot_plugin_moellmchats.generated_tool_runner import generated_tool_runner
from nonebot_plugin_moellmchats.tool_contracts import ToolResult


@pytest.mark.asyncio
async def test_custom_file_network_requires_explicit_literal_policy(
    tmp_path: Path, monkeypatch
) -> None:
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
                artifact.handler_name,
                artifact.spec.policy.effective.network,
                artifact.spec.policy.effective.process,
                arguments,
                expected_artifact_digest,
                expected_bundle_digest,
                generation,
                artifact.source,
            )
        )
        return ToolResult(text="ok")

    monkeypatch.setattr(
        generated_tool_runner,
        "execute_artifact",
        execute_artifact,
    )
    implicit = tmp_path / "implicit.py"
    implicit.write_text(
        "async def implicit(value: str):\n    return value\n",
        encoding="utf-8",
    )
    explicit = tmp_path / "explicit.py"
    explicit.write_text(
        "async def explicit(value: str):\n    return value\n\n"
        "TOOLS_REGISTRY = [{\n"
        "  'name': 'explicit',\n"
        "  'description': 'explicit network tool',\n"
        "  'parameters': {\n"
        "    'type': 'object',\n"
        "    'properties': {'value': {'type': 'string'}},\n"
        "    'required': ['value'],\n"
        "  },\n"
        "  'func': explicit,\n"
        "  'capabilities': {\n"
        "    'network': True, 'process': False, 'workspace': True,\n"
        "  },\n"
        "}]\n",
        encoding="utf-8",
    )

    tools, _ = load_file_tools([implicit, explicit], generation=9)
    assert not tools["implicit"]["tool_spec"].policy.effective.network
    assert tools["explicit"]["tool_spec"].policy.effective.network
    for schema in tools.values():
        artifact = schema["tool_artifact"]
        assert artifact.generation == schema["generation"] == 9
        assert artifact.artifact_digest == schema["artifact_digest"]
        assert artifact.contract.declared_effect.value == "read_only"
        assert artifact.contract.effective_effect.value == "read_only"
        assert schema["tool_contract_version"] == 2
        assert schema["artifact_digest_version"] == 2
        assert schema["capability_policy"]["schema_version"] == 2
    implicit.write_text(
        "raise RuntimeError('active file must not be reread')\n",
        encoding="utf-8",
    )
    await tools["implicit"]["func"](value="a")
    await tools["explicit"]["func"](value="b")
    assert [call[:4] for call in calls] == [
        ("implicit", False, False, {"value": "a"}),
        ("explicit", True, False, {"value": "b"}),
    ]
    assert all(call[4] == tools[call[0]]["artifact_digest"] for call in calls)
    assert all(call[5] is None and call[6] == 9 for call in calls)
    assert b"active file must not be reread" not in calls[0][7]


def test_custom_file_rejects_unknown_capability(tmp_path: Path) -> None:
    source = tmp_path / "bad.py"
    source.write_text(
        "async def bad():\n    return 'bad'\n\n"
        "TOOLS_REGISTRY = [{\n"
        "  'name': 'bad', 'description': 'bad policy',\n"
        "  'parameters': {'type': 'object', 'properties': {}},\n"
        "  'func': bad,\n"
        "  'capabilities': {'kernel': True},\n"
        "}]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="capabilities 非法"):
        load_file_tools([source])


def test_custom_file_accepts_static_explicit_host_capabilities(
    tmp_path: Path,
) -> None:
    source = tmp_path / "host.py"
    source.write_text(
        "async def host():\n    return 'ok'\n\n"
        "TOOLS_REGISTRY = [{\n"
        "  'name': 'host', 'description': 'host policy',\n"
        "  'parameters': {'type': 'object', 'properties': {}},\n"
        "  'func': host,\n"
        "  'capabilities': {\n"
        "      'host_filesystem': True, 'secrets': True,\n"
        "  },\n"
        "}]\n",
        encoding="utf-8",
    )

    tools, _ = load_file_tools([source])

    capability = tools["host"]["tool_spec"].policy.effective
    assert capability.host_filesystem is True
    assert capability.secrets is True


def test_custom_file_publishes_scoped_v2_capability_without_legacy_widening(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scoped.py"
    source.write_text(
        "async def scoped():\n    return 'ok'\n\n"
        "TOOLS_REGISTRY = [{\n"
        "  'name': 'scoped', 'description': 'scoped policy',\n"
        "  'parameters': {'type': 'object', 'properties': {}},\n"
        "  'func': scoped,\n"
        "  'capabilities': {\n"
        "    'network': {'allow': ['api.example']},\n"
        "    'filesystem': {'workspace': True, 'host': False},\n"
        "  },\n"
        "}]\n",
        encoding="utf-8",
    )

    tools, _ = load_file_tools([source])
    policy = tools["scoped"]["tool_spec"].policy

    assert policy.requested_v2.network_allow == ("api.example",)
    assert policy.effective.network is True
    assert policy.effective_v2.legacy_runner_compatible is False
    assert policy.effective_v2.safe_http_runner_compatible is True
    assert tools["scoped"]["capability_policy"]["effective"]["network"] == {
        "allow": ["api.example"]
    }


def test_custom_file_network_must_use_safe_request_facade(tmp_path: Path) -> None:
    safe = tmp_path / "safe.py"
    safe.write_text(
        "async def fetch(url: str):\n"
        "    response = await safe_request(url)\n"
        "    return response.text\n\n"
        "TOOLS_REGISTRY = [{\n"
        "  'name': 'fetch', 'description': 'bounded fetch',\n"
        "  'parameters': {'type': 'object', 'properties': {\n"
        "    'url': {'type': 'string'}}, 'required': ['url']},\n"
        "  'func': fetch,\n"
        "  'capabilities': {'network': {'allow': ['api.example']}},\n"
        "}]\n",
        encoding="utf-8",
    )
    raw = tmp_path / "raw.py"
    raw.write_text(
        "import aiohttp\n"
        "async def raw(url: str):\n"
        "    return await aiohttp.request('GET', url)\n\n"
        "TOOLS_REGISTRY = [{\n"
        "  'name': 'raw', 'description': 'raw fetch',\n"
        "  'parameters': {'type': 'object', 'properties': {\n"
        "    'url': {'type': 'string'}}, 'required': ['url']},\n"
        "  'func': raw,\n"
        "  'capabilities': {'network': {'allow': ['api.example']}},\n"
        "}]\n",
        encoding="utf-8",
    )

    tools, _ = load_file_tools([safe])
    assert tools["fetch"]["detected_capabilities"]["network"] is True
    assert tools["fetch"]["capability_policy"]["effective"]["network"] == {
        "allow": ["api.example"]
    }
    with pytest.raises(ValueError, match="safe_request"):
        load_file_tools([raw])


@pytest.mark.parametrize("generation", [True, -1, 1.5, "1"])
def test_custom_file_rejects_invalid_generation(generation) -> None:
    with pytest.raises(ValueError, match="generation"):
        load_file_tools([], generation=generation)
