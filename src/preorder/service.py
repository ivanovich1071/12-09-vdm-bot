"""Предзаказ из спецификации или проверенного заказа, передача менеджеру, решения менеджера.

Правила:

- предзаказ собирается по текущей версии каталога; расхождение со спецификацией
  или оценкой видно в позициях, а не исправляется молча;
- оценка заказа должна быть сделана на текущей версии каталога, иначе — сначала
  переоценка;
- передать менеджеру можно только с согласием на обработку ПДн: проверка здесь, а
  не в канале (как у `OrderService.submit`);
- сбой уведомления не теряет предзаказ: он остаётся `READY_FOR_MANAGER`, попытка
  записана, `retry_notifications` повторяет.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime

from catalog.runtime import CatalogRuntime
from core.errors import Conflict, Forbidden, InvalidRequest, NotFound, Notice
from core.models import Customer
from order_import.evaluation import EvaluationStatus, PriceStatus
from order_import.service import OrderCoreService
from preorder.models import (
    NotificationStatus,
    Preorder,
    PreorderEvent,
    PreorderItem,
    PreorderSource,
    PreorderStatus,
    totals_of,
)
from preorder.notifications import NotificationChannel
from preorder.repository import PreorderRepository
from procurement.models import SpecificationStatus
from procurement.service import ProcurementService

log = logging.getLogger(__name__)

MAX_NOTIFICATION_ATTEMPTS = 5
SYSTEM = "system"
_REVIEW_NORMS = {"NORM_MISMATCH", "REVIEW_REQUIRED"}


class PreorderService:
    def __init__(
        self,
        repository: PreorderRepository,
        runtime: CatalogRuntime,
        procurement: ProcurementService,
        orders: OrderCoreService,
        consents: Callable[[str], str | None],
        notifier: NotificationChannel,
        *,
        clock: Callable[[], datetime] | None = None,
        test_owners: frozenset[str] = frozenset(),
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self.procurement = procurement
        self.orders = orders
        self.consents = consents
        self.notifier = notifier
        self._clock = clock or (lambda: datetime.now(UTC))
        self.test_owners = test_owners

    # --- Создание ---------------------------------------------------------------

    def ready(
        self,
        owner: str,
        *,
        source: str | None = None,
        source_id: str | None = None,
        fingerprint: str | None = None,
    ) -> Preorder | None:
        """Готовый предзаказ того же состава: повтор «Оформить» не плодит копии (шаг 5.3)."""
        return self.repository.find_ready(owner, source=source, source_id=source_id, fingerprint=fingerprint)

    def create_from_specification(
        self, spec_id: str, owner: str, channel: str, comment: str | None = None, fingerprint: str | None = None
    ) -> Preorder:
        spec = self.procurement.get_specification(spec_id, owner)
        if spec.status is SpecificationStatus.SUPERSEDED:
            raise Conflict("Спецификация заменена новой — возьмите актуальную.", code="SPECIFICATION_SUPERSEDED")
        warnings: list[Notice] = []
        with self.runtime.turn() as state:
            items = []
            for item in spec.items:
                product = state.index.get(item.product_id)
                flags: list[str] = []
                price = product.price if product is not None and product.is_active else None
                if product is None or not product.is_active:
                    price_status = PriceStatus.PRICE_NOT_FOUND
                    flags.append("REMOVED_FROM_CATALOG")
                elif price is None:
                    price_status = PriceStatus.PRICE_NOT_FOUND
                elif price != item.unit_price:
                    price_status = PriceStatus.PRICE_CHANGED
                    flags.append("PRICE_CHANGED")
                else:
                    price_status = PriceStatus.PRICE_OK
                availability = str(product.availability) if product is not None else "UNKNOWN"
                if availability != "AVAILABLE":
                    flags.append(availability)
                if str(item.norm_status) in _REVIEW_NORMS:
                    flags.append(str(item.norm_status))
                items.append(
                    PreorderItem(
                        line_no=item.line_no,
                        product_id=item.product_id,
                        article=item.article,
                        name=item.name,
                        quantity=item.quantity,
                        quantity_source=str(item.quantity_source),
                        unit_price=price,
                        total_price=price * item.quantity if price is not None else None,
                        availability=availability,
                        match_status="MATCHED_EXACT",
                        price_status=str(price_status),
                        norm_status=str(item.norm_status),
                        norm_document=item.norm_document,
                        norm_item=item.norm_item,
                        document_price=item.unit_price,
                        flags=tuple(flags),
                    )
                )
            version = state.label
        if version != spec.catalog_version:
            warnings.append(
                Notice(
                    "CATALOG_CHANGED_SINCE_SPECIFICATION",
                    f"Каталог обновился после спецификации ({spec.catalog_version} → {version}): "
                    "цены сверены по текущей версии.",
                )
            )
        items_tuple = tuple(items)
        review = any(
            item.price_status == PriceStatus.PRICE_NOT_FOUND or str(item.norm_status) in _REVIEW_NORMS
            for item in items_tuple
        )
        preorder = self._new(owner, channel, PreorderSource.SPECIFICATION, spec.id, None, version, spec.norm_version, items_tuple, review, warnings, comment, fingerprint=fingerprint)
        for status in (PreorderStatus.PRICE_CHECKED, PreorderStatus.READY_FOR_MANAGER):
            preorder = preorder.with_status(status, SYSTEM, self._now())
        self.repository.save(preorder)
        self.procurement.finalize_specification(spec.id, owner)
        self.procurement.enter_order(spec.task_id, owner)
        return preorder

    def create_from_order(
        self, order_id: str, owner: str, channel: str, comment: str | None = None, fingerprint: str | None = None
    ) -> Preorder:
        order = self.orders.get_order(order_id, owner)
        evaluation = self.orders.latest_evaluation(order_id, owner)
        if evaluation is None:
            raise Conflict("Сначала проверьте заказ.", code="EVALUATION_REQUIRED", details={"order_id": order_id})
        if evaluation.status is EvaluationStatus.REJECTED:
            raise Conflict(
                "Заказ не прошёл проверку: ни одна позиция не найдена в каталоге.",
                code="ORDER_REJECTED",
                details={"evaluation_id": evaluation.id},
            )
        current = self.runtime.current().label
        if evaluation.catalog_version != current:
            raise Conflict(
                f"Оценка сделана на версии каталога {evaluation.catalog_version}, текущая — {current}. "
                "Проверьте заказ заново.",
                code="EVALUATION_OUTDATED",
                details={"evaluation_version": evaluation.catalog_version, "current_version": current},
            )
        items = tuple(
            PreorderItem(
                line_no=item.line_no,
                product_id=item.product_id,
                article=item.article,
                name=item.name or item.source_name or "",
                quantity=item.quantity,
                quantity_source="user",
                unit_price=item.current_price,
                total_price=item.current_total,
                availability=str(item.availability),
                match_status=str(item.match_status),
                price_status=str(item.price_status),
                norm_status=str(item.norm_status),
                norm_document=item.norm_document,
                norm_item=item.norm_item,
                source_line=item.source_line,
                source_name=item.source_name,
                source_article=item.source_article,
                document_price=item.document_price,
                flags=tuple(notice.code for notice in (*item.errors, *item.warnings)),
            )
            for item in evaluation.items
        )
        preorder = self._new(
            owner,
            channel,
            PreorderSource.UPLOADED_ORDER,
            order.id,
            evaluation.id,
            evaluation.catalog_version,
            evaluation.norm_version,
            items,
            evaluation.status is EvaluationStatus.REVIEW_REQUIRED,
            [],
            comment,
            fingerprint=fingerprint,
        )
        for status in (
            PreorderStatus.IMPORTED,
            PreorderStatus.MATCHED,
            PreorderStatus.PRICE_CHECKED,
            PreorderStatus.READY_FOR_MANAGER,
        ):
            preorder = preorder.with_status(status, SYSTEM, self._now())
        self.repository.save(preorder)
        return preorder

    # --- Клиент -------------------------------------------------------------------

    def get(self, preorder_id: str, owner: str) -> Preorder:
        preorder = self.repository.get(preorder_id)
        if preorder is None or preorder.owner != owner:
            raise NotFound("Предзаказ не найден.", code="PREORDER_NOT_FOUND", details={"preorder_id": preorder_id})
        return preorder

    def of_owner(self, owner: str, limit: int = 20) -> list[Preorder]:
        return self.repository.of_owner(owner, limit)

    def send_to_manager(self, preorder_id: str, owner: str, customer: Customer) -> Preorder:
        preorder = self.get(preorder_id, owner)
        if preorder.status is PreorderStatus.SENT_TO_MANAGER:
            return preorder
        if preorder.status is not PreorderStatus.READY_FOR_MANAGER:
            raise Conflict(
                f"Предзаказ в статусе {preorder.status} — передать менеджеру нельзя.",
                code="PREORDER_NOT_READY",
            )
        consent_id = self.consents(owner)
        if consent_id is None:
            raise Forbidden(
                "Нет действующего согласия на обработку персональных данных.", code="CONSENT_REQUIRED"
            )
        if not customer.is_complete:
            raise InvalidRequest(
                "Нужны имя и телефон или e-mail — иначе менеджер не свяжется.", code="CUSTOMER_INCOMPLETE"
            )
        preorder = replace(preorder, customer=asdict(customer), consent_id=consent_id, updated_at=self._now())
        self.repository.save(preorder)
        return self._notify(preorder)

    def retry_notifications(self) -> int:
        sent = 0
        for preorder_id in self.repository.failed_notifications(MAX_NOTIFICATION_ATTEMPTS):
            preorder = self.repository.get(preorder_id)
            if preorder is None or preorder.status is not PreorderStatus.READY_FOR_MANAGER or not preorder.customer:
                continue
            if self._notify(preorder).status is PreorderStatus.SENT_TO_MANAGER:
                sent += 1
        return sent

    # --- Менеджер (domain API для админки) -----------------------------------------

    def manager_get(self, preorder_id: str) -> Preorder:
        preorder = self.repository.get(preorder_id)
        if preorder is None:
            raise NotFound("Предзаказ не найден.", code="PREORDER_NOT_FOUND", details={"preorder_id": preorder_id})
        return preorder

    def manager_queue(self, status: PreorderStatus = PreorderStatus.SENT_TO_MANAGER) -> list[Preorder]:
        return self.repository.by_status(status)

    def start_review(self, preorder_id: str, actor: str) -> Preorder:
        preorder = self.manager_get(preorder_id).with_status(PreorderStatus.MANAGER_REVIEW, actor, self._now())
        self.repository.save(preorder)
        return preorder

    def manual_match(self, preorder_id: str, line_no: int, product_id: str, actor: str) -> Preorder:
        """Менеджер выбирает товар для строки. Цена и наличие — из текущего каталога."""
        preorder = self._in_review(preorder_id)
        with self.runtime.turn() as state:
            product = state.index.get(product_id)
            if product is None or not product.is_active:
                raise InvalidRequest(f"Товара {product_id} нет в каталоге.", code="UNKNOWN_PRODUCT")
            item = self._line(preorder, line_no)
            price = product.price
            updated = replace(
                item,
                product_id=product.id,
                article=product.article,
                name=product.name,
                unit_price=price,
                total_price=price * item.quantity if price is not None and item.quantity else None,
                availability=str(product.availability),
                match_status="MATCHED_EXACT",
                price_status=str(PriceStatus.PRICE_OK if price is not None else PriceStatus.PRICE_NOT_FOUND),
                flags=tuple(flag for flag in item.flags if flag not in {"NOT_FOUND", "AMBIGUOUS", "MATCH_REVIEW"}) + ("MANUAL_MATCH",),
            )
        preorder = self._replace_line(preorder, updated)
        self.repository.record_decision(
            "match", f"{preorder_id}:{line_no}", {"product_id": product_id, "previous": item.product_id}, actor, "APPLIED", self._now()
        )
        return preorder

    def set_quantity(self, preorder_id: str, line_no: int, quantity: int, actor: str) -> Preorder:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise InvalidRequest("Количество — целое от 1.", code="INVALID_QUANTITY")
        preorder = self._in_review(preorder_id)
        item = self._line(preorder, line_no)
        updated = replace(
            item,
            quantity=quantity,
            quantity_source="manager",
            total_price=item.unit_price * quantity if item.unit_price is not None else None,
            flags=tuple(flag for flag in item.flags if not flag.startswith("QUANTITY_")),
        )
        preorder = self._replace_line(preorder, updated)
        self.repository.record_decision(
            "quantity", f"{preorder_id}:{line_no}", {"quantity": quantity, "previous": item.quantity}, actor, "APPLIED", self._now()
        )
        return preorder

    def confirm(self, preorder_id: str, actor: str, comment: str | None = None) -> Preorder:
        preorder = self.manager_get(preorder_id)
        unresolved = [
            item.line_no for item in preorder.items if item.product_id is None or item.quantity is None
        ]
        if unresolved and preorder.status is PreorderStatus.MANAGER_REVIEW:
            raise Conflict(
                "Есть строки без товара или количества — сопоставьте или отклоните.",
                code="PREORDER_HAS_UNRESOLVED_LINES",
                details={"lines": unresolved},
            )
        preorder = replace(
            preorder.with_status(PreorderStatus.CONFIRMED, actor, self._now(), comment), manager_comment=comment
        )
        self.repository.save(preorder)
        self.repository.record_decision("approval", preorder_id, {"comment": comment}, actor, "CONFIRMED", self._now())
        return preorder

    def reject(self, preorder_id: str, actor: str, reason: str) -> Preorder:
        if not (reason or "").strip():
            raise InvalidRequest("Укажите причину отклонения.", code="REASON_REQUIRED")
        preorder = replace(
            self.manager_get(preorder_id).with_status(PreorderStatus.REJECTED, actor, self._now(), reason),
            manager_comment=reason,
        )
        self.repository.save(preorder)
        self.repository.record_decision("approval", preorder_id, {"reason": reason}, actor, "REJECTED", self._now())
        return preorder

    def record_recoding(self, old_sku: str, new_sku: str, actor: str, comment: str | None = None) -> str:
        """Решение «старый код 1С = новый код». Каталог не меняет: применение — через админку (NEXT-7)."""
        old_sku, new_sku = (old_sku or "").strip(), (new_sku or "").strip()
        if not old_sku or not new_sku or old_sku == new_sku:
            raise InvalidRequest("Нужны два разных кода 1С.", code="INVALID_RECODING")
        return self.repository.record_decision(
            "recoding", f"{old_sku}->{new_sku}", {"old_sku": old_sku, "new_sku": new_sku, "comment": comment}, actor, "PROPOSED", self._now()
        )

    def decisions(self, kind: str | None = None) -> list[dict]:
        return self.repository.decisions(kind)

    # --- Внутреннее ---------------------------------------------------------------

    def _new(self, owner, channel, source, source_id, evaluation_id, version, norm_version, items, review, warnings, comment, fingerprint=None) -> Preorder:  # noqa: ANN001
        now = self._clock()
        stamp = now.isoformat(timespec="seconds")
        preorder = Preorder(
            id=f"PO-{now:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
            owner=owner,
            channel=channel,
            source=source,
            source_id=source_id,
            evaluation_id=evaluation_id,
            status=PreorderStatus.DRAFT,
            catalog_version=version,
            norm_version=norm_version,
            review_required=review,
            items=items,
            totals=totals_of(items),
            created_at=stamp,
            updated_at=stamp,
            warnings=tuple(warnings),
            comment=(comment or "").strip()[:1000] or None,
            fingerprint=fingerprint,
        )
        return replace(preorder, history=(PreorderEvent(PreorderStatus.DRAFT, SYSTEM, stamp, None),))

    def _notify(self, preorder: Preorder) -> Preorder:
        if preorder.owner in self.test_owners:
            # Тестовый аккаунт автотеста (QA_USER_IDS): ночью 14.09 он отправил менеджеру четыре предзаказа,
            # один — с выдуманным телефоном. Для бота всё как у настоящего, наружу не уходит ничего.
            log.info("Предзаказ %s тестового пользователя — менеджеру не отправлен.", preorder.id)
            preorder = preorder.with_status(PreorderStatus.SENT_TO_MANAGER, SYSTEM, self._now())
            self.repository.save(preorder)
            self.repository.set_notification(
                preorder.id, "qa-test", NotificationStatus.SENT, "тестовый пользователь — не отправлялось", self._now()
            )
            return self.repository.get(preorder.id)  # type: ignore[return-value]
        channel = getattr(self.notifier, "name", "notifier")
        try:
            self.notifier.send(preorder)
        except Exception as exc:
            attempts = self.repository.set_notification(preorder.id, channel, NotificationStatus.FAILED, str(exc), self._now())
            log.error("Предзаказ %s: уведомление не доставлено (попытка %s): %s", preorder.id, attempts, exc)
            return self.repository.get(preorder.id)  # type: ignore[return-value]
        preorder = preorder.with_status(PreorderStatus.SENT_TO_MANAGER, SYSTEM, self._now())
        self.repository.save(preorder)
        self.repository.set_notification(preorder.id, channel, NotificationStatus.SENT, None, self._now())
        return self.repository.get(preorder.id)  # type: ignore[return-value]

    def _in_review(self, preorder_id: str) -> Preorder:
        preorder = self.manager_get(preorder_id)
        if preorder.status is not PreorderStatus.MANAGER_REVIEW:
            raise Conflict("Правка строк — только на проверке менеджера.", code="PREORDER_NOT_IN_REVIEW")
        return preorder

    @staticmethod
    def _line(preorder: Preorder, line_no: int) -> PreorderItem:
        for item in preorder.items:
            if item.line_no == line_no:
                return item
        raise NotFound("Строки нет.", code="PREORDER_LINE_NOT_FOUND", details={"line_no": line_no})

    def _replace_line(self, preorder: Preorder, updated: PreorderItem) -> Preorder:
        items = tuple(updated if item.line_no == updated.line_no else item for item in preorder.items)
        review = any(
            item.product_id is None or item.quantity is None or item.price_status == PriceStatus.PRICE_NOT_FOUND
            for item in items
        )
        preorder = replace(preorder, items=items, totals=totals_of(items), review_required=review, updated_at=self._now())
        self.repository.save(preorder)
        return preorder

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")
