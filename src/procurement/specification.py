"""Спецификация: сборка, проверка, сравнение с текущим каталогом.

Модель передаёт только коды товаров и количества (п. 46 ТЗ). Название, цена,
наличие, пункт перечня и итоги заполняет бэкенд из закреплённой версии каталога.

Спецификация фиксирует `catalog_version` и `norm_version`. Если каталог потом
изменился, это видно через `compare`, но цены в спецификации не пересчитываются:
новая версия — только явным `revise` в сервисе, с `parent_id`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from catalog.models import Product
from catalog.runtime import CatalogRuntimeState
from core.errors import InvalidRequest, Notice
from norms import documents as docs
from norms.mapping import MappingStatus, NormCheckStatus, NormMappingService
from norms.selector import NormResolution
from procurement.models import (
    EXTERNAL_QUANTITY_SOURCES,
    FreshnessStatus,
    ItemChange,
    ProcurementTask,
    QuantitySource,
    Specification,
    SpecificationFreshness,
    SpecificationItem,
    SpecificationStatus,
    SpecificationTotals,
)
from procurement.quantity import QuantityResolver

UNIT = "шт."
MAX_QUANTITY = 100_000
MAX_LINES = 500


@dataclass(frozen=True)
class SpecificationLine:
    product_id: str
    quantity: int | None = None
    source: QuantitySource | None = None
    reason: str = ""


class SpecificationBuilder:
    def __init__(self, mapping: NormMappingService, quantities: QuantityResolver) -> None:
        self.mapping = mapping
        self.quantities = quantities

    def build(
        self,
        *,
        spec_id: str,
        task: ProcurementTask,
        lines: Sequence[SpecificationLine],
        state: CatalogRuntimeState,
        resolution: NormResolution,
        created_at: str,
        parent_id: str | None = None,
    ) -> Specification:
        merged, warnings = _merge(lines)
        items: list[SpecificationItem] = []
        for number, line in enumerate(merged, 1):
            product = state.index.get(line.product_id)
            if product is None:
                raise InvalidRequest(
                    f"Товара {line.product_id} нет в каталоге версии {state.label}.",
                    code="UNKNOWN_PRODUCT",
                    details={"product_id": line.product_id, "catalog_version": state.label},
                )
            if not product.is_active:
                raise InvalidRequest(
                    f"Товар {line.product_id} снят с продажи.",
                    code="INACTIVE_PRODUCT",
                    details={"product_id": line.product_id},
                )
            items.append(self._item(number, task, product, line, resolution))
            warnings += _item_warnings(items[-1])

        totals = _totals(items, task.budget)
        if totals.over_budget:
            warnings.append(
                Notice(
                    "BUDGET_EXCEEDED",
                    f"Сумма {totals.amount} ₽ больше бюджета {task.budget} ₽.",
                    {"amount": totals.amount, "budget": task.budget},
                )
            )
        if resolution.requires_review:
            warnings.append(
                Notice("NORM_REVIEW_REQUIRED", "Нормативное основание требует проверки.")
            )
        return Specification(
            id=spec_id,
            task_id=task.id,
            owner=task.owner,
            status=SpecificationStatus.DRAFT,
            created_at=created_at,
            catalog_version=state.label,
            catalog_sha256=state.sha256,
            norm_version=resolution.norm_version,
            header=_header(task, resolution),
            items=tuple(items),
            totals=totals,
            warnings=tuple(warnings),
            parent_id=parent_id,
        )

    def _item(
        self,
        number: int,
        task: ProcurementTask,
        product: Product,
        line: SpecificationLine,
        resolution: NormResolution,
    ) -> SpecificationItem:
        audience = task.audience
        mappings = self.mapping.mappings(product, audience)
        if line.quantity is not None:
            source = line.source or QuantitySource.USER
            quantity, note = line.quantity, (
                "задано менеджером" if source is QuantitySource.MANAGER else "задано пользователем"
            )
        else:
            decision = self.quantities.resolve(task, product, mappings, resolution)
            quantity, source, note = decision.quantity, decision.source, decision.note

        document = resolution.document
        if document is None:
            approved = [m for m in mappings if m.status is MappingStatus.APPROVED]
            document = approved[0].doc_id if approved else None
        check = self.mapping.check(product, document, resolution.point, audience)
        mapping = check.mapping
        if mapping is None or not mapping.item_code:
            coded = [m for m in mappings if m.doc_id == document and m.item_code]
            mapping = coded[0] if coded else mapping

        return SpecificationItem(
            line_no=number,
            product_id=product.id,
            article=product.article,
            name=product.name,
            quantity=quantity,
            quantity_source=source,
            quantity_note=note,
            unit=UNIT,
            unit_price=product.price,
            total_price=product.price * quantity if product.price is not None else None,
            availability=product.availability,
            norm_document=document if check.status is not NormCheckStatus.NORM_UNKNOWN else None,
            norm_item=mapping.item_code if mapping else None,
            norm_item_title=mapping.item_title if mapping else None,
            norm_status=check.status,
            selection_reason=line.reason or "выбрано пользователем",
            url=product.url,
        )


def validate_specification(spec: Specification) -> list[Notice]:
    """Проверка целостности перед выгрузкой: суммы, номера строк, коды, версии."""
    issues: list[Notice] = []
    if not spec.catalog_version:
        issues.append(Notice("CATALOG_VERSION_MISSING", "Не записана версия каталога."))
    if not spec.norm_version:
        issues.append(Notice("NORM_VERSION_MISSING", "Не записана версия нормативной базы."))
    if not spec.items:
        issues.append(Notice("EMPTY_SPECIFICATION", "В спецификации нет позиций."))
    seen: set[str] = set()
    for number, item in enumerate(spec.items, 1):
        where = {"line_no": item.line_no, "product_id": item.product_id}
        if item.line_no != number:
            issues.append(Notice("LINE_NUMBER", "Номера строк идут не подряд.", where))
        if item.product_id in seen:
            issues.append(Notice("DUPLICATE_PRODUCT", "Товар встречается дважды.", where))
        seen.add(item.product_id)
        if not isinstance(item.quantity, int) or item.quantity < 1:
            issues.append(Notice("INVALID_QUANTITY", "Количество должно быть целым от 1.", where))
        expected = item.unit_price * item.quantity if item.unit_price is not None else None
        if item.total_price != expected:
            issues.append(Notice("TOTAL_MISMATCH", "Сумма строки не равна цене × количество.", where))
        if item.article != item.product_id:
            issues.append(Notice("ARTICLE_MISMATCH", "Артикул не совпадает с кодом 1С.", where))
    expected_totals = _totals(list(spec.items), spec.totals.budget)
    if expected_totals != spec.totals:
        issues.append(Notice("TOTALS_MISMATCH", "Итоги не совпадают с позициями."))
    return issues


def compare(spec: Specification, state: CatalogRuntimeState) -> SpecificationFreshness:
    """Что изменилось в текущем каталоге по позициям спецификации."""
    changes: list[ItemChange] = []
    for item in spec.items:
        product = state.index.get(item.product_id)
        if product is None or not product.is_active:
            changes.append(ItemChange(item.line_no, item.product_id, "removed", item.name, None))
            continue
        if product.price != item.unit_price:
            changes.append(ItemChange(item.line_no, item.product_id, "price", item.unit_price, product.price))
        if product.availability != item.availability:
            changes.append(
                ItemChange(
                    item.line_no, item.product_id, "availability", str(item.availability), str(product.availability)
                )
            )
        if product.name != item.name:
            changes.append(ItemChange(item.line_no, item.product_id, "name", item.name, product.name))
    changed = state.label != spec.catalog_version
    return SpecificationFreshness(
        specification_id=spec.id,
        status=FreshnessStatus.CATALOG_CHANGED if changed else FreshnessStatus.CURRENT,
        specification_version=spec.catalog_version,
        current_version=state.label,
        changes=tuple(changes),
    )


def _merge(lines: Sequence[SpecificationLine]) -> tuple[list[SpecificationLine], list[Notice]]:
    if not lines:
        raise InvalidRequest("В спецификации нет позиций.", code="EMPTY_SPECIFICATION")
    if len(lines) > MAX_LINES:
        raise InvalidRequest(f"Позиций больше {MAX_LINES}.", code="TOO_MANY_LINES")
    merged: dict[str, SpecificationLine] = {}
    warnings: list[Notice] = []
    for line in lines:
        product_id = (line.product_id or "").strip()
        if not product_id:
            raise InvalidRequest("Не указан код товара.", code="UNKNOWN_PRODUCT")
        if line.quantity is not None and (
            isinstance(line.quantity, bool)
            or not isinstance(line.quantity, int)
            or not 1 <= line.quantity <= MAX_QUANTITY
        ):
            raise InvalidRequest(
                f"Количество {line.quantity!r} для {product_id}: нужно целое от 1 до {MAX_QUANTITY}.",
                code="INVALID_QUANTITY",
                details={"product_id": product_id, "quantity": line.quantity},
            )
        if line.source is not None and line.source not in EXTERNAL_QUANTITY_SOURCES:
            raise InvalidRequest(
                f"Источник количества «{line.source}» задаёт только бэкенд.",
                code="QUANTITY_SOURCE_NOT_ALLOWED",
                details={"product_id": product_id, "source": str(line.source)},
            )
        current = merged.get(product_id)
        if current is None:
            merged[product_id] = SpecificationLine(product_id, line.quantity, line.source, line.reason)
            continue
        warnings.append(
            Notice("DUPLICATE_MERGED", f"Товар {product_id} указан дважды — строки объединены.", {"product_id": product_id})
        )
        if current.quantity is not None and line.quantity is not None:
            merged[product_id] = SpecificationLine(
                product_id, current.quantity + line.quantity, current.source, current.reason
            )
    return list(merged.values()), warnings


def _item_warnings(item: SpecificationItem) -> list[Notice]:
    where = {"line_no": item.line_no, "product_id": item.product_id}
    found: list[Notice] = []
    if item.unit_price is None:
        found.append(Notice("PRICE_MISSING", f"Цена «{item.name}» не указана в каталоге — уточнит менеджер.", where))
    if str(item.availability) == "NOT_AVAILABLE":
        found.append(Notice("NOT_AVAILABLE", f"«{item.name}» нет в наличии — под заказ.", where))
    elif str(item.availability) == "UNKNOWN":
        found.append(Notice("UNKNOWN_STOCK", f"Наличие «{item.name}» неизвестно.", where))
    if item.quantity_source is QuantitySource.DEFAULT:
        found.append(Notice("DEFAULT_QUANTITY", f"Количество «{item.name}» не определено — стоит 1.", where))
    if item.norm_status in (NormCheckStatus.REVIEW_REQUIRED, NormCheckStatus.NORM_MISMATCH):
        found.append(Notice(f"NORM_{item.norm_status.value.removeprefix('NORM_')}", "Нормативное основание позиции требует проверки.", where))
    return found


def _totals(items: Sequence[SpecificationItem], budget: int | None) -> SpecificationTotals:
    amount = sum(item.total_price for item in items if item.total_price is not None)
    missing = sum(1 for item in items if item.unit_price is None)
    return SpecificationTotals(
        positions=len(items),
        quantity=sum(item.quantity for item in items),
        amount=amount,
        complete=missing == 0,
        missing_prices=missing,
        budget=budget,
        over_budget=budget is not None and amount > budget,
    )


def _header(task: ProcurementTask, resolution: NormResolution) -> dict[str, object]:
    document = resolution.document if resolution.document in docs.DOCUMENTS else None
    return {
        "institution_type": task.institution_type,
        "institution_name": task.institution_name,
        "room": task.room,
        "zone": task.zone,
        "grade": task.grade,
        "age_group": task.age_group,
        "deadline": task.deadline,
        "budget": task.budget,
        "norm_document": resolution.document,
        "norm_document_name": docs.get(document).short_name if document else None,
        "norm_item": resolution.point,
        "norm_citation": resolution.citation,
        "norm_status": str(resolution.status),
    }
