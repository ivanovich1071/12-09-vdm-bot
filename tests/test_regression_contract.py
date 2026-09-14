"""Контракт baseline под именами из ТЗ v2.

Поведение, которое обязан сохранить каждый следующий EPIC. Сами проверки не
новые: у большинства есть подробные аналоги в профильных файлах (docs/AUDIT.md,
§6). Здесь они собраны под именами, на которые ссылается ТЗ, чтобы поломка
обязательного сценария была видна по названию упавшего теста.

Модель не участвует: агент не подключён, всё решают поиск, инструменты,
маршрутизатор и ядро.
"""

from __future__ import annotations

import json

import pytest

from agent.agent import may_show_cards, tools_for
from agent.routing import CONSULT, SELL, by_rules
from agent.tools import ToolBox
from catalog.models import Product
from catalog.search import CatalogIndex, SearchQuery
from core.config import Settings
from core.dialog import DialogEngine, Session
from core.profile import DialogProfile
from core.storage import Storage
from core.ui import ProductList
from norms import items as norm_items
from norms.items import ItemIndex, NormItem
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"
USER = "u1"

SCHOOL = "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"
PRESCHOOL = "ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"
CITATIONS = {
    "order_838": "приказ Минпросвещения России от 28.11.2024 № 838",
    "order_1057": "приказ Минпросвещения России от 25.12.2024 № 1057",
}


def ref(doc_id: str, code: str) -> dict:
    return {
        "doc_id": doc_id,
        "doc_citation": CITATIONS[doc_id],
        "item_code": code,
        "item_title": None,
        "source": "heading",
        "confidence": 0.9,
    }


def product(sku: str, name: str, roots: list[str], refs: list[dict], price: int) -> Product:
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "url": None,
            "short_url": None,
            "price": price,
            "currency": "RUB",
            "in_stock": 3,
            "category_paths": [[root] for root in roots],
            "description": "",
            "kit_contents": [],
            "norms": refs,
            "bitrix_id": None,
        }
    )


@pytest.fixture
def index() -> CatalogIndex:
    return CatalogIndex(
        [
            product("MILL", "Фрезерный станок с ЧПУ", [SCHOOL], [ref("order_838", "2.20.63")], 253000),
            product(
                "HOOP", "Обруч гимнастический 52 см", [PRESCHOOL], [ref("order_1057", "1.5.1.41")], 245
            ),
            # Один товар в обеих ветках каталога и в обоих приказах — ровно тот
            # случай, на котором 838 и 1057 путались до 02.09.
            product(
                "SPEECH",
                "Речевая игра «Составь сообщение»",
                [PRESCHOOL, SCHOOL],
                [ref("order_838", "2.1.14"), ref("order_1057", "1.13.4.3.1.6")],
                6111,
            ),
        ]
    )


@pytest.fixture
def engine(index, tmp_path, monkeypatch) -> DialogEngine:
    monkeypatch.setattr(norm_items, "load", lambda *_a, **_kw: {})
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    engine = DialogEngine(
        index, storage, OrderService(storage, JsonlSink(tmp_path / "orders.jsonl")), settings
    )
    engine.norm_texts = ItemIndex(
        {
            "order_838": {
                "2.1.14": NormItem("order_838", "2.1.14", "Игровые наборы по русскому языку"),
                "2.20.63": NormItem("order_838", "2.20.63", "Фрезерно-гравировальный станок"),
            },
            "order_1057": {
                "1.5.1.41": NormItem("order_1057", "1.5.1.41", "Обруч гимнастический"),
                "1.13.4.3.1.6": NormItem("order_1057", "1.13.4.3.1.6", "Комплект эмоционального развития"),
            },
        }
    )
    return engine


def tool_names(branch: str) -> set[str]:
    return {schema["function"]["name"] for schema in tools_for(branch) or []}


# --- Нормативы ------------------------------------------------------------------


def test_norm_838(index):
    hits = index.search(
        SearchQuery(text="2.20.63", norm_code="2.20.63", norm_doc_id="order_838", limit=5)
    )
    assert [hit.product.sku_1c for hit in hits] == ["MILL"]
    assert "№ 838" in hits[0].citation()


def test_norm_1057(index):
    hits = index.search(
        SearchQuery(text="1.5.1.41", norm_code="1.5.1.41", norm_doc_id="order_1057", limit=5)
    )
    assert [hit.product.sku_1c for hit in hits] == ["HOOP"]
    assert "№ 1057" in hits[0].citation()


def test_norm_838_not_1057(engine, index):
    """Закупка по 838: садовский перечень не подставляется ни в поиск, ни в основание."""
    assert index.search(
        SearchQuery(text="1.5.1.41", norm_code="1.5.1.41", norm_doc_id="order_838", limit=5)
    ) == []
    assert {ref.doc_id for ref in index.get("SPEECH").norms_for("school")} == {"order_838"}

    box = ToolBox(engine, Session(user_id=USER, channel=CHANNEL))
    result = json.loads(box.run("find_by_norm_code", {"code": "1.13.4.3.1.6", "document": "838"}))
    assert result["found"] == 0
    assert "не содержит" in result["note"]


def test_norm_1057_not_838(engine, index):
    """Закупка по 1057: школьный пункт не отвечает ни по документу, ни по аудитории."""
    assert index.search(
        SearchQuery(text="2.1.14", norm_code="2.1.14", norm_doc_id="order_1057", limit=5)
    ) == []
    assert index.search(
        SearchQuery(text="2.1.14", norm_code="2.1.14", audience="preschool", limit=5)
    ) == []
    assert {ref.doc_id for ref in index.get("SPEECH").norms_for("preschool")} == {"order_1057"}

    box = ToolBox(engine, Session(user_id=USER, channel=CHANNEL))
    result = json.loads(box.run("find_by_norm_code", {"code": "2.1.14", "document": "1057"}))
    assert result["found"] == 0
    assert result["also_in"]["document_id"] == "order_838"


def test_preschool_selection(engine):
    responses = engine.handle_text(USER, CHANNEL, "нужен обруч для детского сада")

    assert engine.session(USER, CHANNEL).profile.audience == "preschool"
    listing = next(item for item in responses if isinstance(item, ProductList))
    assert listing.cards
    assert not any("838" in (card.citation or "") for card in listing.cards)


def test_school_selection(engine):
    responses = engine.handle_text(USER, CHANNEL, "нужен станок для школы")

    assert engine.session(USER, CHANNEL).profile.audience == "school"
    listing = next(item for item in responses if isinstance(item, ProductList))
    citations = [card.citation or "" for card in listing.cards]
    assert any("№ 838" in citation for citation in citations)
    assert not any("1057" in citation for citation in citations)


# --- Роли -----------------------------------------------------------------------


def test_consultant():
    decision = by_rules("что значит приказ 838", DialogProfile())

    assert decision.branch == CONSULT
    assert not tool_names(CONSULT) & {"search_products", "find_by_norm_code", "add_to_cart"}
    ready = DialogProfile(institution="школа", room="кабинет химии", ready_to_see=True)
    allowed, _ = may_show_cards(ready, decision)
    assert not allowed


def test_equipping_a_room_is_a_consultation():
    """ORCHESTRATOR.md (14.09): подбор оборудования для помещения — задача консультанта."""
    assert by_rules("подбери оборудование для кабинета химии в школе", DialogProfile()).branch == CONSULT


def test_salesman():
    decision = by_rules("покажите микроскопы для кабинета химии в школе", DialogProfile())

    assert decision.branch == SELL
    assert decision.ready_to_see
    assert {"search_products", "find_by_norm_code", "add_to_cart"} <= tool_names(SELL)


# --- Корзина и заказ ------------------------------------------------------------


def test_cart(engine):
    engine.handle_action(USER, CHANNEL, "add:MILL")
    engine.handle_action(USER, CHANNEL, "inc:MILL")
    cart = engine.storage.load_cart(USER)
    assert cart.count == 2 and cart.total == 506000

    engine.handle_action(USER, CHANNEL, "dec:MILL")
    assert engine.storage.load_cart(USER).count == 1

    engine.handle_action(USER, CHANNEL, "del:MILL")
    assert engine.storage.load_cart(USER).is_empty


def test_order(engine):
    engine.handle_action(USER, CHANNEL, "add:HOOP")
    engine.handle_action(USER, CHANNEL, "confirm_order")
    assert engine.storage.orders_of(USER) == [], "без согласия заказ не создаётся"

    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    for value in ("Детский сад 5", "Петров", "+7 916 000-00-00", "-", "Казань", "-"):
        engine.handle_text(USER, CHANNEL, value)
    engine.handle_action(USER, CHANNEL, "confirm_order")

    orders = engine.storage.orders_of(USER)
    assert len(orders) == 1
    assert orders[0].status == "sent"
    assert [item.sku_1c for item in orders[0].items] == ["HOOP"]
    assert orders[0].total == 245
    assert engine.storage.load_cart(USER).is_empty


# --- Каналы ---------------------------------------------------------------------


def test_existing_telegram(engine):
    pytest.importorskip("aiogram")
    from adapters.telegram.bot import render_list_item, to_markup

    responses = engine.handle_text(USER, CHANNEL, "2.20.63")
    card = next(item for item in responses if isinstance(item, ProductList)).cards[0]

    text = render_list_item(card)
    assert "Фрезерный станок с ЧПУ" in text
    assert "253 000 ₽" in text
    assert "2.20.63" in text
    markup = to_markup(card.keyboard)
    assert "add:MILL" in [button.callback_data for row in markup.inline_keyboard for button in row]


def test_existing_web(engine, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from web import app as web_app

    monkeypatch.setattr(web_app, "build_engine", lambda _settings, **_kw: engine)
    client = TestClient(web_app.create_app(engine.settings))

    session_id = client.post("/widget/session").json()["session_id"]
    found = client.post(
        "/widget/message", json={"session_id": session_id, "text": "нужен обруч для детского сада"}
    ).json()
    assert "list" in [item["type"] for item in found["responses"]]

    client.post("/widget/action", json={"session_id": session_id, "action": "add:HOOP"})
    reopened = client.post("/widget/session", json={"session_id": session_id}).json()
    assert "order" in [item["type"] for item in reopened["responses"]]
