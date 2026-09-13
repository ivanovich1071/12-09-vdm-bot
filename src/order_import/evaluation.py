"""Оценка заказа: сопоставление, цена, наличие, норматив — по строкам и в целом.

Всё считает программа по текущей версии каталога и нормативной базе; модель
статусов не ставит. Строка:

- `REVIEW_REQUIRED` — есть ошибка: товар не найден, неоднозначен, требует проверки,
  нет цены в каталоге, нет количества, норматив не подтверждён или противоречит;
- `READY_WITH_WARNINGS` — только предупреждения: цена изменилась, нет в наличии,
  наличие неизвестно, привязки к нормативу нет;
- `READY` — ни того, ни другого.

`UNKNOWN` в наличии — не «нет в наличии»: это отдельное предупреждение.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from catalog.matcher import MatchStatus
from catalog.models import Availability
from catalog.placement import institution_code
from catalog.runtime import CatalogRuntimeState
from core.errors import Notice
from norms.mapping import NormCheckStatus, NormMappingService
from norms.selector import DEFAULT_DOCUMENT
from order_import.matching import OrderMatcher
from order_import.models import UploadedOrder, UploadedOrderItem


class PriceStatus(StrEnum):
    PRICE_OK = "PRICE_OK"
    PRICE_CHANGED = "PRICE_CHANGED"
    PRICE_NOT_FOUND = "PRICE_NOT_FOUND"


class EvaluationStatus(StrEnum):
    READY = "READY"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


MATCHED = frozenset({MatchStatus.MATCHED_EXACT, MatchStatus.MATCHED_HIGH})


@dataclass(frozen=True)
class OrderEvaluationItem:
    line_no: int
    source_line: int
    source_article: str | None
    source_name: str | None
    quantity: int | None
    document_price: int | None
    match_status: MatchStatus
    match_method: str
    confidence: float
    candidates: tuple[dict[str, Any], ...]
    product_id: str | None
    article: str | None
    name: str | None
    current_price: int | None
    current_total: int | None
    price_status: PriceStatus
    price_delta: int | None
    price_delta_pct: float | None
    availability: Availability
    quantity_available: int | None
    norm_status: NormCheckStatus
    norm_document: str | None
    norm_item: str | None
    norm_reason: str
    errors: tuple[Notice, ...]
    warnings: tuple[Notice, ...]
    status: EvaluationStatus

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for name in ("match_status", "price_status", "availability", "norm_status", "status"):
            data[name] = str(data[name])
        data["candidates"] = list(self.candidates)
        data["errors"] = [notice.to_dict() for notice in self.errors]
        data["warnings"] = [notice.to_dict() for notice in self.warnings]
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OrderEvaluationItem:
        data = dict(raw)
        data["match_status"] = MatchStatus(data["match_status"])
        data["price_status"] = PriceStatus(data["price_status"])
        data["availability"] = Availability(data["availability"])
        data["norm_status"] = NormCheckStatus(data["norm_status"])
        data["status"] = EvaluationStatus(data["status"])
        data["candidates"] = tuple(data["candidates"])
        data["errors"] = tuple(Notice(**notice) for notice in data["errors"])
        data["warnings"] = tuple(Notice(**notice) for notice in data["warnings"])
        return cls(**data)


@dataclass(frozen=True)
class OrderEvaluation:
    id: str
    order_id: str
    owner: str
    status: EvaluationStatus
    catalog_version: str
    norm_version: str
    created_at: str
    items: tuple[OrderEvaluationItem, ...]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "status": str(self.status),
            "catalog_version": self.catalog_version,
            "norm_version": self.norm_version,
            "created_at": self.created_at,
            "items": [item.to_dict() for item in self.items],
            "summary": dict(self.summary),
        }


class OrderEvaluator:
    def __init__(self, mapping: NormMappingService) -> None:
        self.mapping = mapping

    def evaluate(
        self,
        order: UploadedOrder,
        state: CatalogRuntimeState,
        matcher: OrderMatcher,
        *,
        evaluation_id: str,
        created_at: str,
        norm_version: str,
    ) -> OrderEvaluation:
        items = tuple(self._item(order, state, matcher, item) for item in order.items)
        return OrderEvaluation(
            id=evaluation_id,
            order_id=order.id,
            owner=order.owner,
            status=_overall(order, items),
            catalog_version=state.label,
            norm_version=norm_version,
            created_at=created_at,
            items=items,
            summary=_summary(items),
        )

    def _item(
        self,
        order: UploadedOrder,
        state: CatalogRuntimeState,
        matcher: OrderMatcher,
        item: UploadedOrderItem,
    ) -> OrderEvaluationItem:
        where = {"line_no": item.line_no, "source_line": item.source_line}
        errors: list[Notice] = []
        warnings: list[Notice] = []
        match = matcher.match(item)
        product = state.index.get(match.product_id) if match.product_id else None

        if match.status is MatchStatus.NOT_FOUND:
            errors.append(Notice("NOT_FOUND", "Товар в текущем каталоге не найден.", where))
        elif match.status is MatchStatus.AMBIGUOUS:
            errors.append(Notice("AMBIGUOUS", "Подходит несколько товаров — нужен выбор менеджера.", where))
        elif match.status is MatchStatus.MATCHED_REVIEW:
            errors.append(Notice("MATCH_REVIEW", "Сопоставление требует проверки менеджера.", where))

        if item.quantity is None:
            code = "QUANTITY_INVALID" if "QUANTITY_INVALID" in item.issues else "QUANTITY_UNKNOWN"
            errors.append(Notice(code, "Количество не указано или не распознано — уточните.", where))

        price_status, delta, pct = PriceStatus.PRICE_NOT_FOUND, None, None
        current = product.price if product else None
        if product is not None and current is None:
            errors.append(Notice("PRICE_NOT_FOUND", "В каталоге у товара нет цены — уточнит менеджер.", where))
        elif product is not None:
            if item.price is None:
                price_status = PriceStatus.PRICE_OK
                warnings.append(Notice("DOCUMENT_PRICE_MISSING", "В файле цена не указана — берётся текущая.", where))
            elif item.price == current:
                price_status = PriceStatus.PRICE_OK
            else:
                price_status, delta = PriceStatus.PRICE_CHANGED, current - item.price
                pct = round(delta / item.price * 100, 2) if item.price else None
                warnings.append(
                    Notice(
                        "PRICE_CHANGED",
                        f"Цена изменилась: в файле {item.price} ₽, сейчас {current} ₽.",
                        {**where, "document_price": item.price, "current_price": current, "delta": delta, "delta_pct": pct},
                    )
                )

        availability = product.availability if product else Availability.UNKNOWN
        if product is not None:
            if availability is Availability.NOT_AVAILABLE:
                warnings.append(Notice("NOT_AVAILABLE", "Нет в наличии — под заказ.", where))
            elif availability is Availability.UNKNOWN:
                warnings.append(Notice("UNKNOWN_STOCK", "Наличие неизвестно.", where))
            elif item.quantity and product.quantity_available is not None and item.quantity > product.quantity_available:
                warnings.append(
                    Notice(
                        "INSUFFICIENT_STOCK",
                        f"В наличии {product.quantity_available} шт., в заказе {item.quantity}.",
                        where,
                    )
                )

        norm_status, norm_document, norm_item, norm_reason = self._norm(order, item, product)
        if norm_status is NormCheckStatus.NORM_MISMATCH:
            errors.append(Notice("NORM_MISMATCH", "По данным каталога товар не относится к этому пункту или перечню.", where))
        elif norm_status is NormCheckStatus.REVIEW_REQUIRED:
            errors.append(Notice("NORM_REVIEW", "Нормативное основание требует проверки.", where))
        elif norm_status is NormCheckStatus.NORM_UNKNOWN and norm_reason == "NO_MAPPING":
            warnings.append(Notice("NORM_UNKNOWN", "Данных о нормативной привязке товара нет.", where))

        status = (
            EvaluationStatus.REVIEW_REQUIRED
            if errors
            else EvaluationStatus.READY_WITH_WARNINGS
            if warnings
            else EvaluationStatus.READY
        )
        return OrderEvaluationItem(
            line_no=item.line_no,
            source_line=item.source_line,
            source_article=item.article,
            source_name=item.name,
            quantity=item.quantity,
            document_price=item.price,
            match_status=match.status,
            match_method=match.method,
            confidence=match.confidence,
            candidates=tuple(match.candidates_dict()),
            product_id=product.id if product else None,
            article=product.article if product else None,
            name=product.name if product else None,
            current_price=current,
            current_total=current * item.quantity if current is not None and item.quantity else None,
            price_status=price_status,
            price_delta=delta,
            price_delta_pct=pct,
            availability=availability,
            quantity_available=product.quantity_available if product else None,
            norm_status=norm_status,
            norm_document=norm_document,
            norm_item=norm_item,
            norm_reason=norm_reason,
            errors=tuple(errors),
            warnings=tuple(warnings),
            status=status,
        )

    def _norm(self, order: UploadedOrder, item: UploadedOrderItem, product) -> tuple:  # noqa: ANN001
        context = order.context
        audience = institution_code(context.institution_type)
        document = item.norm_document or context.norm_document
        point = item.norm_item or (context.norm_item if not item.norm_document else None)
        if document is None and point is None:
            return NormCheckStatus.NORM_UNKNOWN, None, None, "NORM_NOT_REQUESTED"
        if document is None:
            document = DEFAULT_DOCUMENT.get(audience or "")
            if document is None:
                return NormCheckStatus.REVIEW_REQUIRED, None, point, "DOCUMENT_UNKNOWN"
        if product is None:
            return NormCheckStatus.NORM_UNKNOWN, document, point, "PRODUCT_NOT_MATCHED"
        check = self.mapping.check(product, document, point, audience)
        return check.status, document, point, check.reason


def _overall(order: UploadedOrder, items: tuple[OrderEvaluationItem, ...]) -> EvaluationStatus:
    if not items or all(item.match_status is MatchStatus.NOT_FOUND for item in items):
        return EvaluationStatus.REJECTED
    if any(item.status is EvaluationStatus.REVIEW_REQUIRED for item in items) or order.warnings:
        return EvaluationStatus.REVIEW_REQUIRED
    if any(item.status is EvaluationStatus.READY_WITH_WARNINGS for item in items):
        return EvaluationStatus.READY_WITH_WARNINGS
    return EvaluationStatus.READY


def _summary(items: tuple[OrderEvaluationItem, ...]) -> dict[str, Any]:
    def count(predicate) -> int:  # noqa: ANN001
        return sum(1 for item in items if predicate(item))

    document_amount = sum(item.document_price * item.quantity for item in items if item.document_price is not None and item.quantity)
    current_amount = sum(item.current_total for item in items if item.current_total is not None)
    return {
        "checked": len(items),
        "matched": count(lambda i: i.match_status in MATCHED),
        "match_review": count(lambda i: i.match_status is MatchStatus.MATCHED_REVIEW),
        "ambiguous": count(lambda i: i.match_status is MatchStatus.AMBIGUOUS),
        "not_found": count(lambda i: i.match_status is MatchStatus.NOT_FOUND),
        "price_changed": count(lambda i: i.price_status is PriceStatus.PRICE_CHANGED),
        "price_not_found": count(lambda i: i.price_status is PriceStatus.PRICE_NOT_FOUND),
        "available": count(lambda i: i.product_id and i.availability is Availability.AVAILABLE),
        "not_available": count(lambda i: i.product_id and i.availability is Availability.NOT_AVAILABLE),
        "unknown_stock": count(lambda i: i.product_id and i.availability is Availability.UNKNOWN),
        "quantity_unknown": count(lambda i: i.quantity is None),
        "norm_ok": count(lambda i: i.norm_status is NormCheckStatus.NORM_OK),
        "norm_mismatch": count(lambda i: i.norm_status is NormCheckStatus.NORM_MISMATCH),
        "norm_review": count(lambda i: i.norm_status is NormCheckStatus.REVIEW_REQUIRED),
        "norm_unknown": count(lambda i: i.norm_status is NormCheckStatus.NORM_UNKNOWN and i.norm_reason != "NORM_NOT_REQUESTED"),
        "review_required": count(lambda i: i.status is EvaluationStatus.REVIEW_REQUIRED),
        "document_amount": document_amount,
        "current_amount": current_amount,
        "amount_difference": current_amount - document_amount,
    }
