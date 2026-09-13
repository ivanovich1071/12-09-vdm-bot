"""Предзаказ: заявка на проверку менеджером, а не окончательный заказ.

До подтверждения менеджером ни наличие, ни резерв, ни срок, ни окончательная цена
не обещаются (ТЗ §11). Статусы и переходы — ниже; каждый переход пишется в историю.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from core.errors import Conflict, Notice


class PreorderStatus(StrEnum):
    DRAFT = "DRAFT"
    IMPORTED = "IMPORTED"
    MATCHED = "MATCHED"
    PRICE_CHECKED = "PRICE_CHECKED"
    READY_FOR_MANAGER = "READY_FOR_MANAGER"
    SENT_TO_MANAGER = "SENT_TO_MANAGER"
    MANAGER_REVIEW = "MANAGER_REVIEW"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


TRANSITIONS: dict[PreorderStatus, frozenset[PreorderStatus]] = {
    PreorderStatus.DRAFT: frozenset({PreorderStatus.IMPORTED, PreorderStatus.PRICE_CHECKED, PreorderStatus.REJECTED}),
    PreorderStatus.IMPORTED: frozenset({PreorderStatus.MATCHED, PreorderStatus.REJECTED}),
    PreorderStatus.MATCHED: frozenset({PreorderStatus.PRICE_CHECKED, PreorderStatus.REJECTED}),
    PreorderStatus.PRICE_CHECKED: frozenset({PreorderStatus.READY_FOR_MANAGER, PreorderStatus.REJECTED}),
    PreorderStatus.READY_FOR_MANAGER: frozenset({PreorderStatus.SENT_TO_MANAGER, PreorderStatus.REJECTED}),
    PreorderStatus.SENT_TO_MANAGER: frozenset({PreorderStatus.MANAGER_REVIEW, PreorderStatus.REJECTED}),
    PreorderStatus.MANAGER_REVIEW: frozenset({PreorderStatus.CONFIRMED, PreorderStatus.REJECTED}),
    PreorderStatus.CONFIRMED: frozenset(),
    PreorderStatus.REJECTED: frozenset(),
}


class PreorderSource(StrEnum):
    SPECIFICATION = "specification"
    UPLOADED_ORDER = "uploaded_order"


class NotificationStatus(StrEnum):
    SENT = "SENT"
    FAILED = "FAILED"


@dataclass(frozen=True)
class PreorderItem:
    line_no: int
    product_id: str | None
    article: str | None
    name: str
    quantity: int | None
    quantity_source: str
    # Текущая цена каталога на момент проверки; окончательную подтверждает менеджер.
    unit_price: int | None
    total_price: int | None
    availability: str
    match_status: str
    price_status: str
    norm_status: str
    norm_document: str | None
    norm_item: str | None
    source_line: int | None = None
    source_name: str | None = None
    source_article: str | None = None
    document_price: int | None = None
    flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["flags"] = list(self.flags)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PreorderItem:
        return cls(**{**raw, "flags": tuple(raw.get("flags", ()))})


@dataclass(frozen=True)
class PreorderEvent:
    status: PreorderStatus
    actor: str
    at: str
    comment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "status": str(self.status)}


@dataclass(frozen=True)
class PreorderTotals:
    positions: int
    quantity: int
    amount: int
    complete: bool
    missing_prices: int
    unknown_quantities: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Preorder:
    id: str
    owner: str
    channel: str
    source: PreorderSource
    source_id: str
    status: PreorderStatus
    catalog_version: str
    norm_version: str
    review_required: bool
    items: tuple[PreorderItem, ...]
    totals: PreorderTotals
    created_at: str
    updated_at: str
    evaluation_id: str | None = None
    warnings: tuple[Notice, ...] = ()
    customer: dict[str, str] | None = None
    consent_id: str | None = None
    comment: str | None = None
    manager_comment: str | None = None
    history: tuple[PreorderEvent, ...] = ()
    notification: NotificationStatus | None = None
    notification_error: str | None = None

    def with_status(self, status: PreorderStatus, actor: str, at: str, comment: str | None = None) -> Preorder:
        if status not in TRANSITIONS[self.status]:
            raise Conflict(
                f"Предзаказ {self.id} в статусе {self.status}: переход в {status} невозможен.",
                code="PREORDER_TRANSITION_NOT_ALLOWED",
                details={"from": str(self.status), "to": str(status)},
            )
        return replace(
            self,
            status=status,
            updated_at=at,
            history=(*self.history, PreorderEvent(status, actor, at, comment)),
        )

    def to_dict(self, *, include_customer: bool = True) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": str(self.source),
            "source_id": self.source_id,
            "evaluation_id": self.evaluation_id,
            "status": str(self.status),
            "is_final_order": False,
            "catalog_version": self.catalog_version,
            "norm_version": self.norm_version,
            "review_required": self.review_required,
            "items": [item.to_dict() for item in self.items],
            "totals": self.totals.to_dict(),
            "warnings": [notice.to_dict() for notice in self.warnings],
            "customer": self.customer if include_customer else None,
            "comment": self.comment,
            "manager_comment": self.manager_comment,
            "history": [event.to_dict() for event in self.history],
            "notification": str(self.notification) if self.notification else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def totals_of(items: tuple[PreorderItem, ...]) -> PreorderTotals:
    return PreorderTotals(
        positions=len(items),
        quantity=sum(item.quantity or 0 for item in items),
        amount=sum(item.total_price for item in items if item.total_price is not None),
        complete=all(item.total_price is not None for item in items),
        missing_prices=sum(1 for item in items if item.unit_price is None),
        unknown_quantities=sum(1 for item in items if item.quantity is None),
    )
