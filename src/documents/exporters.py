"""Выгрузка спецификации в Excel и Word.

Поток один: данные ядра → проверенная `Specification` → экспортёр → файл. Модель
бинарный документ не собирает никогда (ТЗ §12). Перед выгрузкой спецификация
проверяется ещё раз: суммы, номера строк, коды, версии.

Колонки и подписи — в шаблоне `templates/specification.json`, общем для обоих
форматов: Excel и Word не расходятся составом.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from core.errors import InvalidRequest
from norms import documents as docs
from procurement.models import QUANTITY_SOURCE_LABELS, Specification
from procurement.specification import validate_specification

TEMPLATES = Path(__file__).parent / "templates"

AVAILABILITY_LABELS = {
    "AVAILABLE": "в наличии",
    "NOT_AVAILABLE": "нет в наличии",
    "UNKNOWN": "нет данных",
}
INSTITUTION_LABELS = {"preschool": "детский сад", "school": "школа"}


@dataclass(frozen=True)
class ExportedDocument:
    filename: str
    media_type: str
    content: bytes


class SpecificationExporter(Protocol):
    format: str
    extension: str
    media_type: str

    def export(self, spec: Specification) -> bytes: ...


@cache
def template() -> dict[str, Any]:
    return json.loads((TEMPLATES / "specification.json").read_text(encoding="utf-8"))


def exporters() -> dict[str, SpecificationExporter]:
    from documents.docx import WordExporter
    from documents.xlsx import ExcelExporter

    return {exporter.format: exporter for exporter in (ExcelExporter(), WordExporter())}


def export_specification(spec: Specification, fmt: str) -> ExportedDocument:
    available = exporters()
    exporter = available.get((fmt or "").lower())
    if exporter is None:
        raise InvalidRequest(
            f"Формат «{fmt}» не поддерживается: {', '.join(sorted(available))}.",
            code="UNSUPPORTED_FORMAT",
            details={"formats": sorted(available)},
        )
    issues = validate_specification(spec)
    if issues:
        raise InvalidRequest(
            "Спецификация не прошла проверку и не выгружается.",
            code="INVALID_SPECIFICATION",
            details={"issues": [issue.to_dict() for issue in issues]},
        )
    return ExportedDocument(
        filename=f"{spec.id}.{exporter.extension}",
        media_type=exporter.media_type,
        content=exporter.export(spec),
    )


def meta_rows(spec: Specification) -> list[tuple[str, str]]:
    header = spec.header
    norm = header.get("norm_citation") or ""
    values = {
        "institution": INSTITUTION_LABELS.get(header.get("institution_type") or "", header.get("institution_type") or ""),
        "institution_name": header.get("institution_name") or "",
        "room": header.get("room") or "",
        "age_group": header.get("age_group") or "",
        "grade": header.get("grade") or "",
        "norm": norm,
        "budget": "" if header.get("budget") is None else str(header["budget"]),
        "catalog_version": spec.catalog_version,
        "norm_version": spec.norm_version,
        "created_at": spec.created_at,
    }
    return [(label, values[key]) for key, label in template()["meta"] if values.get(key)]


def cell_values(spec: Specification) -> list[list[Any]]:
    """Строки таблицы: числа — числами, остальное — текстом."""
    rows: list[list[Any]] = []
    for item in spec.items:
        document = docs.DOCUMENTS.get(item.norm_document or "")
        values = {
            "line_no": item.line_no,
            "article": item.article,
            "name": item.name,
            "quantity": item.quantity,
            "unit": item.unit,
            "unit_price": item.unit_price,
            "total_price": item.total_price,
            "availability": AVAILABILITY_LABELS.get(str(item.availability), str(item.availability)),
            "quantity_source": QUANTITY_SOURCE_LABELS[item.quantity_source],
            "norm_document": document.short_name if document else "",
            "norm_item": item.norm_item or "",
            "selection_reason": item.selection_reason,
        }
        rows.append([values[column["key"]] for column in template()["columns"]])
    return rows


def totals_text(spec: Specification) -> str:
    totals = spec.totals
    text = f"Итого: позиций {totals.positions}, штук {totals.quantity}, сумма {totals.amount} ₽"
    if not totals.complete:
        text += f" (без {totals.missing_prices} позиций без цены)"
    return text
