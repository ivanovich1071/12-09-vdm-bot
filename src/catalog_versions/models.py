"""Версия каталога и изменение товара (EPIC 4, D11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class VersionSource(StrEnum):
    BASELINE = "baseline"
    ONE_C = "1c"
    MEDIA = "media"
    REGISTRY = "registry"
    ROLLBACK = "rollback"


class VersionStatus(StrEnum):
    # Снимок записан и проверен, указатель ещё может смотреть на родителя.
    READY = "READY"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class ChangeStatus(StrEnum):
    NEW = "NEW"
    UPDATED = "UPDATED"
    REMOVED = "REMOVED"


SOURCE_LABELS: dict[VersionSource, str] = {
    VersionSource.BASELINE: "исходный каталог",
    VersionSource.ONE_C: "импорт 1С",
    VersionSource.MEDIA: "фото и характеристики",
    VersionSource.REGISTRY: "реестр 1057",
    VersionSource.ROLLBACK: "откат",
}


@dataclass(frozen=True)
class CatalogVersion:
    version: str
    source: VersionSource
    status: VersionStatus
    created_at: str
    snapshot_path: str
    sha256: str
    product_count: int
    parent_version: str | None = None
    import_id: str | None = None
    created_by: str | None = None
    applied_at: str | None = None
    applied_seq: int | None = None
    counters: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    forced: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ProductChange:
    """Изменение товара между снимком родителя и снимком версии."""

    sku_1c: str
    change_status: ChangeStatus
    card: dict[str, Any]
    changed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplyResult:
    """Итог команды, которая может ничего не менять: `media`, `registry`."""

    version: CatalogVersion | None
    message: str

    @property
    def noop(self) -> bool:
        return self.version is None
