"""Загруженный заказ клиента: файл, строки как в файле и нормализованные поля."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from core.errors import Notice


class UploadStatus(StrEnum):
    PARSED = "PARSED"
    # Файл не читается: заказ сохранён с ошибкой, чтобы человек её увидел.
    FAILED = "FAILED"
    EVALUATED = "EVALUATED"


@dataclass(frozen=True)
class SourceFile:
    filename: str
    media_type: str
    size: int
    checksum: str
    storage_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrderContext:
    """Что известно о закупке помимо файла: учреждение и норматив для проверки."""

    institution_type: str | None = None
    norm_document: str | None = None
    norm_item: str | None = None
    task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UploadedOrderItem:
    line_no: int
    # Номер строки в файле: строка Excel, строка таблицы Word, строка текста PDF.
    source_line: int
    source_table: int
    # Исходные значения распознанных колонок и все ячейки строки.
    raw: dict[str, str]
    cells: tuple[str, ...]
    article: str | None
    name: str | None
    name_canonical: str | None
    manufacturer: str | None
    characteristics: str | None
    dimensions: str | None
    # `None` — количество не указано или не разобрано. Не выдумываем.
    quantity: int | None
    unit: str | None
    price: int | None
    total: int | None
    norm_document: str | None
    norm_item: str | None
    issues: tuple[str, ...] = ()
    # Ручное сопоставление менеджера.
    manual_product_id: str | None = None
    manual_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cells"] = list(self.cells)
        data["issues"] = list(self.issues)
        return data


@dataclass(frozen=True)
class UploadedOrder:
    id: str
    owner: str
    channel: str
    status: UploadStatus
    source_file: SourceFile
    parser: str | None
    catalog_version: str
    norm_version: str
    context: OrderContext
    created_at: str
    updated_at: str
    items: tuple[UploadedOrderItem, ...] = ()
    warnings: tuple[Notice, ...] = field(default_factory=tuple)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": str(self.status),
            "source_file": {
                key: value for key, value in self.source_file.to_dict().items() if key != "storage_path"
            },
            "parser": self.parser,
            "catalog_version": self.catalog_version,
            "norm_version": self.norm_version,
            "context": self.context.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "items": [item.to_dict() for item in self.items],
            "warnings": [notice.to_dict() for notice in self.warnings],
            "error": self.error,
        }
