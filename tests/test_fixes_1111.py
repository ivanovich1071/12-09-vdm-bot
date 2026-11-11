"""Исправления по замечаниям заказчика 06.10–10.11 («мои замечания.md»).

З1 — корзина пуста после принятого заказа; З2 — подпись к файлу управляет файлом;
З3 — кнопка «по 1 шт.» честна к количеству; З4 — «из наличия» фильтрует и говорит
цифры; З5 — строки-заголовки секций не считаются позициями; З6 — классификатору
хватает обрезанной истории; З7 — таймаут по вине бюджета не выключает провайдера;
З8 — «собери заказ из первых N позиций» исполняется кодом.
"""

import pytest

from agent.agent import SalesAgent as Agent
from agent.client import ChatClient, LLMBudgetError, LLMError, LLMTimeoutError
from agent.providers import STARVED_COOLDOWN_SECONDS, LLMRouter
from agent.routing import _history
from catalog.runtime import CatalogRuntime
from catalog.search import CatalogIndex
from core import intent
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from core_fixtures import procurement_service, products, state
from order_import.normalizer import OrderNormalizer
from order_import.parsers import RawDocument, RawRow, RawTable, WordOrderParser
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"
USER = "u-fix"
CONTACTS = ("Школа 1", "Иванов", "+7 916 330-02-79", "-", "Москва", "-")

FILE = "Комплектация_кабинет.docx"


@pytest.fixture
def engine(tmp_path):
    storage = Storage(tmp_path / "fix.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
    engine = DialogEngine(CatalogIndex(products()), storage, orders, settings)
    engine.procurement = procurement_service(tmp_path, CatalogRuntime(state()))
    return engine


def with_file(engine, positions):
    """Проверенный заказ в профиле — как после gateway.upload → note_dialog."""
    engine.session(USER, CHANNEL).profile.order = {"file": FILE, "positions": positions}


# --- З1: корзина после принятого заказа -------------------------------------------------------


def test_accepted_order_empties_the_cart(engine):
    """06.10: спортзальный заказ «дожил» в корзине, файл кабинета лёг поверх него."""
    engine.handle_action(USER, CHANNEL, "add:B2")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    for value in CONTACTS:
        engine.handle_text(USER, CHANNEL, value)
    [done] = engine.handle_action(USER, CHANNEL, "confirm_order")

    assert "принят" in done.text
    assert engine.storage.load_cart(USER).is_empty


# --- З2: подпись к файлу — параметры файла, а не старых карточек -------------------------------


def test_caption_controls_the_file_not_old_cards(engine):
    """06.10: «по этому списку…» под файлом положила в корзину 9 показанных спорттоваров."""
    with_file(engine, [{"sku": "B2", "quantity": 5}, {"sku": "B3", "quantity": 2}])
    engine.session(USER, CHANNEL).profile.offered = ["B1"]

    [reply] = engine.handle_caption(USER, CHANNEL, "по этому списку сформируй все позиции по 1шт")

    cart = engine.storage.load_cart(USER)
    assert {item.sku_1c for item in cart.items} == {"B2", "B3"}, "показанный B1 не должен попасть"
    assert all(item.quantity == 1 for item in cart.items)
    assert "по 1 шт." in reply.text


def test_caption_without_order_words_is_answered_normally(engine):
    """«Спасибо, файл получил?» под файлом — не команда корзине."""
    with_file(engine, [{"sku": "B2", "quantity": 1}])
    engine.handle_caption(USER, CHANNEL, "а наличие проверяли?")
    assert engine.storage.load_cart(USER).is_empty


# --- З3: кнопки количества ---------------------------------------------------------------------


def test_button_by_one_overrides_file_quantities(engine):
    """07.10 кнопка «по 1 шт.» клала количество из файла, где оно есть."""
    with_file(engine, [{"sku": "B2", "quantity": 5}])
    [reply] = engine.handle_action(USER, CHANNEL, "order_cart:1")

    cart = engine.storage.load_cart(USER)
    assert cart.items[0].quantity == 1
    assert "по 1 шт." in reply.text


def test_button_as_in_file_keeps_file_quantities(engine):
    with_file(engine, [{"sku": "B2", "quantity": 5}])
    [reply] = engine.handle_action(USER, CHANNEL, "order_cart_file")

    cart = engine.storage.load_cart(USER)
    assert cart.items[0].quantity == 5
    assert "количество — из файла" in reply.text


# --- З4: «из наличия» --------------------------------------------------------------------------


def test_in_stock_filter_keeps_only_available(engine):
    """07.10 логопед: в корзину легли 57 позиций при 32 в наличии — наличие не упомянуто."""
    with_file(engine, [{"sku": "B2", "quantity": 1}, {"sku": "B3", "quantity": 1}])

    [reply] = engine.handle_caption(USER, CHANNEL, "подбери из наличия все по 1 шт")

    cart = engine.storage.load_cart(USER)
    assert {item.sku_1c for item in cart.items} == {"B2"}
    assert "нет в наличии: 1" in reply.text


def test_in_stock_filter_with_nothing_available_asks(engine):
    with_file(engine, [{"sku": "B3", "quantity": 1}])

    [reply] = engine.handle_caption(USER, CHANNEL, "все позиции из наличия по 1 шт")

    assert engine.storage.load_cart(USER).is_empty
    assert "в наличии нет ни одной" in reply.text
    assert any(button.action == "order_cart_file" for button in reply.keyboard.rows[0])


def test_in_stock_words_are_not_confused_with_questions(engine):
    assert intent.asks_in_stock_only("подбери из наличия все по 1 шт")
    assert intent.asks_in_stock_only("только в наличии, остальное менеджеру")
    assert not intent.asks_in_stock_only("проверьте наличие, пожалуйста")


# --- Замечание 2: произвольный тире-список в Word ----------------------------------------------


def _docx_with_lines(path, lines):
    import zipfile

    body = "<w:br/>".join(f'<w:t xml:space="preserve">{line}</w:t>' for line in lines)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r>{body}</w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", xml)
    return path


def test_word_file_with_dash_list_gives_order_lines(tmp_path):
    """07.10 «произвольный список.docx» отвечал «позиций 0» — тире-список теперь читается."""
    path = _docx_with_lines(
        tmp_path / "произвольный список.docx",
        [
            "-Стойки для хранения ГИА-лабораторий  -3 шт;",
            "- Калориметр с набором калориметрических тел-2шт;",
            "-Термометр лабораторный -",
        ],
    )
    items, _ = OrderNormalizer().normalize(WordOrderParser().parse(path))

    assert [item.name for item in items] == [
        "Стойки для хранения ГИА-лабораторий",
        "Калориметр с набором калориметрических тел",
        "Термометр лабораторный",
    ]
    assert [item.quantity for item in items] == [3, 2, None]


def test_single_dash_line_is_not_an_order(tmp_path):
    """Один дефис в обычном письме не превращается в позицию заказа."""
    document = WordOrderParser().parse(_docx_with_lines(tmp_path / "x.docx", ["Заметки:", "- см. приказ 838"]))
    assert document.warnings and document.warnings[0].code == "NO_TABLES"


def test_dash_list_does_not_mix_with_point_list(tmp_path):
    """Есть пункты приказа — тире-строки письма не подмешиваются в заказ."""
    path = _docx_with_lines(
        tmp_path / "x.docx",
        [
            "1.5.1.7 Мат гимнастический — 2 шт.",
            "- примечание для менеджера",
            "- см. приложение",
        ],
    )
    items, _ = OrderNormalizer().normalize(WordOrderParser().parse(path))

    assert [item.norm_item for item in items] == ["1.5.1.7"]


# --- З5: строки-заголовки секций ---------------------------------------------------------------


def _order_table():
    header = (
        "№",
        "Код 1С",
        "Наименование",
        "Кол-во",
        "Ед.",
        "Цена, ₽",
        "Наличие",
        "Пункт",
        "Наименование по перечню",
        "Норма по перечню",
        "Сопоставление",
    )
    return RawTable(
        "Таблица 1",
        (
            RawRow(1, header),
            # Строка-заголовок секции: формулировка раздела есть, позиции нет.
            RawRow(2, ("", "", "", "", "", "", "", "1.13.3.1", "Рабочее место педагога", "", "")),
            RawRow(
                3,
                (
                    "1",
                    "",
                    "Накопитель для бумаг",
                    "1",
                    "шт.",
                    "по запросу",
                    "",
                    "1.13.3.1.1",
                    "Накопитель для бумаг",
                    "1 Шт.",
                    "нет в каталоге",
                ),
            ),
        ),
    )


def test_section_header_rows_are_not_positions():
    """07.10: «88 строк» и «не нашлось 29» вместо 85 позиций и 26."""
    items, _ = OrderNormalizer().normalize(RawDocument("word", (_order_table(),)))
    assert len(items) == 1
    assert items[0].name == "Накопитель для бумаг"


# --- З6: классификатор и история ---------------------------------------------------------------


def test_classify_history_is_trimmed(engine):
    session = engine.session(USER, CHANNEL)
    session.remember("assistant", "1.5.1.7 Мат гимнастический — 2 шт. " * 40)
    session.remember("user", "спасибо, длинный список " * 20)

    history = _history(session, "собери заказ из первых 29 позиций по 1 шт")

    assert all(len(item["content"]) <= 500 for item in history)


def test_classify_timeout_comes_from_settings(tmp_path):
    """Настройка llm_router_timeout_seconds раньше существовала, но никуда не была подключена."""
    storage = Storage(tmp_path / "fix2.sqlite3")
    engine = DialogEngine(
        CatalogIndex(products()),
        storage,
        OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl")),
        Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl")),
    )
    router = LLMRouter(clients=[], classify_timeout=6.0)
    agent = Agent(engine, router)
    assert agent.routing.llm is router
    assert router.classify_timeout == 6.0


# --- З7: таймаут по вине бюджета не выключает провайдера ---------------------------------------


def test_budget_error_is_distinct_from_provider_timeout():
    assert issubclass(LLMBudgetError, LLMTimeoutError)
    assert issubclass(LLMTimeoutError, LLMError)


def test_starved_timeout_gets_short_cooldown():
    client = ChatClient(api_key="k", model="m", name="t")
    router = LLMRouter(clients=[client])

    starved = LLMTimeoutError("t не ответил за 6 с")
    router.mark_down(client, starved, pause=STARVED_COOLDOWN_SECONDS)
    assert 0 < router.blocked_for(client) <= STARVED_COOLDOWN_SECONDS + 0.01

    router.mark_up(client)
    router.mark_down(client, LLMTimeoutError("t не ответил за 30 с"))
    assert router.blocked_for(client) > STARVED_COOLDOWN_SECONDS


def test_exhausted_budget_does_not_block_provider(engine):
    """10.11: qwen получил «отключён на 25 с» за чужой перерасход бюджета хода."""
    agent = Agent(engine, LLMRouter(clients=[ChatClient(api_key="k", model="m", name="t")]))

    with pytest.raises(LLMBudgetError):
        agent._call_timeout(_past_deadline(), 30.0)
    assert agent.router.available


def _past_deadline() -> float:
    import time

    return time.monotonic() - 1.0


# --- З8: «собери заказ из первых N позиций» кодом ----------------------------------------------


def test_first_n_positions_collected_without_model(engine):
    """10.11: фраза ушла в модель, модель не успела — клиент получил «консультант недоступен»."""
    session = engine.session(USER, CHANNEL)
    session.remember(
        "assistant",
        "1.5.1.7 Мат гимнастический — 2 шт.\n"
        "1.5.1.13 Доска с ребристой поверхностью — 2 шт.\n"
        "1.5.1.33 Мяч для игр — 4 шт.",
    )

    [reply] = engine.handle_text(USER, CHANNEL, "собери заказ из первых 2 позиций по 1 шт")

    cart = engine.storage.load_cart(USER)
    assert {item.sku_1c for item in cart.items} == {"B2", "B3"}
    assert all(item.quantity == 1 for item in cart.items)
    assert "из первых 2 позиций" in reply.text


def test_first_n_without_order_words_is_not_a_cart_command(engine):
    assert intent.asks_first_n("покажи первые 3 позиции") is None
    assert intent.asks_first_n("первые 29 пунктов перечня") is None
    assert intent.asks_first_n("собери заказ из первых 29 позиций по 1 шт") == 29
