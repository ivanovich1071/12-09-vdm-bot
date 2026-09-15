"""Регрессии ночного автотеста 14–15.09 (`data/qa/night-1`) и утреннего заказа файлом 15.09.

Что сломалось и что здесь проверяется:
- «Связаться с менеджером» под ответом модели вела в меню (сц. 17, 29, 38);
- «Скачать Excel» отдавал последний разобранный раздел, а не тот, о котором текст (сц. 24: стулья вместо технопарка);
- пока бот ждал контакт, «Начать заново» читалось как имя, и сценарий 29 начался с «Не вижу телефона»;
- свой же файл «Подобранные позиции» без количества ушёл менеджеру предзаказом на 0 ₽, а на «сформируй
  предзаказ» и «все найденные по 1 шт.» модель трижды пересобрала строки файла по-разному;
- выгрузка списка была без количества и обратно читалась как «не указано количество».
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapters.telegram.gateway import ContactRequest, TelegramGateway
from agent import verify
from agent.agent import SalesAgent, _short_kit_answer
from agent.routing import CONSULT
from agent.tools import ToolBox
from core import intent
from core_fixtures import item as norm_item
from ingest.xlsx_reader import XlsxFile
from norms.items import ItemIndex
from test_agent import (  # noqa: F401 — engine: фикстура
    CHANNEL,
    USER,
    FakeCloudRu,
    answer,
    attach,
    client,
    engine,
)
from test_core_api import build
from test_live_0914 import _docx_with_lines
from test_order_core import HEADER, xlsx
from test_order_followup import ORDER_LINES

NO_QUANTITY = [
    HEADER,
    ["1", "B1", "Мяч баскетбольный № 3", "", "908"],
    ["2", "B2", "Мат детский", "", "8164"],
    ["3", "", "Телескоп космический", "", ""],
]


@pytest.fixture
def env(tmp_path):
    api = build(tmp_path)
    return api, TelegramGateway(api.core, 20 * 1024 * 1024)


def actions(reply) -> list[str]:
    keyboard = getattr(reply, "keyboard", None)
    return [button.action for row in (keyboard.rows if keyboard else []) for button in row]


def _preorder_waiting_for_contact(api, gateway):
    api.storage.record_consent(USER, "telegram", "test", "granted")
    [evaluation] = gateway.upload(USER, "заказ.xlsx", xlsx([HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "908"]]))
    _, ask = gateway.action(USER, next(a for a in actions(evaluation) if a.startswith("po_order:")))
    assert isinstance(ask, ContactRequest)


# --- Менеджер и «Начать заново» ------------------------------------------------------------------


def test_manager_button_under_the_answer_leads_to_the_manager():
    tools = ToolBox(None, None)
    tools.handoff_reason = "сроки поставки"
    keyboard = SalesAgent._keyboard(SimpleNamespace(engine=None), None, tools, SimpleNamespace(branch=CONSULT, sells=False))
    assert actions(SimpleNamespace(keyboard=keyboard)) == ["manager"]


def test_restart_from_the_keyboard_leaves_the_contact_wait(env):
    api, gateway = env
    _preorder_waiting_for_contact(api, gateway)

    [confirm] = gateway.text(USER, "Начать заново")

    assert "Начать заново?" in confirm.text and "restart_yes" in actions(confirm)
    assert USER not in gateway._awaiting_contact and api.notifier.sent == []


def test_second_message_without_a_phone_is_answered_and_the_preorder_waits(env):
    api, gateway = env
    _preorder_waiting_for_contact(api, gateway)

    [again] = gateway.text(USER, "а сроки поставки какие?")
    note, *answered = gateway.text(USER, "Школа, кабинет информатики")

    assert isinstance(again, ContactRequest)
    assert "без телефона менеджеру не ушёл" in note.text and answered
    assert USER not in gateway._awaiting_contact and api.notifier.sent == []


def test_preorders_of_the_test_account_do_not_reach_the_manager(env):
    api, gateway = env
    api.core.services.preorders.test_owners = frozenset({USER})
    _preorder_waiting_for_contact(api, gateway)

    [done] = gateway.text(USER, "Иван Тестов, +7 900 111-22-33")

    assert "передан менеджеру" in done.text and api.notifier.sent == []


# --- Заказ файлом: корзина вместо предзаказа на 0 ₽ ----------------------------------------------


def test_file_without_quantities_offers_the_cart_not_a_zero_preorder(env):
    api, gateway = env
    [reply] = gateway.upload(USER, "Подобранные позиции.xlsx", xlsx(NO_QUANTITY))

    assert "order_cart:1" in actions(reply) and not any(a.startswith("po_order:") for a in actions(reply))
    assert "Количество указано не у всех позиций" in reply.text

    [added] = gateway.action(USER, "order_cart:1")
    gateway.action(USER, "order_cart:1")

    assert "Положил в корзину 2 позиции" in added.text and "Не нашлось в каталоге: 1 строка" in added.text
    assert {"cart", "checkout"} <= set(actions(added))
    assert api.storage.load_cart(USER).count == 2, "повторное нажатие не удваивает количество"


def test_checkout_words_after_the_file_are_handled_by_the_core_not_the_model(env):
    api, gateway = env
    gateway.upload(USER, "Подобранные позиции.xlsx", xlsx(NO_QUANTITY))
    with FakeCloudRu([answer("Какие позиции добавляем и по сколько?")] * 6) as cloud:
        attach(api.engine, client(cloud.base_url))
        ask = gateway.text(USER, "подобрал позиции, сформируй предзаказ")[0]
        route = dict(api.engine.session(USER, "telegram").route)
        added = gateway.text(USER, "все найденные позиции  по 1шт")[0]

    assert route["fallback"] == "order_cart"
    assert "не указано количество" in ask.text and "order_cart:1" in actions(ask)
    assert "Положил в корзину 2 позиции" in added.text and "по 1 шт." in added.text
    assert api.storage.load_cart(USER).count == 2 and "checkout" in actions(added)


def test_checkout_words_with_quantities_in_the_file_offer_the_whole_file_preorder(env, tmp_path):
    api, gateway = env
    path = _docx_with_lines(tmp_path / "Заказ на Элтик.docx", ORDER_LINES)
    gateway.upload(USER, path.name, path.read_bytes())
    with FakeCloudRu([answer("Какие позиции добавляем?")] * 6) as cloud:
        attach(api.engine, client(cloud.base_url))
        ready = gateway.text(USER, "оформи предзаказ")[0]

    assert "всё готово к предзаказу: в каталоге 3 из 4 строк" in ready.text
    assert any(action.startswith("po_order:") for action in actions(ready))
    assert api.storage.load_cart(USER).is_empty


def test_exported_list_carries_quantities_and_reads_back_as_an_order(env, tmp_path):
    api, gateway = env
    content = xlsx([HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "908"], ["2", "B2", "Мат детский", "1", "8164"]])
    gateway.upload(USER, "заказ.xlsx", content)
    api.engine.order_list(api.engine.session(USER, "telegram"), "", None)

    excel = gateway.action(USER, "export:xlsx")[0]
    path = tmp_path / excel.filename
    path.write_bytes(excel.content)
    cells = [str(value) for row in XlsxFile(path).rows() for value in row.values()]
    [again] = gateway.upload(USER, excel.filename, excel.content)

    assert "Кол-во" in cells and any("файл сформирован" in cell for cell in cells)
    assert "не указано количество" not in again.text
    assert any(action.startswith("po_order:") for action in actions(again))


@pytest.mark.parametrize(
    "text, quantity",
    [
        ("все найденные позиции  по 1шт", 1),
        ("все по 2", 2),
        ("по 3 штуки каждой", 3),
        ("Все 14 найденных — по 1 штуке, как вы просили. Нажмите «Оформить»", 1),
        ("оформи предзаказ по приказу 1057", None),
    ],
)
def test_checkout_and_quantity_are_read_from_the_words(text, quantity):
    assert intent.asks_order_checkout(text)
    assert intent.each_quantity(text) == quantity


def test_ordinary_words_are_not_a_checkout():
    assert not intent.asks_order_checkout("подбери мячи по приказу 1057")
    assert not intent.asks_order_checkout("а сроки какие?")


# --- Файл комплектации = раздел из текста ----------------------------------------------------------


def test_file_follows_the_section_named_in_the_answer():
    tools = ToolBox(None, None)
    tools.kits = {
        "order_838:2.14": {"document": "order_838", "code": "2.14", "title": "Стул ученический", "positions": []},
        "order_838:2.20": {"document": "order_838", "code": "2.20", "title": "Кабинет технологии", "positions": []},
        "order_1057:1.14.7": {"document": "order_1057", "code": "1.14.7", "title": "Группы 5–6 лет", "positions": []},
    }

    technopark = "Основание: приказ № 838 от 28.11.2024\n1. Робототехника\n- 2.20.153 — набор"
    assert tools.kit_for(technopark)["code"] == "2.20"
    assert tools.kit_for("Раздел 1.14.7 «Групповые помещения для детей 5–6 лет», приказ № 1057")["code"] == "1.14.7"
    assert tools.kit_for("Раздел 2.12 «Словари» — 15 позиций") is None
    assert tools.kit_for("Приказ № 1057 от 25.12.2024") is None


# --- Защита ответа модели --------------------------------------------------------------------------

REGISTRY = ItemIndex(
    {
        "order_838": {
            entry.code: entry
            for entry in (
                norm_item("order_838", "2.12", "Словари, справочники, энциклопедия (по предметной области)"),
                norm_item("order_838", "2.12.2", "Словарь толковый"),
            )
        },
        "order_1057": {
            entry.code: entry
            for entry in (
                norm_item("order_1057", "1.14", "Групповые помещения"),
                norm_item("order_1057", "1.14.3", "Групповые помещения для детей 1 - 2 лет"),
                norm_item("order_1057", "1.14.7", "Групповые помещения для детей 5 - 6 лет"),
                norm_item("order_1057", "1.14.7.1.1", "Аптечка универсальная"),
            )
        },
    }
)


def _checker(**engine_fields) -> SalesAgent:
    agent = SalesAgent.__new__(SalesAgent)
    agent.engine = SimpleNamespace(**engine_fields)
    return agent


def test_foreign_words_are_found_and_latin_brands_are_not():
    assert verify.foreign_script("полоса должна быть不大, лёгкая; WeDo 2.0") == {"不大"}
    assert verify.foreign_script("Набор «Винтики и гаечки» MIN 45303") == set()


def test_foreign_words_are_sent_back_for_a_rewrite(engine):  # noqa: F811
    script = [answer("Полоса должна быть不大 и лёгкой."), answer("Полоса должна быть небольшой и лёгкой.")]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "нужен станок")

    assert "небольшой" in responses[0].text and not verify.foreign_script(responses[0].text)
    assert "не на русском" in cloud.requests[-1]["messages"][-1]["content"]


def test_listed_codes_take_list_lines_and_sections_but_not_dates():
    text = (
        "Основание: приказ № 838 от 28.11.2024, раздел 2.12 «Кабинет изобразительного искусства»\n"
        "1. Мебель\n"
        "2.12.2 — Мольберт/этюдник — 1 шт.\n"
        "- 1.14.5.7.1.39 Кубики — развитие"
    )
    assert verify.listed_codes(text) == [
        ("2.12", "Кабинет изобразительного искусства"),
        ("2.12.2", "Мольберт/этюдник"),
        ("1.14.5.7.1.39", "Кубики"),
    ]


def test_titles_may_be_shortened_but_not_replaced():
    assert verify.title_matches("Гимнастическая стенка (шведская стенка)", ["Гимнастическая стенка"])
    assert verify.title_matches("Словари и справочники", ["Словари, справочники, энциклопедия (по предметной области)"])
    assert not verify.title_matches("Кабинет изобразительного искусства", ["Словари, справочники, энциклопедия"])


def test_sections_and_points_are_checked_against_the_orders():
    agent = _checker(norm_texts=REGISTRY)
    wrong = (
        "Основание: приказ № 838, раздел 2.12 «Кабинет изобразительного искусства»\n"
        "2.12.2 — Мольберт/этюдник — 1 шт.\n"
        "- 1.14.5.7.1.39 Кубики"
    )

    complaint = agent._complaint(wrong, set(), set())
    kept = verify.without_unverified(wrong, set(), set(), agent._registry_problems(wrong)[1])

    assert "1.14.5.7.1.39" in complaint and "«Словарь толковый»" in complaint and "«Словари, справочники" in complaint
    assert "Мольберт" not in kept and "Кубики" not in kept
    assert agent._complaint("Раздел 2.12 «Словари и справочники»", set(), set()) == ""


def test_group_section_for_another_age_is_sent_back():
    agent = _checker(norm_texts=REGISTRY)
    session = SimpleNamespace(profile=SimpleNamespace(age="5–6 лет"))
    wrong = "Основание: приказ № 1057, раздел 1.14.3 «Групповые помещения для детей 1 - 2 лет»"
    right = (
        "Основание: приказ № 1057, раздел 1.14.7 «Групповые помещения для детей 5 - 6 лет»\n"
        "1.14.7.1.1 Аптечка универсальная — 1 шт."
    )

    complaint = agent._complaint(wrong, set(), set(), session)

    assert "1.14.3 (для детей 1–2 лет)" in complaint and "5–6 лет" in complaint
    assert agent._complaint(right, set(), set(), session) == ""


def test_card_goes_only_to_the_line_with_its_own_article():
    products = {
        "S214": SimpleNamespace(name="ПОН Звонкий-глухой. Игра - лото (Д-214) настольно-печатная игра"),
        "S222": SimpleNamespace(name="ПОН Логопедическое лото (Д-222) настольно-печатная игра"),
    }
    agent = _checker(index=SimpleNamespace(get=products.get))
    tools = SimpleNamespace(shown_skus=["S214", "S222"])
    listed = (
        "13. ПОН Звонкий-глухой. Игра - лото (Д-214) настольно-печатная игра — 205 ₽\n"
        "14. Логопедические картинки для автоматизации звука «Л» — 303 ₽"
    )

    assert agent._mentioned_skus(tools, listed) == ["S214"]
    assert agent._mentioned_skus(tools, "Звонкий-глухой, игра-лото — 205 ₽") == ["S214"]


def test_long_kit_answer_is_cut_to_the_file_with_the_question_kept():
    lines = [f"1.14.7.1.{number} Позиция перечня номер {number} — 1 шт. — назначение" for number in range(1, 120)]
    long = "Предварительная комплектация\n" + "\n".join(lines) + "\n\nС какой позиции начнём подбор?"

    short = _short_kit_answer(long)

    assert len(short) <= 2500 and "Полный список — в файле" in short
    assert short.endswith("С какой позиции начнём подбор?")
    assert _short_kit_answer("Коротко.") == "Коротко."
