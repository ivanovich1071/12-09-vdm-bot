"""Импорт выгрузки 1С: загруженный файл, импорт, проблемы строк, предпросмотр."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ImportStatus(StrEnum):
    # Файл сохранён, разбор не закончен. Если процесс упал посередине, повторная
    # загрузка того же файла разберёт его заново, а не вернёт незаконченный импорт.
    UPLOADED = "UPLOADED"
    PARSED = "PARSED"
    # Файл целиком непригоден: не читается, не те колонки, нет ни одного товара.
    INVALID = "INVALID"


class Severity(StrEnum):
    # Товар из этой строки в импорт не попадает.
    ERROR = "ERROR"
    # Товар попадает, но с оговоркой: цена по запросу, наличие неизвестно.
    WARNING = "WARNING"


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    row_number: int | None = None
    column: str | None = None
    sku_1c: str | None = None
    # Лист книги: 1 — товары, 2 — ID элементов Битрикса.
    sheet: int = 1


@dataclass(frozen=True)
class StoredFile:
    id: str
    filename: str
    mime_type: str
    size: int
    checksum: str
    storage_path: str
    uploaded_by: str
    uploaded_at: str
    status: str = "stored"


@dataclass(frozen=True)
class ImportItem:
    """Товар импорта: запись на один код 1С и строки листа, из которых она собрана."""

    sku_1c: str
    name: str
    price: int | None
    stock: int | None
    rows: list[int]
    payload: dict[str, Any]


@dataclass(frozen=True)
class CatalogComparison:
    """Сравнение с каталогом бота: состояние кода 1С и итог сопоставления, только счётчики.

    Новый, существующий и исчезнувший товар определяются по точному коду 1С (D9,
    решение A). Сопоставление (EPIC 3, `matching.py`) проверяет существующие коды и
    ищет новым кодам пару только среди исчезнувших. Изменения цены и остатка по
    позициям — EPIC 4.
    """

    in_catalog: int
    new: int
    missing_from_file: int
    # Сопоставление выполнялось. У импортов, загруженных до EPIC 3, — `False`.
    matching: bool = False
    # Проверка «код → товар» у кодов, которые есть в каталоге: число позиций по статусам.
    existing_by_status: dict[str, int] = field(default_factory=dict)
    # Новые коды, сравнённые с исчезнувшими, и сколько из них получили кандидата.
    # Без исчезнувших кодов оба счётчика — ноль: сравнивать не с чем.
    recoding_checked: int = 0
    recoding_candidates: int = 0
    recoding_by_status: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportSummary:
    # Непустых строк на листе товаров, включая заголовок.
    rows_total: int = 0
    headings: int = 0
    product_rows: int = 0
    # Уникальных кодов 1С в файле, вместе с исключёнными из-за ошибок.
    products: int = 0
    cross_listed: int = 0
    accepted: int = 0
    rejected: int = 0
    errors: int = 0
    warnings: int = 0
    issues_by_code: dict[str, int] = field(default_factory=dict)
    # `None` — база знаний бота не собрана, сравнивать не с чем.
    comparison: CatalogComparison | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ImportSummary:
        comparison = raw.get("comparison")
        return cls(
            **{
                **raw,
                "comparison": CatalogComparison(**comparison) if comparison else None,
            }
        )


@dataclass(frozen=True)
class CatalogImport:
    id: str
    status: ImportStatus
    file: StoredFile
    uploaded_by: str
    created_at: str
    parsed_at: str | None = None
    summary: ImportSummary = field(default_factory=ImportSummary)
    error: str | None = None
    # Этот же файл уже загружали: возвращён прежний импорт, новых записей нет.
    duplicate: bool = False
