from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from nonebot_plugin_moellmchats.audit_event import AuditEventRecord, AuditJsonValue
from nonebot_plugin_moellmchats.local_spool import (
    LocalSpoolConfigurationError,
    LocalSpoolDrainRequiredError,
    LocalSpoolKind,
    LocalSpoolOwnershipError,
    LocalSpoolResultUnknownError,
    LocalSpoolSettings,
    LocalSpoolState,
    LocalUsageAuditSpool,
)
from nonebot_plugin_moellmchats.model_usage import ModelUsageRecord

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _usage(run_id: str = "run_1") -> ModelUsageRecord:
    return ModelUsageRecord(
        usage_id=None,
        run_id=run_id,
        provider="provider",
        model="model",
        input_tokens=10,
        output_tokens=4,
        reasoning_tokens=2,
        cached_tokens=3,
        cost=Decimal("1.230000000000"),
        created_at=NOW,
    )


def _audit(
    event_type: str = "runtime_reload",
    *,
    metadata_json: dict[str, AuditJsonValue] | None = None,
) -> AuditEventRecord:
    return AuditEventRecord(
        event_id=None,
        event_type=event_type,
        actor_user_id=None,
        actor_type="system",
        target_type="runtime",
        target_id="runtime",
        run_id=None,
        tool_call_id=None,
        metadata_json=metadata_json or {"generation": 7, "success": True},
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_spool_roundtrip_is_private_canonical_and_lease_bound(
    tmp_path: Path,
) -> None:
    spool = LocalUsageAuditSpool(
        generation=7,
        settings=LocalSpoolSettings(root=tmp_path / "spool"),
        token_factory=lambda: "a" * 32,
        time_ns=lambda: 1_000,
    )
    await spool.start()

    await spool.append_usage((_usage(),))
    diagnostics = spool.safe_diagnostics()
    assert diagnostics["state"] == "running"
    assert diagnostics["ready_files"] == 1
    assert diagnostics["ready_records"] == 1
    assert set(diagnostics) == {
        "generation",
        "leased_files",
        "ready_bytes",
        "ready_files",
        "ready_records",
        "result_unknown_files",
        "state",
    }

    generation_root = tmp_path / "spool" / "generation-7"
    ready = next(generation_root.glob("ready.usage.*.json"))
    assert (tmp_path / "spool").stat().st_mode & 0o777 == 0o700
    assert generation_root.stat().st_mode & 0o777 == 0o700
    assert ready.stat().st_mode & 0o777 == 0o600
    raw = ready.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert (
        json.dumps(
            json.loads(raw),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        == raw
    )
    assert "1.23" in raw

    lease = await spool.lease_next(LocalSpoolKind.USAGE)
    assert lease is not None
    assert lease.records == (_usage(),)
    assert not tuple(generation_root.glob("ready.usage.*.json"))
    assert len(tuple(generation_root.glob("leased.usage.*.json"))) == 1

    await spool.release_unwritten(lease)
    second = await spool.lease_next(LocalSpoolKind.USAGE)
    assert second is not None
    assert second.lease_id != lease.lease_id
    await spool.acknowledge_committed(second)
    assert not tuple(generation_root.iterdir())
    await spool.close()
    assert spool.state is LocalSpoolState.CLOSED


@pytest.mark.asyncio
async def test_spool_supports_immediate_and_batchable_audit_records(
    tmp_path: Path,
) -> None:
    tokens = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32))
    spool = LocalUsageAuditSpool(
        generation=1,
        settings=LocalSpoolSettings(root=tmp_path / "spool"),
        token_factory=lambda: next(tokens),
        time_ns=lambda: 1_000,
    )
    await spool.start()
    immediate = _audit("tool_bundle.approved")
    batchable = _audit()

    await spool.append_audit((immediate, batchable))
    lease = await spool.lease_next(LocalSpoolKind.AUDIT)

    assert lease is not None
    assert lease.records == (immediate, batchable)
    await spool.acknowledge_committed(lease)
    await spool.close()


@pytest.mark.asyncio
async def test_unknown_result_is_durably_quarantined_and_never_released(
    tmp_path: Path,
) -> None:
    tokens = iter(("a" * 32, "b" * 32))
    settings = LocalSpoolSettings(root=tmp_path / "spool")
    spool = LocalUsageAuditSpool(
        generation=2,
        settings=settings,
        token_factory=lambda: next(tokens),
        time_ns=lambda: 1_000,
    )
    await spool.start()
    await spool.append_usage((_usage(),))
    lease = await spool.lease_next(LocalSpoolKind.USAGE)
    assert lease is not None

    await spool.mark_result_unknown(lease)

    assert spool.state is LocalSpoolState.RESULT_UNKNOWN
    assert spool.safe_diagnostics()["result_unknown_files"] == 1
    with pytest.raises(LocalSpoolResultUnknownError):
        await spool.append_usage((_usage("run_2"),))
    with pytest.raises(LocalSpoolResultUnknownError):
        await spool.lease_next(LocalSpoolKind.USAGE)
    with pytest.raises(LocalSpoolDrainRequiredError):
        await spool.close()


@pytest.mark.asyncio
async def test_interrupted_lease_becomes_unknown_on_restart(
    tmp_path: Path,
) -> None:
    settings = LocalSpoolSettings(root=tmp_path / "spool")
    first = LocalUsageAuditSpool(
        generation=3,
        settings=settings,
        token_factory=lambda: "a" * 32,
        time_ns=lambda: 1_000,
    )
    await first.start()
    await first.append_usage((_usage(),))
    assert await first.lease_next(LocalSpoolKind.USAGE) is not None
    # Closing the held descriptor is the same kernel-visible event as a process
    # crash; the stale leased file must then be quarantined by the next owner.
    first._release_generation_lock_sync()

    restarted = LocalUsageAuditSpool(
        generation=3,
        settings=settings,
        token_factory=lambda: "b" * 32,
        time_ns=lambda: 2_000,
    )
    await restarted.start()

    assert restarted.state is LocalSpoolState.RESULT_UNKNOWN
    root = tmp_path / "spool" / "generation-3"
    assert not tuple(root.glob("leased.*.json"))
    assert len(tuple(root.glob("unknown.usage.*.json"))) == 1
    with pytest.raises(LocalSpoolResultUnknownError):
        await restarted.lease_next(LocalSpoolKind.USAGE)


@pytest.mark.asyncio
async def test_spool_rejects_a_second_live_generation_owner(tmp_path: Path) -> None:
    settings = LocalSpoolSettings(root=tmp_path / "spool")
    first = LocalUsageAuditSpool(generation=9, settings=settings)
    second = LocalUsageAuditSpool(generation=9, settings=settings)

    await first.start()
    with pytest.raises(LocalSpoolOwnershipError, match="活跃 owner"):
        await second.start()

    await first.close()
    await second.start()
    await second.close()


@pytest.mark.asyncio
async def test_spool_capacity_counts_leased_file_bytes(tmp_path: Path) -> None:
    tokens = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32))
    spool = LocalUsageAuditSpool(
        generation=10,
        settings=LocalSpoolSettings(
            root=tmp_path / "spool",
            max_ready_bytes=128 * 1024,
        ),
        token_factory=lambda: next(tokens),
        time_ns=lambda: 1_000,
    )
    await spool.start()
    await spool.append_audit((_audit(metadata_json={"blob": "x" * 58_000}),))
    await spool.append_audit((_audit(metadata_json={"blob": "y" * 58_000}),))
    first = await spool.lease_next(LocalSpoolKind.AUDIT)
    second = await spool.lease_next(LocalSpoolKind.AUDIT)
    assert first is not None
    assert second is not None
    assert spool.safe_diagnostics()["ready_bytes"] == 0

    with pytest.raises(LocalSpoolDrainRequiredError, match="上限"):
        await spool.append_audit((_audit(metadata_json={"blob": "z" * 20_000}),))

    await spool.acknowledge_committed(first)
    await spool.acknowledge_committed(second)
    await spool.close()


@pytest.mark.asyncio
async def test_lease_token_failure_leaves_file_ready_and_retryable(tmp_path: Path) -> None:
    responses: list[str | Exception] = ["a" * 32, RuntimeError("entropy unavailable"), "b" * 32]

    def token_factory() -> str:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    root = tmp_path / "spool"
    spool = LocalUsageAuditSpool(
        generation=11,
        settings=LocalSpoolSettings(root=root),
        token_factory=token_factory,
        time_ns=lambda: 1_000,
    )
    await spool.start()
    await spool.append_usage((_usage(),))

    with pytest.raises(LocalSpoolConfigurationError, match="token 生成失败"):
        await spool.lease_next(LocalSpoolKind.USAGE)

    generation_root = root / "generation-11"
    assert len(tuple(generation_root.glob("ready.usage.*.json"))) == 1
    assert not tuple(generation_root.glob("leased.usage.*.json"))
    lease = await spool.lease_next(LocalSpoolKind.USAGE)
    assert lease is not None
    await spool.acknowledge_committed(lease)
    await spool.close()


@pytest.mark.asyncio
async def test_failed_close_with_pending_records_remains_drainable(tmp_path: Path) -> None:
    spool = LocalUsageAuditSpool(
        generation=12,
        settings=LocalSpoolSettings(root=tmp_path / "spool"),
    )
    await spool.start()
    await spool.append_usage((_usage(),))

    with pytest.raises(LocalSpoolDrainRequiredError, match="未确认"):
        await spool.close()

    assert spool.state is LocalSpoolState.RUNNING
    lease = await spool.lease_next(LocalSpoolKind.USAGE)
    assert lease is not None
    await spool.acknowledge_committed(lease)
    await spool.close()
    assert spool.state is LocalSpoolState.CLOSED


@pytest.mark.asyncio
async def test_spool_bounds_are_checked_before_writing(tmp_path: Path) -> None:
    spool = LocalUsageAuditSpool(
        generation=4,
        settings=LocalSpoolSettings(
            root=tmp_path / "spool",
            max_ready_files=1,
            max_ready_bytes=256 * 1024,
        ),
        token_factory=lambda: "a" * 32,
        time_ns=lambda: 1_000,
    )
    await spool.start()
    await spool.append_usage((_usage(),))

    with pytest.raises(LocalSpoolDrainRequiredError, match="上限"):
        await spool.append_usage((_usage("run_2"),))

    assert len(tuple((tmp_path / "spool" / "generation-4").iterdir())) == 1


@pytest.mark.asyncio
async def test_spool_rejects_tampering_before_leasing(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    spool = LocalUsageAuditSpool(
        generation=5,
        settings=LocalSpoolSettings(root=root),
        token_factory=lambda: "a" * 32,
        time_ns=lambda: 1_000,
    )
    await spool.start()
    await spool.append_usage((_usage(),))
    ready = next((root / "generation-5").iterdir())
    ready.write_text('{"forged":true}\n', encoding="utf-8")

    with pytest.raises(LocalSpoolDrainRequiredError, match="损坏"):
        await spool.lease_next(LocalSpoolKind.USAGE)


def test_spool_module_has_no_import_time_instance() -> None:
    import nonebot_plugin_moellmchats.local_spool as module

    assert not any(isinstance(value, LocalUsageAuditSpool) for value in vars(module).values())
