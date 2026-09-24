"""Регрессии живого прогона 23.09 (`data/qa/live-0923-manual`, 25 сценариев).

Каждый тест — находка прогона: составная реплика, потерянный субъект экспорта,
имена файлов китов 838, шаблонные хвосты, карточки мимо списка, возраст.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from adapters.telegram.bot import send
from agent.agent import SalesAgent as _SalesAgent
from agent.agent import _about_deadline_only, _without_codes
from agent.client import ChatClient, LLMRateLimitError
from agent.providers import _pause_for
from agent.routing import _also_asks_other, _asks_export
from agent.tools import ToolBox
from catalog.models import Product
from catalog.search import CatalogIndex, SearchQuery
from core import exports
from core.ui import Message, ProductCard
from norms.items import ItemIndex, NormItem
from procurement.discovery import query_from_text
from test_agent import (  # noqa: F401 — engine: фикстура
    CHANNEL,
    USER,
    FakeCloudRu,
    answer,
    attach,
    client,
    engine,
)


def texts(replies) -> str:  # noqa: ANN001
    return "\n".join(reply.text for reply in replies if isinstance(reply, Message))


def actions(replies) -> list[str]:  # noqa: ANN001
    return [
        button.action
        for reply in replies
        for row in (getattr(reply, "keyboard", None).rows if getattr(reply, "keyboard", None) else [])
        for button in row
    ]


def kit() -> dict:
    """Комплектация раздела 2.20 приказа 838 — как её оставляет в профиле консультант."""
    return {
        "document": "order_838",
        "code": "2.20",
        "title": "Кабинет технологии",
        "positions": [
            {"code": "2.20.1", "title": "Станок фрезерный", "quantity": "1 шт."},
            {"code": "2.20.2", "title": "Верстак", "quantity": "2 шт."},
        ],
    }


# --- Пакет A: составная реплика доезжает целиком ---------------------------------------------


TURN9 = "Тогда давайте ваши. Пришлите спецификацию Excel, счёт и срок поставки"


def test_excel_invoice_deadline_in_one_turn(engine):  # noqa: F811
    """Ход 9 из десяти диалогов: «Excel + счёт + срок» отвечал только файлом, срок — после повтора."""
    engine.session(USER, CHANNEL).profile.remember_kit(kit())
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, TURN9)
    said = texts(replies)
    assert "Собираю файл в Excel" in said, "файл должен уйти сразу — формат назван"
    assert "менеджер" in said.lower(), "срок отвечает менеджер — нота в том же ходу"
    assert "Счёт на оплату" in said, "счёт из той же реплики не должен выпадать"
    assert "export:xlsx" in actions(replies) and "manager" in actions(replies)
    assert not cloud.requests, "составная реплика файла и срока не стоит вызова модели"


def test_file_and_invoice_without_deadline(engine):  # noqa: F811
    """«Пришлите файл и счёт» без слова «срок»: счёт всё равно отвечен нотой."""
    engine.session(USER, CHANNEL).profile.remember_kit(kit())
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, "Пришлите спецификацию Excel и счёт")
    said = texts(replies)
    assert "Собираю файл в Excel" in said
    assert "Счёт на оплату" in said
    assert "export:xlsx" in actions(replies)
    assert not cloud.requests


def test_goods_and_file_both_answered(engine):  # noqa: F811
    """Сц. 5/9: «добавьте … и пришлите файл» — экспорт забирал реплику, добавление терялось."""
    engine.session(USER, CHANNEL).profile.remember_kit(kit())
    with FakeCloudRu([answer("Коврики: в наличии три расцветки.")]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, "Добавьте коврики, посмотрите что есть, и пришлите файл")

    said = texts(replies)
    assert "Коврики" in said, "подбор не должен пропасть за файлом"
    assert "файлом" in said and "В каком виде?" in said, "файл — префиксом того же хода"


def test_goods_and_deadline_both_answered(engine):  # noqa: F811
    """Сц. 7: «цвет берёза. срок поставки» — цвет терялся, отвечал только шаблон срока."""
    engine.session(USER, CHANNEL).profile.remember_kit(kit())
    with FakeCloudRu([answer("Цвет берёза есть у трёх моделей станков.")]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, "Цвет берёза. Какой срок поставки?")

    said = texts(replies)
    assert "берёза" in said.lower(), "ответ о товаре не должен съедаться шаблоном срока"
    assert "менеджер" in said.lower(), "срок отвечает менеджер — нота в конце"


def test_pure_deadline_still_answers_by_template(engine):  # noqa: F811
    """Чистый вопрос о сроке отвечает шаблоном ядра, без вызова модели."""
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, "Какой срок поставки?")

    said = texts(replies)
    assert "менеджер" in said.lower()
    assert "manager" in actions(replies), "шаблон зовёт нажать кнопку — кнопка должна быть"
    assert engine.session(USER, CHANNEL).route.get("fallback") == "deadline"
    assert not cloud.requests, "шаблон срока не должен стоить вызова модели"


@pytest.mark.parametrize(
    ("said", "only"),
    [
        ("Какой срок поставки?", True),
        ("хорошо, скачала. а сколько по времени от оформления до счёта?", True),
        ("Спасибо! Когда будет отгрузка?", True),
        ("Нужно успеть до 10 декабря. Возрасты групп уточните, что обязательно сейчас?", False),
        ("Цвет берёза. Срок поставки какой?", False),
        ("Пришлите единый стандарт комплектации. И график поставок по этапам?", False),
    ],
)
def test_deadline_dominance(said: str, only: bool):
    assert _about_deadline_only(said) is only


@pytest.mark.parametrize(
    ("said", "export", "other"),
    [
        (TURN9, True, False),
        ("сохраните спецификацию в Excel", True, False),
        ("Добавьте коврики, посмотрите что есть, и пришлите файл", True, True),
        ("Оформите заказ и пришлите файл спецификации", True, True),
    ],
)
def test_export_hijack_guard(said: str, export: bool, other: bool):
    """Экспорт забирает реплику целиком только когда в ней нет других просьб."""
    assert _asks_export(said) is export
    assert _also_asks_other(said) is other


# --- Пакет B: субъект экспорта живёт и после обычного поиска ----------------------------------


def test_export_after_plain_search(engine):  # noqa: F811
    """Сц. 4/10/14/19/22: после диалога-подбора кнопка «Скачать» говорила «Сохранять пока нечего»."""
    session = engine.session(USER, CHANNEL)
    session.profile.remember_offered(["S1"])
    assert exports.ready(session), "показанные позиции — уже субъект для файла"
    file = exports.build(engine, session, exports.EXCEL)
    assert file is not None
    assert file.filename.startswith("Подобранные позиции")
    assert len(file.content) > 1000


def test_kit_survives_later_search_turn(engine):  # noqa: F811
    """Сц. 22: показанный кит не должен терять приоритет перед показанным товаром."""
    session = engine.session(USER, CHANNEL)
    session.profile.remember_kit(kit())
    session.profile.remember_offered(["S1"])
    assert exports._subject(session).startswith("комплектацию"), "кит остаётся субъектом файла"


def test_single_kit_registered_without_code_in_answer(engine):  # noqa: F811
    """Ответ без кода раздела не теряет единственную комплектацию хода (сц. 22)."""
    tools = ToolBox(engine, engine.session(USER, CHANNEL))
    section = kit()
    tools.kits[f"{section['document']}:{section['code']}"] = section
    assert tools.single_kit() == section
    assert tools.kit_for("Вот состав кабинета технологии, файл по кнопке.") is None
    tools.kits["order_838:2.14"] = {**section, "code": "2.14"}
    assert tools.single_kit() is None, "разделов несколько без кода в ответе — файл не угадываем"


# --- Пакет C: имена файлов китов 838 — имя раздела, а не текст позиции -----------------------


def _with_norms(dialog):  # noqa: ANN001
    dialog.norm_texts = ItemIndex(
        {
            "order_838": {
                "2.14": NormItem(
                    "order_838",
                    "2.14",
                    "Стул ученический, регулируемый по высоте, рост 1-4 (далее — Стул ученический)",
                    section="Подраздел 14. Мебель ученическая",
                ),
                "2.14.1": NormItem("order_838", "2.14.1", "Стул ученический, рост 1", section="Подраздел 14. Мебель ученическая"),
            },
            "order_1057": {
                "1.14": NormItem("order_1057", "1.14", "Групповые помещения"),
                "1.14.2.2.1": NormItem("order_1057", "1.14.2.2.1", "Шкаф для раздевальной"),
            },
        }
    )
    return dialog


def test_838_kit_file_named_by_section(engine):  # noqa: F811
    """Сц. 11/12/18/21/29: файл кита назывался текстом позиции («Комплектация_2_14_Стул_ученический…»)."""
    engine = _with_norms(engine)
    tools = ToolBox(engine, engine.session(USER, CHANNEL))
    brief = tools._item_brief(engine.norm_texts.get("order_838", "2.14"), with_positions=True)
    assert "Стул ученический" in brief["title"], "модель по-прежнему видит формулировку позиции"
    assert tools.kit["title"] == "Подраздел 14. Мебель ученическая", "файл — имя раздела"
    assert [p["code"] for p in tools.kit["positions"]] == ["2.14.1"]


def test_1057_kit_file_names_unchanged(engine):  # noqa: F811
    """Короткие названия разделов 1057 остаются в имени файла как были."""
    engine = _with_norms(engine)
    tools = ToolBox(engine, engine.session(USER, CHANNEL))
    tools._item_brief(engine.norm_texts.get("order_1057", "1.14"), with_positions=True)
    assert tools.kit["title"] == "Групповые помещения"


def test_long_kit_filename_cuts_at_word_boundary(engine):  # noqa: F811
    """Сц. 29: имя резалось посреди склейки — «…по_высотестул_у.xlsx»."""
    session = engine.session(USER, CHANNEL)
    long_title = (
        "Стул ученический, регулируемый по высоте, с полкой для книг и крючком для портфеля, "
        "окраска светлых тонов, комплект поставки без сборки"
    )
    session.profile.remember_kit({**kit(), "title": long_title})
    file = exports.build(engine, session, exports.EXCEL)
    assert file is not None
    stem = file.filename.rsplit(".", 1)[0]
    assert len(stem) <= 60
    assert not stem.endswith((" ", ",", ";", "-")), "обрез не оставляет мусор"
    assert stem.count(" ") >= 3, "слова не склеиваются в одно"


# --- Пакет D: текст без поломок и честное «не найдено» по артикулу ----------------------------


def test_code_removal_leaves_no_empty_quotes_or_glued_words():
    """Сц. 50: «По вашему запросу «» … напрямуюни одна из них не содержит»."""
    said = (
        "По вашему запросу «артикул 12345» нашлось несколько позиций, "
        "но напрямую артикул 12345 ни одна из них не содержит"
    )
    clean = _without_codes(said)
    assert "«»" not in clean and "„“" not in clean, "пустых кавычек не остаётся"
    assert "напрямуюни" not in clean, "слова не склеиваются"
    assert "напрямую ни одна" in clean
    assert "12345" not in clean, "код 1С человеку не показываем"


def _mini_index() -> CatalogIndex:
    return CatalogIndex(
        [
            Product.from_dict(
                {
                    "sku_1c": "777",
                    "name": "Панель Солнечная система",
                    "price": 1500,
                    "currency": "RUB",
                    "in_stock": 5,
                    "category_paths": [["КАТАЛОГ", "Развитие речи"]],
                    "description": "",
                    "kit_contents": [],
                    "norms": [],
                }
            ),
            Product.from_dict(
                {
                    "sku_1c": "12345",
                    "name": "Мольберт двухсторонний",
                    "price": 4300,
                    "currency": "RUB",
                    "in_stock": 2,
                    "category_paths": [["КАТАЛОГ", "ИЗО"]],
                    "description": "",
                    "kit_contents": [],
                    "norms": [],
                }
            ),
        ]
    )


def test_unknown_article_returns_nothing_instead_of_random_goods():
    """Сц. 50: несуществующий артикул показывал три случайных товара вместо «не найдено»."""
    index = _mini_index()
    assert index.search(SearchQuery(text="артикул 98765")) == []
    found = index.search(SearchQuery(text="артикул 12345"))
    assert [hit.product.sku_1c for hit in found] == ["12345"], "точное совпадение — только оно"


def test_article_survives_query_from_text():
    """Раньше query_from_text выбрасывал цифры — «артикул 12345» искался как «артикул»."""
    assert "12345" in query_from_text("Есть ли в наличии артикул 12345?")
    assert query_from_text("нужны мячи для спортзала, дети 3-4 лет") == "мячи"


# --- Пакет E: карточки соответствуют списку в ответе ------------------------------------------


def _product(sku: str, name: str) -> Product:
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "price": 1000,
            "currency": "RUB",
            "in_stock": 1,
            "category_paths": [["КАТАЛОГ", "Разное"]],
            "description": "",
            "kit_contents": [],
            "norms": [],
        }
    )


def _cards_agent(names: dict[str, str]):  # noqa: ANN001
    """Агент-заглушка: _mentioned_skus/_section_products нужен только index.get."""
    products = {sku: _product(sku, name) for sku, name in names.items()}
    fake_index = SimpleNamespace(get=products.get)
    return SimpleNamespace(engine=SimpleNamespace(index=fake_index))


SECTION_GOODS = {
    "A1": "1.14.5.1.1 Ковёр детский",
    "A2": "1.14.5.1.2 Стол воспитателя",
    "A3": "1.14.5.1.3 Стул детский",
    "A4": "1.14.2.2.3 Ящик для игрушек Моби",
}


def _tools_with(dialog, skus):  # noqa: ANN001
    tools = ToolBox(dialog, dialog.session(USER, CHANNEL))
    tools.shown_skus = list(skus)
    return tools


def test_cards_follow_cited_points_not_section(engine):  # noqa: F811
    """Сц. 1/7/12: под списком пунктов 1.14.5.1.1-3 приходила карточка ящика 1.14.2.2.3."""
    agent = _cards_agent(SECTION_GOODS)
    tools = _tools_with(engine, SECTION_GOODS)
    said = "Первые три пункта раздела — 1.14.5.1.1, 1.14.5.1.2 и 1.14.5.1.3."
    got = _SalesAgent._section_products(agent, tools, said)
    assert [p.sku_1c for p in got] == ["A1", "A2", "A3"], "ровно названные пункты"


def test_named_goods_outside_cited_points_get_no_card(engine):  # noqa: F811
    """«Ящик для игрушек вне этой тройки» — карточка ящика не приходит."""
    agent = _cards_agent(SECTION_GOODS)
    tools = _tools_with(engine, SECTION_GOODS)
    said = (
        "Первые три пункта — 1.14.5.1.1 ковёр, 1.14.5.1.2 стол и 1.14.5.1.3 стул. "
        "Ящик для игрушек в тройку не входит."
    )
    got = _SalesAgent._mentioned_skus(agent, tools, said)
    assert "A4" not in got, "товар с пунктом вне названных не показываем"


def test_supplier_article_in_answer_picks_that_product(engine):  # noqa: F811
    """Сц. 12: «первые три: EKUD 0335, 0321/1Т, 0420» — карточки должны быть из списка."""
    goods = {
        "A5": "EKUD 0335 Лесенка-4",
        "A6": "EKUD 0306 Зеркало",
        "A1": "1.14.5.1.1 Ковёр детский",
    }
    agent = _cards_agent(goods)
    tools = _tools_with(engine, goods)
    said = "Из показанного возьмём первые три: EKUD 0335, 0321/1Т и 0420."
    got = _SalesAgent._mentioned_skus(agent, tools, said)
    assert got == ["A5"], "артикул из ответа — только этот товар, не первые показанные"


# --- Пакет F: провайдер под лимитом и доставка без потерь --------------------------------------


class ScriptedServer:
    """POST-сервер с заданным сценарием ответов: (код, JSON, заголовки)."""

    def __init__(self, script: list[tuple[int, dict, dict[str, str]]]) -> None:
        self.script = script
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(json.loads(self.rfile.read(length) or b"{}"))
                code, data, headers = outer.script[min(len(outer.requests) - 1, len(outer.script) - 1)]
                payload = json.dumps(data).encode()
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> ScriptedServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"


_OK = (200, {"choices": [{"message": {"role": "assistant", "content": "готово"}}]}, {})
_429 = (429, {"error": {"message": "ModelArts.81101 rate limit"}}, {"Retry-After": "0"})


def test_rate_limit_gets_one_quick_retry():
    """Сц. 5: один 429 раньше сажал провайдера на 5 минут — теперь быстрый повтор."""
    with ScriptedServer([_429, _OK]) as server:
        got = ChatClient(api_key="k", base_url=server.base_url, timeout=10).complete(
            [{"role": "user", "content": "привет"}]
        )
    assert got["content"] == "готово"
    assert len(server.requests) == 2, "после 429 — ровно один повтор"


def test_rate_limit_after_retry_is_a_short_cooldown():
    """Два 429 подряд — короткий cooldown с джиттером, не пять минут."""
    with ScriptedServer([_429, _429]) as server:
        client = ChatClient(api_key="k", base_url=server.base_url, timeout=10)
        with pytest.raises(LLMRateLimitError) as caught:
            client.complete([{"role": "user", "content": "привет"}])
    pause = _pause_for(caught.value)
    assert 60 * 0.8 <= pause <= 60 * 1.2, f"cooldown для 429 — около минуты, получили {pause:.0f} с"


class _NoPhotoBot:
    """Телеграм, у которого падает каждое фото, а текст доходит."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):  # noqa: ANN001
        raise TelegramBadRequest(method="sendPhoto", message="failed to get HTTP URL content")

    async def send_message(self, chat_id, text, reply_markup=None):  # noqa: ANN001
        self.messages.append(text)


def test_failed_photo_does_not_drop_the_rest_of_the_turn():
    """Сц. 23.09: упавшее фото гасило остальные карточки хода — текст приходил, карточки нет."""
    bot = _NoPhotoBot()
    card = ProductCard(product=_product("S9", "1.5.1.41 Мяч гимнастический"), image="https://vdm.ru/p.jpg")
    asyncio.run(send(bot, 1, [card, Message("Что дальше?")]))

    assert any("Мяч гимнастический" in text for text in bot.messages), "карточка дошла текстом"
    assert any("Что дальше?" in text for text in bot.messages), "остальные ответы хода доставлены"
