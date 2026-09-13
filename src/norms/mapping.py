"""Привязка «пункт перечня → товар» и проверка товара по нормативу.

Привязки лежат в снимке каталога (`Product.norms`): реестр заказчика 1057,
заголовки прайса 838, адрес страницы, описание. Их версия — версия каталога.

Проверка отвечает только тем, что есть в данных, и различает «данные говорят
нет» и «данных нет». У половины каталога привязок нет вовсе — реестра «пункт 838 →
код 1С» у заказчика нет, — и отсутствие привязки не доказывает несоответствия.
Модель объявить товар соответствующим нормативу не может.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from catalog.models import NormRef, Product
from norms.repository import NormRepository, code_key

# Источники, которым верим без проверки: реестр, заголовок прайса, номер в адресе
# ветки перечня, документ в описании. Упоминание и корневой раздел — нет.
STRONG_CONFIDENCE = 0.85


class MappingStatus(StrEnum):
    APPROVED = "APPROVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class NormCheckStatus(StrEnum):
    NORM_OK = "NORM_OK"
    NORM_MISMATCH = "NORM_MISMATCH"
    NORM_UNKNOWN = "NORM_UNKNOWN"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


NORM_CHECK_LABELS = {
    NormCheckStatus.NORM_OK: "Привязка к пункту перечня есть в данных каталога.",
    NormCheckStatus.NORM_MISMATCH: "По данным каталога товар относится к другим пунктам или перечню.",
    NormCheckStatus.NORM_UNKNOWN: "Данных о нормативной привязке нет.",
    NormCheckStatus.REVIEW_REQUIRED: "Привязка неполная или ненадёжная — нужна проверка.",
}


@dataclass(frozen=True)
class NormMapping:
    doc_id: str
    citation: str
    item_code: str | None
    item_title: str | None
    section: str | None
    source: str
    confidence: float
    status: MappingStatus

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "status": str(self.status)}


@dataclass(frozen=True)
class NormCheck:
    status: NormCheckStatus
    doc_id: str | None
    item_code: str | None
    mapping: NormMapping | None
    reason: str

    @property
    def label(self) -> str:
        return NORM_CHECK_LABELS[self.status]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "doc_id": self.doc_id,
            "item_code": self.item_code,
            "mapping": self.mapping.to_dict() if self.mapping else None,
            "reason": self.reason,
            "label": self.label,
        }


def within(code: str | None, point: str) -> bool:
    """Пункт совпадает или лежит внутри подраздела: «2.4» включает «2.4.35»."""
    return bool(code) and (code == point or code.startswith(f"{point}."))


class NormMappingService:
    def __init__(self, repository: NormRepository) -> None:
        self.repository = repository

    def mapping(self, ref: NormRef) -> NormMapping:
        item = self.repository.item(ref.doc_id, ref.item_code) if ref.item_code else None
        strong = bool(ref.item_code) and ref.confidence >= STRONG_CONFIDENCE
        return NormMapping(
            doc_id=ref.doc_id,
            citation=ref.citation,
            item_code=ref.item_code,
            item_title=item.title if item else ref.item_title,
            section=item.section if item else None,
            source=ref.source,
            confidence=ref.confidence,
            status=MappingStatus.APPROVED if strong else MappingStatus.REVIEW_REQUIRED,
        )

    def mappings(
        self, product: Product, audience: str | None = None, doc_id: str | None = None
    ) -> list[NormMapping]:
        """Основания товара, уместные собеседнику: 838 — школе, 1057 — саду."""
        refs = [ref for ref in product.norms_for(audience) if doc_id is None or ref.doc_id == doc_id]
        mappings = [self.mapping(ref) for ref in refs]
        return sorted(
            mappings,
            key=lambda m: (m.status is not MappingStatus.APPROVED, m.doc_id, code_key(m.item_code or "")),
        )

    def products_for(
        self, products: Iterable[Product], doc_id: str, point: str | None = None
    ) -> list[tuple[Product, NormMapping]]:
        """Товары, привязанные к документу и (если задан) пункту или подразделу."""
        found: list[tuple[Product, NormMapping]] = []
        for product in products:
            if not product.is_active:
                continue
            refs = [
                ref
                for ref in product.norms
                if ref.doc_id == doc_id and (point is None or within(ref.item_code, point))
            ]
            if refs:
                best = max(refs, key=lambda ref: (ref.item_code == point, ref.confidence))
                found.append((product, self.mapping(best)))
        return sorted(found, key=lambda pair: (code_key(pair[1].item_code or ""), pair[0].name))

    def check(
        self,
        product: Product | None,
        doc_id: str | None,
        point: str | None = None,
        audience: str | None = None,
    ) -> NormCheck:
        if product is None or doc_id is None:
            reason = "PRODUCT_NOT_MATCHED" if product is None else "NORM_NOT_REQUESTED"
            return NormCheck(NormCheckStatus.NORM_UNKNOWN, doc_id, point, None, reason)

        document = self.repository.document(doc_id)
        if document is None:
            return NormCheck(NormCheckStatus.REVIEW_REQUIRED, doc_id, point, None, "DOCUMENT_UNKNOWN")
        if audience and document.subject not in ("any", audience):
            return NormCheck(
                NormCheckStatus.NORM_MISMATCH, doc_id, point, None, "DOCUMENT_INSTITUTION_CONFLICT"
            )

        refs = [ref for ref in product.norms if ref.doc_id == doc_id]
        if not refs:
            return NormCheck(NormCheckStatus.NORM_UNKNOWN, doc_id, point, None, "NO_MAPPING")

        if point is None:
            best = max(refs, key=lambda ref: (bool(ref.item_code), ref.confidence))
            mapping = self.mapping(best)
            if mapping.status is MappingStatus.APPROVED:
                return NormCheck(NormCheckStatus.NORM_OK, doc_id, None, mapping, "DOCUMENT_MAPPED")
            return NormCheck(NormCheckStatus.REVIEW_REQUIRED, doc_id, None, mapping, "WEAK_MAPPING")

        matching = [ref for ref in refs if within(ref.item_code, point)]
        if matching:
            best = max(matching, key=lambda ref: (ref.item_code == point, ref.confidence))
            mapping = self.mapping(best)
            if mapping.status is MappingStatus.APPROVED:
                return NormCheck(NormCheckStatus.NORM_OK, doc_id, point, mapping, "POINT_MAPPED")
            return NormCheck(NormCheckStatus.REVIEW_REQUIRED, doc_id, point, mapping, "WEAK_MAPPING")

        coded = [ref for ref in refs if ref.item_code]
        if coded:
            # Данные говорят: товар закрывает другие пункты этого перечня.
            mapping = self.mapping(max(coded, key=lambda ref: ref.confidence))
            return NormCheck(NormCheckStatus.NORM_MISMATCH, doc_id, point, mapping, "OTHER_POINT")
        # Привязка к документу без номера пункта: подтвердить пункт нечем.
        mapping = self.mapping(max(refs, key=lambda ref: ref.confidence))
        return NormCheck(NormCheckStatus.REVIEW_REQUIRED, doc_id, point, mapping, "POINT_NOT_MAPPED")
