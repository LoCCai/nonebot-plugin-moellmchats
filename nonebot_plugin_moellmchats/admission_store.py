from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from typing import Protocol, runtime_checkable

from .admission import AdmissionRejected

_LOWER_HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")
_LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")

AdmissionKey = int | str | None


class AdmissionStoreError(AdmissionRejected):
    """Distributed admission state could not be resolved safely."""


class AdmissionLeaseLostError(AdmissionStoreError):
    """A pending or active admission lease expired or changed ownership."""


class AdmissionActivationStatus(str, Enum):
    WAITING = "waiting"
    ACTIVATED = "activated"
    LOST = "lost"


@dataclass(frozen=True)
class AdmissionLease:
    namespace_fingerprint: str
    lease_id: str
    key_fingerprint: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.namespace_fingerprint, str) or not _LOWER_HEX_64_RE.fullmatch(self.namespace_fingerprint):
            raise ValueError("namespace_fingerprint 必须是 64 位小写十六进制字符串")
        if not isinstance(self.lease_id, str) or not _LOWER_HEX_32_RE.fullmatch(self.lease_id):
            raise ValueError("lease_id 必须是 32 位小写十六进制字符串")
        if self.key_fingerprint is not None and (
            not isinstance(self.key_fingerprint, str) or not _LOWER_HEX_64_RE.fullmatch(self.key_fingerprint)
        ):
            raise ValueError("key_fingerprint 必须为空或 64 位小写十六进制字符串")


@dataclass(frozen=True)
class AdmissionSnapshot:
    active: int
    pending: int

    def __post_init__(self) -> None:
        for field, value in (("active", self.active), ("pending", self.pending)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} 必须是非负整数")


@dataclass(frozen=True)
class AdmissionReservation:
    lease: AdmissionLease
    snapshot: AdmissionSnapshot


@dataclass(frozen=True)
class AdmissionActivation:
    status: AdmissionActivationStatus
    snapshot: AdmissionSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.status, AdmissionActivationStatus):
            raise TypeError("status 必须是 AdmissionActivationStatus")


@dataclass(frozen=True)
class AdmissionRenewal:
    renewed: bool
    snapshot: AdmissionSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.renewed, bool):
            raise TypeError("renewed 必须是 bool")


@dataclass(frozen=True)
class AdmissionRelease:
    released: bool
    snapshot: AdmissionSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.released, bool):
            raise TypeError("released 必须是 bool")


def validate_interval(
    value: object,
    *,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} 必须是 {minimum:g} 到 {maximum:g} 的有限秒数")
    return float(value)


@runtime_checkable
class AdmissionStoreProtocol(Protocol):
    async def reserve(self, key: AdmissionKey = None) -> AdmissionReservation: ...

    async def try_activate(self, lease: AdmissionLease) -> AdmissionActivation: ...

    async def renew_active(self, lease: AdmissionLease) -> AdmissionRenewal: ...

    async def release(self, lease: AdmissionLease) -> AdmissionRelease: ...

    async def snapshot(self) -> AdmissionSnapshot: ...
