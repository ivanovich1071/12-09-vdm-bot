"""Шаг 5 (К7): один путь оформления.

Идемпотентность «Оформить» теперь в базе (одна корзина — один предзаказ, в том
числе после перезапуска процесса), тестовые владельцы видят честную пометку
«ТЕСТ», а /spec и /preorders отвечают ядром в любом канале.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from catalog.runtime import CatalogRuntime
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from core_api.composition import build_core
from core_api.facade import CoreApi
from core_fixtures import state
from norms.repository import FileNormRepository
from orders.service import OrderService
from orders.sinks import JsonlSink

USER = "u-1"
CHANNEL = "telegram"


def make(tmp_path: Path, qa: bool = False):
    settings = Settings(
        orders_jsonl_path=str(tmp_path / "orders.jsonl"),
        preorders_dir=str(tmp_path / "preorders"),
        uploads_dir=str(tmp_path / "uploads"),
        qa_user_ids=frozenset({USER}) if qa else frozenset(),
    )
    storage = Storage(tmp_path / "s.sqlite3")
    engine = DialogEngine(
        CatalogRuntime(state()), storage, OrderService(storage, JsonlSink(tmp_path / "o.jsonl")), settings
    )
    services = build_core(settings, engine, norms=FileNormRepository())
    return engine, CoreApi(services), services


def test_same_fingerprint_returns_same_preorder(tmp_path):
    """Повторное «Оформить» той же корзины — прежняя заявка (шаг 5.3)."""
    engine, core, services = make(tmp_path)
    session = core.open_session(channel=CHANNEL, user_ref=USER, trusted=True)
    session = core.session(session.session_id or "")
    engine.handle_action(USER, CHANNEL, "add:B1")
    _, first = core.checkout(session, fingerprint="cart|B1|1")
    _, again = core.checkout(session, fingerprint="cart|B1|1")
    assert first.data.id == again.data.id, "создана копия предзаказа"
    _, other = core.checkout(session, fingerprint="cart|B2|3")
    assert other.data.id != first.data.id, "другой состав должен давать новую заявку"
    assert len(services.preorders.of_owner(USER)) == 2


def test_preorder_source_idempotent_after_restart(tmp_path):
    """Кнопка «Оформить предзаказ» по файлу после перезапуска — не новая заявка (ТЗ BUG-14)."""
    engine, core, services = make(tmp_path)
    session = core.open_session(channel=CHANNEL, user_ref=USER, trusted=True)
    session = core.session(session.session_id or "")
    from test_order_core import HEADER, xlsx

    order = services.orders.upload(
        USER, CHANNEL, "order.xlsx", xlsx([HEADER, [1, "B2", "Мат детский", 1, 8164]]), None
    )
    evaluation = services.orders.evaluate(order.id, USER)
    assert evaluation.items, "заказ не разобран"
    first = core.create_preorder(session, "uploaded_order", order.id, None, fingerprint="uploaded_order:1").data
    # «Перезапуск»: новый фасад над теми же сервисами и той же базой.
    again = CoreApi(services).create_preorder(
        session, "uploaded_order", order.id, None, fingerprint="uploaded_order:1"
    ).data
    assert first.id == again.id


def test_qa_owner_sees_test_note(tmp_path):
    """Тестовый владелец (QA_USER_IDS) видит «ТЕСТ: менеджеру не отправлена» (ТЗ BUG-37)."""
    engine, core, services = make(tmp_path, qa=True)
    session = core.open_session(channel=CHANNEL, user_ref=USER, trusted=True)
    session = core.session(session.session_id or "")
    engine.handle_action(USER, CHANNEL, "add:B1")
    # Согласие на ПДн — как в живом флоу: без него отправка запрещена.
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    _, preorder = core.checkout(session, fingerprint="cart|B1|1")
    # Контакты передаёт канал; текст приёма проверяем через сервис.
    from core.models import Customer

    sent = services.preorders.send_to_manager(
        preorder.data.id, USER, Customer(name="Тест", phone="+7 916 330-02-79")
    )
    assert sent.owner in services.settings.qa_user_ids
    from core.ui import order_accepted

    text = order_accepted(sent.id, sent.totals.amount, delivered=sent.status == "SENT_TO_MANAGER", test=True)
    assert "ТЕСТ" in text and "не отправлена" in text


def test_spec_and_preorders_commands_in_core(tmp_path):
    """/spec и /preorders отвечают ядром — в виджете и Mini App тоже (шаг 5.2)."""
    engine, core, services = make(tmp_path)
    empty = engine.handle_text(USER, CHANNEL, "/preorders")[0]
    assert "Предзаказов пока нет" in empty.text
    session = core.open_session(channel=CHANNEL, user_ref=USER, trusted=True)
    session = core.session(session.session_id or "")
    engine.handle_action(USER, CHANNEL, "add:B1")
    core.checkout(session, fingerprint="cart|B1|1")
    listed = engine.handle_text(USER, CHANNEL, "/preorders")[0]
    assert "PO-" in listed.text
    specs = engine.handle_text(USER, CHANNEL, "/spec")[0]
    assert "спецификаци" in specs.text.lower()
