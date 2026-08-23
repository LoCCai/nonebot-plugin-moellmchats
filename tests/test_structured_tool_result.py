from __future__ import annotations

from dataclasses import FrozenInstanceError
import math
from types import MappingProxyType, SimpleNamespace

import pytest

from nonebot_plugin_moellmchats import generated_tool_worker
from nonebot_plugin_moellmchats import tool_contracts as contracts
from nonebot_plugin_moellmchats.generated_tool_runner import GeneratedToolRunner
from nonebot_plugin_moellmchats.tool_contracts import (
    TOOL_RESULT_MAX_CITATIONS,
    TOOL_RESULT_MAX_FILES,
    TOOL_RESULT_MAX_IMAGES,
    ToolResult,
    ToolResultCitation,
    ToolResultFile,
    mutable_tool_result_json,
    render_tool_result,
)
from nonebot_plugin_moellmchats.tool_execution import _normalize_result


def _file() -> ToolResultFile:
    return ToolResultFile(
        locator="artifact:sha256:0123456789abcdef",
        name="report.pdf",
        media_type="application/pdf",
        size_bytes=42,
        sha256="0" * 64,
    )


def _citation() -> ToolResultCitation:
    return ToolResultCitation(
        title="Weather source",
        url="https://example.com/weather?id=1",
        excerpt="Rain is expected.",
    )


def test_legacy_constructor_order_is_preserved_and_recursively_frozen() -> None:
    source = {
        "nested": {"items": [1, {"ready": True}]},
    }
    result = ToolResult("ok", ("image:one",), source)
    source["nested"]["items"].append(2)  # type: ignore[union-attr]

    assert result.text == "ok"
    assert result.images == ("image:one",)
    assert vars(result)["text"] == "ok"
    assert result.metadata == {
        "nested": {"items": (1, {"ready": True})},
    }
    assert isinstance(result.metadata, MappingProxyType)
    assert isinstance(result.metadata["nested"], MappingProxyType)
    assert result.metadata["nested"]["items"] == (  # type: ignore[index]
        1,
        {"ready": True},
    )
    with pytest.raises(TypeError):
        result.metadata["late"] = True  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.text = "changed"  # type: ignore[misc]


def test_all_structured_fields_detach_and_as_dict_returns_fresh_mutable_tree() -> None:
    structured = {"weather": {"temperature": 26, "flags": ["rain"]}}
    metadata = {"provider": {"generation": 3}}
    file_value = {
        "locator": "attachment:weather-report",
        "name": "weather.json",
        "media_type": "application/json",
    }
    citation_value = {
        "title": "Forecast",
        "url": "https://example.com/forecast",
    }
    result = ToolResult(
        text="forecast",
        metadata=metadata,
        files=(file_value,),  # type: ignore[arg-type]
        structured=structured,
        citations=(citation_value,),  # type: ignore[arg-type]
    )
    structured["weather"]["flags"].append("late")  # type: ignore[index,union-attr]
    metadata["provider"]["generation"] = 4  # type: ignore[index]
    file_value["name"] = "changed"
    citation_value["title"] = "changed"

    assert result.structured == {"weather": {"flags": ("rain",), "temperature": 26}}
    assert result.metadata == {"provider": {"generation": 3}}
    assert result.files == (
        ToolResultFile(
            locator="attachment:weather-report",
            name="weather.json",
            media_type="application/json",
        ),
    )
    assert result.citations == (
        ToolResultCitation(
            title="Forecast",
            url="https://example.com/forecast",
        ),
    )

    payload = result.as_dict()
    payload["structured"]["weather"]["flags"].append("caller")
    payload["metadata"]["provider"]["generation"] = 9
    payload["files"][0]["name"] = "caller"
    assert result.structured == {"weather": {"flags": ("rain",), "temperature": 26}}
    assert result.metadata == {"provider": {"generation": 3}}
    assert result.files[0].name == "weather.json"


@pytest.mark.parametrize(
    "locator",
    [
        "/etc/passwd",
        "C:\\Windows\\secret.txt",
        "file:/etc/passwd",
        "https://example.com/file",
        "artifact:/etc/passwd",
        "artifact:../secret",
        "artifact:safe/../secret",
        "unknown:opaque",
    ],
)
def test_file_references_reject_host_paths_and_unapproved_schemes(
    locator: str,
) -> None:
    with pytest.raises(ValueError, match="locator"):
        ToolResultFile(locator=locator)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"media_type": 1}, "media_type"),
        ({"media_type": False}, "media_type"),
        ({"sha256": 1}, "sha256"),
        ({"sha256": False}, "sha256"),
    ],
)
def test_file_fields_reject_non_string_values_with_domain_error(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ToolResultFile(locator="result:report", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/source",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://user:pass@example.com/source",
        "https://localhost/source",
        "https://127.0.0.1/source",
        "https://[::1]/source",
        "https://metadata.google.internal/source",
        "https://service.internal/source",
        "https://example.com:8443/source",
        "https://example.com/source#fragment",
    ],
)
def test_untrusted_citations_fail_closed(url: str) -> None:
    with pytest.raises(ValueError, match=r"Citation\.url"):
        ToolResultCitation(title="source", url=url)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"text": 1}, "ToolResult.text"),
        ({"images": "image:one"}, "ToolResult.images"),
        ({"images": ("",)}, "ToolResult.images item"),
        ({"metadata": []}, "ToolResult.metadata"),
        ({"metadata": {1: "bad"}}, "JSON 对象键"),
        ({"structured": {"value": math.nan}}, "浮点数必须有限"),
        ({"structured": {"value": math.inf}}, "浮点数必须有限"),
        ({"structured": {"value": 1 << 80}}, "64-bit"),
        ({"structured": {"value": object()}}, "JSON 兼容值"),
    ],
)
def test_malformed_or_unbounded_values_fail_at_domain_boundary(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ToolResult(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("container_kind", ["mapping", "list"])
def test_cyclic_structured_json_is_rejected(container_kind: str) -> None:
    if container_kind == "mapping":
        value: object = {}
        value["self"] = value  # type: ignore[index]
    else:
        value = []
        value.append(value)  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="循环引用"):
        ToolResult(structured=value)  # type: ignore[arg-type]


def test_depth_node_field_and_total_payload_limits_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested: object = "leaf"
    for _ in range(26):
        nested = {"child": nested}
    with pytest.raises(ValueError, match="嵌套"):
        ToolResult(structured=nested)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=r"structured.*字节"):
        ToolResult(structured={"payload": "x" * 32_768})
    with pytest.raises(ValueError, match=r"metadata.*字节"):
        ToolResult(metadata={"payload": "x" * 16_384})
    monkeypatch.setattr(contracts, "TOOL_RESULT_MAX_PAYLOAD_BYTES", 512)
    with pytest.raises(ValueError, match="canonical payload"):
        ToolResult(
            text="x" * 400,
            structured={"payload": "y" * 200},
        )

    with pytest.raises(ValueError, match=r"images.*数量"):
        ToolResult(images=tuple("image:x" for _ in range(TOOL_RESULT_MAX_IMAGES + 1)))
    with pytest.raises(ValueError, match=r"files.*数量"):
        ToolResult(files=tuple(_file() for _ in range(TOOL_RESULT_MAX_FILES + 1)))
    with pytest.raises(ValueError, match=r"citations.*数量"):
        ToolResult(citations=tuple(_citation() for _ in range(TOOL_RESULT_MAX_CITATIONS + 1)))


def test_canonical_json_and_model_render_are_stable_and_safe() -> None:
    left = ToolResult(
        text="weather",
        images=("private-image-reference",),
        metadata={"z": 2, "a": {"ready": True}},
        files=(_file(),),
        structured={"z": 2, "a": [1, 2]},
        citations=(_citation(),),
    )
    right = ToolResult(
        text="weather",
        images=("private-image-reference",),
        metadata={"a": {"ready": True}, "z": 2},
        files=(_file(),),
        structured={"a": [1, 2], "z": 2},
        citations=(_citation(),),
    )

    assert left.canonical_json() == right.canonical_json()
    assert render_tool_result(left) == render_tool_result(right)
    rendered = render_tool_result(left)
    assert rendered.startswith("weather\n\n[结构化工具结果]\n{")
    assert '"image_count":1' in rendered
    assert '"structured":{"a":[1,2],"z":2}' in rendered
    assert '"locator":"artifact:sha256:0123456789abcdef"' in rendered
    assert '"url":"https://example.com/weather?id=1"' in rendered
    assert "private-image-reference" not in rendered
    assert "private-image-reference" in left.canonical_json()


def test_render_truncation_is_deterministic_and_uses_existing_marker() -> None:
    result = ToolResult(text="abcdefgh", structured={"value": 1})
    assert result.render(max_chars=4) == "abcd\n...[工具结果已截断]"
    assert result.render(max_chars=4) == render_tool_result(result, max_chars=4)
    with pytest.raises(ValueError, match="max_chars"):
        result.render(max_chars=0)


def test_adapter_worker_and_runner_share_one_canonical_result_contract() -> None:
    raw = {
        "text": "weather",
        "images": ["image:one"],
        "files": [_file().as_dict()],
        "structured": {"temperature": 26, "rain": True},
        "citations": [_citation().as_dict()],
        "metadata": {"provider": "demo"},
    }
    adapter_result = _normalize_result(
        raw,
        spec=SimpleNamespace(result_limit=6_000),
    )
    worker_payload = generated_tool_worker._normalize_result(raw)
    runner_result = GeneratedToolRunner._tool_result({"ok": True, **worker_payload})

    assert adapter_result == runner_result
    assert adapter_result.canonical_json() == runner_result.canonical_json()
    assert render_tool_result(adapter_result) == render_tool_result(runner_result)


def test_optional_file_and_citation_fields_survive_canonical_wire_roundtrip() -> None:
    original = ToolResult(
        files=(ToolResultFile(locator="result:report"),),
        citations=(
            ToolResultCitation(
                title="Source",
                url="https://example.com/source",
            ),
        ),
    )
    worker_payload = generated_tool_worker._normalize_result(original.as_dict())
    restored = GeneratedToolRunner._tool_result({"ok": True, **worker_payload})

    assert restored == original
    assert restored.canonical_json() == original.canonical_json()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("images", None),
        ("images", [1]),
        ("files", None),
        ("files", "result:file"),
        ("citations", None),
        ("citations", {}),
        ("metadata", None),
        ("metadata", []),
    ],
)
def test_runner_never_silently_coerces_malformed_structured_fields(
    field: str,
    value: object,
) -> None:
    response = {
        "ok": True,
        "text": "ok",
        "images": [],
        "files": [],
        "structured": None,
        "citations": [],
        "metadata": {},
    }
    response[field] = value
    with pytest.raises(RuntimeError, match="结构化结果非法"):
        GeneratedToolRunner._tool_result(response)


def test_worker_rejects_nonfinite_and_oversized_structured_payloads() -> None:
    with pytest.raises(ValueError, match="bounded JSON"):
        generated_tool_worker._normalize_result({"structured": {"value": float("nan")}})
    with pytest.raises(ValueError, match="bounded JSON"):
        generated_tool_worker._normalize_result({"structured": {1: "value"}})
    with pytest.raises(ValueError, match="48 KiB"):
        generated_tool_worker._normalize_result({"structured": {"value": "x" * 50_000}})


def test_mutable_json_helper_never_returns_owned_frozen_containers() -> None:
    result = ToolResult(structured={"nested": [1, {"ok": True}]})
    mutable = mutable_tool_result_json(result.structured)
    mutable["nested"][1]["ok"] = False
    assert result.structured == {"nested": (1, {"ok": True})}
