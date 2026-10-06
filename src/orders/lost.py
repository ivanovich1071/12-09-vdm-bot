"""Ушедшие клиенты: письмо менеджеру, когда разговор замолчал на срок.

Вопросы 9, 16 и 17 опросного листа. Решение заказчика: напоминаний клиенту бот
не отправляет вовсе. Вместо этого, когда тишина переваливает за
`CLIENT_LOST_DAYS`, менеджерам уходит одно письмо — кто, что искал, что лежало
в корзине и как с человеком связаться, чтобы живой сотрудник взял его в работу.

История диалога маскируется при записи, поэтому в письмо персональные данные не
попадают; контакты берутся из последней заявки, если человек их оставлял.
"""

from __future__ import annotations

import json
import logging
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from core.config import Settings
from core.storage import Storage

log = logging.getLogger(__name__)

# Сколько последних реплик клиента уходит в письмо: менеджеру нужен след задачи,
# а не вся переписка.
HISTORY_IN_LETTER = 5


def notify_lost_clients(settings: Settings, storage: Storage) -> int:
    """Находит замолчавших и отправляет менеджерам письма. Возвращает число писем."""
    if not settings.lost_notify_enabled:
        return 0
    if not settings.smtp_host or not settings.order_email_to:
        # Куда слать не настроено — тихо пропускаем: это штатный режим локальной
        # разработки, а не сбой, и сыпать предупреждениями раз в полчаса не надо.
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=settings.client_lost_days)).isoformat(
        timespec="seconds"
    )
    sent = 0
    for dialog in storage.stale_dialogs(cutoff, channel="telegram"):
        user_id = dialog["user_id"]
        if user_id in settings.qa_user_ids:
            continue
        letter = _letter(settings, storage, dialog)
        if letter is not None and not _send(settings, *letter):
            # Не отметили: недоставленное письмо попробуем в следующий проход.
            continue
        storage.mark_lost_notified(user_id, dialog["channel"])
        if letter is not None:
            sent += 1
    return sent


def _letter(settings: Settings, storage: Storage, dialog: dict) -> tuple[str, str] | None:
    """Тема и тело письма. `None` — разговора по сути не было, писать не о чем."""
    user_id = dialog["user_id"]
    history = json.loads(dialog["history"])
    profile = json.loads(dialog["profile"])
    asked = [
        " ".join(str(turn.get("content", "")).split())[:200]
        for turn in history
        if turn.get("role") == "user" and turn.get("content")
    ][-HISTORY_IN_LETTER:]
    cart = storage.load_cart(user_id)
    if not asked and cart.is_empty and not profile.get("kit") and not profile.get("order"):
        return None

    facts = [
        f"{label}: {profile[key]}"
        for key, label in (
            ("institution", "Учреждение"),
            ("room", "Помещение"),
            ("age", "Возраст"),
            ("region", "Регион"),
        )
        if profile.get(key)
    ]
    lines = [
        f"Клиент молчит с {str(dialog['updated_at'])[:10]} ({settings.client_lost_days}+ дней).",
        f"Канал: {dialog['channel']}. Идентификатор: {user_id}.",
    ]
    contacts = _last_contacts(storage, user_id)
    lines.append("Контакты: " + (contacts if contacts else "не оставлял — ответить некому."))
    if facts:
        lines.append("")
        lines.append("Задача из профиля:")
        lines += [f"- {fact}" for fact in facts]
    if cart.items:
        lines.append("")
        lines.append("Осталось в корзине:")
        lines += [f"- {item.name} — {item.quantity} шт." for item in cart.items[:10]]
    if asked:
        lines.append("")
        lines.append("Что писал (последние реплики):")
        lines += [f"- {line}" for line in asked]
    if profile.get("kit"):
        kit = profile["kit"]
        lines.append("")
        lines.append(f"Разбирали комплектацию: {kit.get('code')} «{kit.get('title')}».")
    return (
        f"Ушедший клиент — молчит с {str(dialog['updated_at'])[:10]}",
        "\n".join(lines),
    )


def _last_contacts(storage: Storage, user_id: str) -> str:
    """Контакты из последней заявки, если человек их оставлял."""
    try:
        orders = storage.orders_of(user_id)
    except Exception:  # noqa: BLE001 — письмо важнее разбора поломки базы
        return ""
    for order in reversed(orders):
        customer = order.customer
        known = " ".join(
            part for part in (customer.name, customer.phone, customer.email) if part
        ).strip()
        if known:
            return known
    return ""


def _send(settings: Settings, subject: str, body: str) -> bool:
    message = EmailMessage()
    message["From"] = settings.smtp_from or settings.smtp_user
    message["To"] = settings.order_email_to
    message["Subject"] = subject
    message.set_content(body)
    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as server:
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(message)
    except Exception as exc:
        log.error("Письмо об ушедшем клиенте не отправлено: %s", exc)
        return False
    return True
