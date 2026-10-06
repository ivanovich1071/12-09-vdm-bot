"""Оформление заказа: сохранить, отправить, при сбое — повторить."""

from __future__ import annotations

import logging
from pathlib import Path

from core.config import Settings
from core.models import Cart, Customer, Order
from core.storage import Storage
from orders.sinks import (
    Bitrix24Sink,
    CompositeSink,
    GoogleSheetsSink,
    JsonlSink,
    OrderSink,
    SmtpSink,
    XlsxSink,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


def build_local_sink(settings: Settings) -> CompositeSink:
    """Локальные файлы (jsonl + Excel): дубль внешних приёмников и единственный
    приёмник для тестовых владельцев (QA_USER_IDS) — наружу их заявки не ходят."""
    return CompositeSink(
        [
            JsonlSink(path=Path(settings.orders_jsonl_path)),
            XlsxSink(directory=Path(settings.orders_xlsx_dir)),
        ]
    )


def build_sink(settings: Settings) -> OrderSink:
    """Приёмник по конфигурации. Локальный файл всегда включён как дубль.

    Пока CRM недоступна, внешних приёмников два: почта менеджеров (`smtp`) и
    Google Sheets. Любой из них может отвалиться — заказ всё равно окажется в файле
    и в базе, и его не придётся искать по логам.
    """
    # Спецификация в Excel идёт всегда: пока интеграции с 1С нет, это тот вид, в
    # котором заказ можно передать менеджеру и завести руками.
    fallback = build_local_sink(settings)
    if settings.order_sink == "google_sheets":
        if not settings.google_sheets_id:
            log.warning("ORDER_SINK=google_sheets, но GOOGLE_SHEETS_ID пуст — пишем в файл")
            return fallback
        return CompositeSink(
            [
                GoogleSheetsSink(
                    spreadsheet_id=settings.google_sheets_id,
                    credentials_file=settings.google_credentials_file,
                ),
                fallback,
            ]
        )
    if settings.order_sink == "bitrix24":
        return CompositeSink([Bitrix24Sink(webhook_url=""), fallback])
    if settings.order_sink == "smtp":
        if not settings.smtp_host or not settings.order_email_to:
            log.warning("ORDER_SINK=smtp, но SMTP_HOST или ORDER_EMAIL_TO пуст — пишем в файл")
            return fallback
        # Письмо первым, файлы следом: даже если почта отвалится, заявка останется
        # в jsonl и в Excel — `CompositeSink` считает заказ доставленным по любому
        # сработавшему приёмнику и пишет в журнал, что именно не прошло.
        return CompositeSink(
            [
                SmtpSink(
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    user=settings.smtp_user,
                    password=settings.smtp_password,
                    sender=settings.smtp_from,
                    to=settings.order_email_to,
                ),
                fallback,
            ]
        )
    return fallback


class OrderService:
    def __init__(
        self,
        storage: Storage,
        sink: OrderSink,
        *,
        local_sink: OrderSink | None = None,
        qa_user_ids: frozenset[str] = frozenset(),
    ) -> None:
        self.storage = storage
        self.sink = sink
        # Тестовым владельцам клиент обещает «ТЕСТ: заявка сохранена, менеджеру не
        # отправлена» — теперь это правда, а не только текст: наружные приёмники
        # (почта, Sheets) для них не вызываются вовсе (раньше при ORDER_SINK=smtp
        # письмо уходило на настоящий адрес).
        self.local_sink = local_sink or sink
        self.qa_user_ids = qa_user_ids

    def _sink_for(self, order: Order) -> OrderSink:
        if order.user_id in self.qa_user_ids:
            return self.local_sink
        return self.sink

    def submit(self, cart: Cart, customer: Customer, channel: str, extras: list[tuple[str, bytes]] | None = None) -> Order:
        """Создаёт заказ и пытается отправить.

        Согласие проверяется здесь, а не в адаптере: канал не должен уметь обходить
        это правило. `extras` — дополнительные вложения письма менеджеру (полный
        перечень приказа): они живут только в письме и при повторной доставке
        не восстанавливаются.
        """
        consent_id = self.storage.active_consent(cart.user_id)
        if consent_id is None:
            raise PermissionError(
                "Нет действующего согласия на обработку персональных данных: "
                "заказ не оформляется."
            )
        if cart.is_empty:
            raise ValueError("Корзина пуста")

        order = Order.create(cart, customer, channel, consent_id)
        self.storage.save_order(order)
        self._deliver(order, extras or [])
        cart.clear()
        self.storage.save_cart(cart)
        return order

    def submit_lead(self, user_id: str, channel: str, customer: Customer) -> Order:
        """Заявка без состава (шаг 4.4): контакты и суть запроса — без корзины.

        Согласие проверяется так же, как у полного заказа: имя и телефон — те же
        персональные данные. Суть запроса живёт в комментарии к контактам.
        """
        consent_id = self.storage.active_consent(user_id)
        if consent_id is None:
            raise PermissionError(
                "Нет действующего согласия на обработку персональных данных: "
                "заявка не оформляется."
            )
        order = Order.create_lead(user_id, channel, customer, consent_id)
        self.storage.save_order(order)
        self._deliver(order, [])
        return order

    def retry_pending(self) -> int:
        """Повторная отправка залежавшихся заказов. Вызывается планировщиком."""
        sent = 0
        for order in self.storage.pending_orders():
            if order.delivery_attempts >= MAX_ATTEMPTS:
                continue
            if self._deliver(order):
                sent += 1
        return sent

    def _deliver(self, order: Order, extras: list[tuple[str, bytes]] = ()) -> bool:
        order.delivery_attempts += 1
        test_owner = order.user_id in self.qa_user_ids
        sink = self.local_sink if test_owner else self.sink
        try:
            sink.push(order, extras)
        except Exception as exc:
            order.status = "failed"
            order.last_error = str(exc)
            log.error("Заказ %s не доставлен (%s попытка): %s", order.id, order.delivery_attempts, exc)
            self.storage.save_order(order)
            return False
        order.status = "sent"
        order.last_error = None
        self.storage.save_order(order)
        if test_owner:
            log.info("Тестовая заявка %s (QA) сохранена в файлы, менеджерам не отправлялась", order.id)
        else:
            log.info("Заказ %s отправлен в %s", order.id, getattr(sink, "name", "sink"))
        return True
