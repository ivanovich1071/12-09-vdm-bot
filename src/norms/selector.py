"""Выбор нормативного основания закупки: документ и пункт.

Сценарии (NEXT-1):

- пользователь назвал документ — проверяем, что он для этого учреждения;
- назвал пункт — ищем, в каком документе он есть;
- документ по типу учреждения: школа — 838, детский сад — 1057;
- пункт неизвестен — работаем на уровне документа;
- несколько вариантов — не выбираем, `REVIEW_REQUIRED`;
- у пункта нет ни одного товара — `REVIEW_REQUIRED`.

Один документ другим не подменяется: садику, назвавшему приказ 838, не отвечаем ни
по 838, ни молча по 1057.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from catalog.models import Product
from catalog.placement import institution_code
from norms import documents as docs
from norms.extract import codes_in_query, document_ids_in_text
from norms.mapping import NormMappingService
from norms.repository import NormRepository, QuantityRule, norm_quantity, quantity_rule

DEFAULT_DOCUMENT = {"preschool": docs.ORDER_1057.id, "school": docs.ORDER_838.id}


class NormResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    # Норматив не запрошен: подбор без нормативного фильтра. Документ по типу
    # учреждения при этом известен — по нему называются основания товаров.
    NOT_REQUESTED = "NOT_REQUESTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class NormReason(StrEnum):
    DOCUMENT_FROM_USER = "DOCUMENT_FROM_USER"
    DOCUMENT_FROM_INSTITUTION = "DOCUMENT_FROM_INSTITUTION"
    DOCUMENT_FROM_POINT = "DOCUMENT_FROM_POINT"
    POINT_FROM_USER = "POINT_FROM_USER"
    POINT_UNKNOWN = "POINT_UNKNOWN"
    DOCUMENT_UNKNOWN = "DOCUMENT_UNKNOWN"
    SEVERAL_DOCUMENTS = "SEVERAL_DOCUMENTS"
    DOCUMENT_INSTITUTION_CONFLICT = "DOCUMENT_INSTITUTION_CONFLICT"
    INSTITUTION_UNKNOWN = "INSTITUTION_UNKNOWN"
    POINT_NOT_IN_DOCUMENT = "POINT_NOT_IN_DOCUMENT"
    POINT_IN_SEVERAL_DOCUMENTS = "POINT_IN_SEVERAL_DOCUMENTS"
    POINT_ONLY_IN_CATALOG = "POINT_ONLY_IN_CATALOG"
    NO_PRODUCT_MAPPING = "NO_PRODUCT_MAPPING"


NORM_REASON_LABELS = {
    NormReason.DOCUMENT_FROM_USER: "Документ назван пользователем",
    NormReason.DOCUMENT_FROM_INSTITUTION: "Документ определён по типу учреждения",
    NormReason.DOCUMENT_FROM_POINT: "Документ определён по номеру пункта",
    NormReason.POINT_FROM_USER: "Пункт назван пользователем",
    NormReason.POINT_UNKNOWN: "Пункт перечня не назван — подбор на уровне документа",
    NormReason.DOCUMENT_UNKNOWN: "Документ не распознан",
    NormReason.SEVERAL_DOCUMENTS: "Названо несколько документов",
    NormReason.DOCUMENT_INSTITUTION_CONFLICT: "Документ не относится к этому типу учреждения",
    NormReason.INSTITUTION_UNKNOWN: "Тип учреждения не известен — документ не выбрать",
    NormReason.POINT_NOT_IN_DOCUMENT: "Такого пункта в документе нет",
    NormReason.POINT_IN_SEVERAL_DOCUMENTS: "Пункт с таким номером есть в нескольких документах",
    NormReason.POINT_ONLY_IN_CATALOG: "Пункта нет в тексте документа — он подтверждён только привязками каталога",
    NormReason.NO_PRODUCT_MAPPING: "К пункту в каталоге не привязан ни один товар",
}


@dataclass(frozen=True)
class NormQuery:
    norm_document: str | None = None
    norm_item: str | None = None
    institution_type: str | None = None
    # `None` — запрошен, если назван документ или пункт.
    required: bool | None = None


@dataclass(frozen=True)
class NormResolution:
    status: NormResolutionStatus
    document: str | None
    point: str | None
    norm_version: str
    point_title: str | None = None
    point_section: str | None = None
    # Количество по перечню — только целым числом из текста приказа.
    quantity: int | None = None
    unit: str | None = None
    quantity_rule: QuantityRule | None = None
    reasons: tuple[NormReason, ...] = ()
    candidates: tuple[tuple[str, str | None], ...] = field(default_factory=tuple)

    @property
    def requires_review(self) -> bool:
        return self.status is NormResolutionStatus.REVIEW_REQUIRED

    @property
    def filters(self) -> bool:
        """Сужать ли выдачу нормативом: только когда он запрошен и однозначен."""
        return self.status is NormResolutionStatus.RESOLVED

    @property
    def citation(self) -> str | None:
        if self.document is None or self.document not in docs.DOCUMENTS:
            return None
        citation = docs.get(self.document).citation
        return f"позиция {self.point} — {citation}" if self.point else citation

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "document": self.document,
            "point": self.point,
            "citation": self.citation,
            "point_title": self.point_title,
            "point_section": self.point_section,
            "quantity": self.quantity,
            "unit": self.unit,
            "quantity_rule": str(self.quantity_rule) if self.quantity_rule else None,
            "norm_version": self.norm_version,
            "reasons": [str(reason) for reason in self.reasons],
            "reason_labels": [NORM_REASON_LABELS[reason] for reason in self.reasons],
            "candidates": [{"document": d, "point": p} for d, p in self.candidates],
        }


def document_ids(value: str | None) -> list[str]:
    """`order_838` из «order_838», «838», «приказ № 838»."""
    text = (value or "").strip()
    if not text:
        return []
    if text in docs.DOCUMENTS:
        return [text]
    if f"order_{text}" in docs.DOCUMENTS:
        return [f"order_{text}"]
    return document_ids_in_text(text)


def point_code(value: str | None) -> str | None:
    found = codes_in_query(value or "")
    return found[0] if found else None


class NormSelector:
    def __init__(self, repository: NormRepository, mapping: NormMappingService) -> None:
        self.repository = repository
        self.mapping = mapping

    def resolve(self, query: NormQuery, products: Iterable[Product] | None = None) -> NormResolution:
        audience = institution_code(query.institution_type)
        explicit = bool((query.norm_document or "").strip() or (query.norm_item or "").strip())
        required = query.required if query.required is not None else explicit

        if not required and not explicit:
            inferred = DEFAULT_DOCUMENT.get(audience or "")
            reasons = (NormReason.DOCUMENT_FROM_INSTITUTION,) if inferred else ()
            return self._result(NormResolutionStatus.NOT_REQUESTED, inferred, None, reasons)

        reasons: list[NormReason] = []
        document: str | None = None
        if (query.norm_document or "").strip():
            named = document_ids(query.norm_document)
            if not named:
                return self._review(None, None, [NormReason.DOCUMENT_UNKNOWN])
            if len(named) > 1:
                candidates = tuple((doc_id, None) for doc_id in named)
                return self._review(None, None, [NormReason.SEVERAL_DOCUMENTS], candidates)
            document = named[0]
            reasons.append(NormReason.DOCUMENT_FROM_USER)
            if not _for_audience(document, audience):
                return self._review(document, None, [*reasons, NormReason.DOCUMENT_INSTITUTION_CONFLICT])

        point = point_code(query.norm_item)
        if point is None:
            if document is None:
                document = DEFAULT_DOCUMENT.get(audience or "")
                if document is None:
                    return self._review(None, None, [NormReason.INSTITUTION_UNKNOWN])
                reasons.append(NormReason.DOCUMENT_FROM_INSTITUTION)
            reasons.append(NormReason.POINT_UNKNOWN)
            return self._result(NormResolutionStatus.RESOLVED, document, None, tuple(reasons))

        reasons.append(NormReason.POINT_FROM_USER)
        catalog = list(products) if products is not None else None
        if document is None:
            where = self._documents_with(point, catalog)
            fitting = [doc_id for doc_id in where if _for_audience(doc_id, audience)]
            if not where:
                return self._review(None, point, [*reasons, NormReason.POINT_NOT_IN_DOCUMENT])
            if not fitting:
                candidates = tuple((doc_id, point) for doc_id in where)
                return self._review(
                    None, point, [*reasons, NormReason.DOCUMENT_INSTITUTION_CONFLICT], candidates
                )
            if len(fitting) > 1:
                candidates = tuple((doc_id, point) for doc_id in fitting)
                return self._review(
                    None, point, [*reasons, NormReason.POINT_IN_SEVERAL_DOCUMENTS], candidates
                )
            document = fitting[0]
            reasons.append(NormReason.DOCUMENT_FROM_POINT)
            if document not in self.repository.documents_with(point):
                reasons.append(NormReason.POINT_ONLY_IN_CATALOG)
        elif document not in self.repository.documents_with(point):
            # Текста пункта нет: документ не загружен или вёрстка PDF потеряла строку.
            # Пункт признаётся, только если к нему привязан товар каталога.
            if catalog is None or not self.mapping.products_for(catalog, document, point):
                return self._review(document, point, [*reasons, NormReason.POINT_NOT_IN_DOCUMENT])
            reasons.append(NormReason.POINT_ONLY_IN_CATALOG)

        if catalog is not None and not self.mapping.products_for(catalog, document, point):
            return self._review(document, point, [*reasons, NormReason.NO_PRODUCT_MAPPING])
        return self._result(NormResolutionStatus.RESOLVED, document, point, tuple(reasons))

    def _documents_with(self, point: str, catalog: list[Product] | None) -> list[str]:
        found = set(self.repository.documents_with(point))
        if catalog is not None:
            found.update(
                doc_id
                for doc_id in docs.DOCUMENTS
                if doc_id not in found and self.mapping.products_for(catalog, doc_id, point)
            )
        return sorted(found)

    def _result(
        self,
        status: NormResolutionStatus,
        document: str | None,
        point: str | None,
        reasons: tuple[NormReason, ...],
        candidates: tuple[tuple[str, str | None], ...] = (),
    ) -> NormResolution:
        item = self.repository.item(document, point) if document and point else None
        return NormResolution(
            status=status,
            document=document,
            point=point,
            norm_version=self.repository.version,
            point_title=item.title if item else None,
            point_section=item.section if item else None,
            quantity=norm_quantity(item),
            unit=item.unit if item else None,
            quantity_rule=quantity_rule(item),
            reasons=reasons,
            candidates=candidates,
        )

    def _review(
        self,
        document: str | None,
        point: str | None,
        reasons: list[NormReason],
        candidates: tuple[tuple[str, str | None], ...] = (),
    ) -> NormResolution:
        return self._result(
            NormResolutionStatus.REVIEW_REQUIRED, document, point, tuple(reasons), candidates
        )


def _for_audience(doc_id: str, audience: str | None) -> bool:
    document = docs.DOCUMENTS.get(doc_id)
    if document is None:
        return False
    return audience is None or document.subject in ("any", audience)
