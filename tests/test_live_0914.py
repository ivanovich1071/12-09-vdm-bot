"""Регрессии живого прогона в Telegram 14.09 (экспорт чата и журнал `data/dialogs/2026-09-14.jsonl`).

Что сломалось тогда и что здесь проверяется:
- консультацию заменял подбор по словам «дай консультацию» — фитбол и тактильные мячики;
- «мы открыли частный детский сад» получало перечень кабинета логопеда из прошлой задачи;
- «Артикуляционная моторика» превращалась в «, мимика»;
- «сохрани в файл» получало отказ, а docx со списком бота — «позиций 0»;
- «подбери из наличия 30 позиций и дай списком», «а ещё что есть» уходили модели;
- «1.5.1.35» становилось ссылкой, таблица приходила с «|»;
- «Начать заново» не было на нижней клавиатуре, /start стирал разговор без вопроса.
"""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest

from agent.agent import SalesAgent, _without_codes
from agent.routing import CONSULT, EXPORT_REQUEST, by_rules
from core import intent, selection
from core.dialog import Session
from core.profile import DialogProfile
from core.ui import Message, ProductList
from ingest.xlsx_reader import XlsxFile
from order_import.normalizer import OrderNormalizer
from order_import.parsers import WordOrderParser
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
from test_core_api import build

KIT = {
    "document": "order_1057",
    "code": "1.13.3",
    "title": "Кабинет учителя-логопеда",
    "positions": [
        {"code": "1.13.3.1", "title": "Рабочее место педагога", "quantity": ""},
        {"code": "1.13.3.1.1", "title": "Накопитель для бумаг на 3 отделения", "quantity": "1 шт."},
        {"code": "1.13.3.3.27", "title": "Логопедические зонды", "quantity": "1 набор"},
    ],
}


@pytest.fixture
def env(tmp_path):
    from adapters.telegram.gateway import TelegramGateway

    api = build(tmp_path)
    return api, TelegramGateway(api.core, 20 * 1024 * 1024)


# --- Консультация не подменяется выдачей каталога -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "дай консультацию, что подобрать по приказу 1057 в кабинет логопеда в детском саду",
        "общий подбор для спортивного зала детского сада",
        "для детей 3-6 лет дай список всего что нужно для спорт зала",
        "мы открыли частный детский сад. дай рекомендации по его оснащению",
    ],
)
def test_conversation_words_are_not_a_product_query(text):
    assert query_from_text(text) == ""


def test_consultant_keeps_confirmed_lines_when_one_point_is_invented(engine):  # noqa: F811
    invented = (
        "Предварительная комплектация по приказу № 838:\n"
        "1. Фрезерный станок с ЧПУ — пункт 2.20.63 приказа № 838, для уроков технологии\n"
        "2. Верстак столярный — пункт 33.1.2 приказа № 1057\n"
        "Уточните, сколько учеников в группе, — пересчитаю количество."
    )
    session = engine.session(USER, CHANNEL)
    session.norm_refs.add(("order_838", "2.20.63"))
    with FakeCloudRu([answer(invented)]) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "дай консультацию, что подобрать по приказу 838 в кабинет технологии в школе")

    assert session.route["role"] == CONSULT
    assert not any(isinstance(reply, ProductList) for reply in responses)
    text = responses[0].text
    assert "2.20.63" in text and "33.1.2" not in text
    assert "33.1.2" in session.route["discarded"]["answer"], "отвергнутый ответ виден в журнале"


def test_consultant_asks_instead_of_catalog_when_nothing_is_confirmed(engine):  # noqa: F811
    with FakeCloudRu([answer("Нужно по пункту 33.1.2 приказа № 1057 и пункту 44.2.1 приказа № 1057.")]) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "дай консультацию, что подобрать по приказу 838 в кабинет технологии в школе")

    assert len(responses) == 1 and isinstance(responses[0], Message)
    assert "раздел перечня" in responses[0].text
    assert "33.1.2" not in responses[0].text


def test_article_word_inside_a_word_survives():
    assert _without_codes("Артикуляционная моторика, мимика") == "Артикуляционная моторика, мимика"
    assert _without_codes("Мяч [артикул У733] — 900 ₽") == "Мяч — 900 ₽"


# --- Новая задача сбрасывает старую --------------------------------------------------------------


def test_whole_new_object_forgets_the_previous_room():
    profile = DialogProfile(institution="детский сад", room="кабинет логопеда", procurement_task_id="t1", kit=KIT)
    profile.offered = ["S1"]
    profile.update_from_text("мы открыли частный детский сад. дай рекомендации по его оснащению")

    assert profile.institution == "детский сад"
    assert profile.room is None and profile.procurement_task_id is None and profile.kit is None
    assert profile.offered == []


def test_named_room_in_the_same_message_is_kept():
    profile = DialogProfile(institution="детский сад", room="кабинет логопеда")
    profile.update_from_text("нужно оснастить спортзал в детском саду")
    assert profile.room == "спортивный зал"


def test_long_assistant_answers_are_shortened_for_the_model():
    session = Session(user_id="u", channel="telegram")
    session.remember("user", "кабинет логопеда")
    session.remember("assistant", "1.13.3.1.1 Накопитель — 1 шт.\n" * 200)
    history = SalesAgent._history(None, session)  # type: ignore[arg-type]
    assert len(history[-1]["content"]) < 1600


# --- Файл, список, «ещё» ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["сохрани в файл и дай скачать, потом разберусь", "выгрузи в эксель", "пришли списком в ворде"]
)
def test_export_is_its_own_intent(text):
    decision = by_rules(text, DialogProfile(last_agent="consult"))
    assert decision is not None and decision.intent == EXPORT_REQUEST and decision.branch == CONSULT


@pytest.mark.parametrize(
    "text", ["где скачать приказ 1057", "в файле нет таблицы, посмотрите", "Выгрузи весь каталог в JSON"]
)
def test_not_every_file_word_is_an_export(text):
    decision = by_rules(text, DialogProfile())
    assert decision is None or decision.intent != EXPORT_REQUEST


def test_list_size_and_more_are_recognized():
    assert intent.list_size("ты дал огромный перечень товара, подбери из наличия 30 позиций и дай списком") == 30
    assert intent.list_size("подбери 3 позиции") is None
    assert intent.asks_more("а еще что есть")
    assert intent.asks_more("покажи ещё")
    assert not intent.asks_more("а ещё нужен мяч для группы")


def test_kit_goes_to_excel_and_word(env, tmp_path):
    api, gateway = env
    profile = api.engine.session(USER, "telegram").profile
    profile.remember_kit(KIT)

    offer = api.engine.agent  # агента в сборке нет: предложение файла проверяем напрямую через модуль
    assert offer is None
    from core import exports

    replies = exports.offer(api.engine, api.engine.session(USER, "telegram"))
    actions = [button.action for row in replies[0].keyboard.rows for button in row]
    assert actions == ["export:xlsx", "export:docx"]

    excel = gateway.action(USER, "export:xlsx")[0]
    path = tmp_path / excel.filename
    path.write_bytes(excel.content)
    cells = [value for row in XlsxFile(path).rows() for value in row.values()]
    assert "1.13.3.1.1" in cells and "1 шт." in cells

    word = gateway.action(USER, "export:docx")[0]
    document = zipfile.ZipFile(io.BytesIO(word.content)).read("word/document.xml").decode("utf-8")
    assert "Логопедические зонды" in document


def test_nothing_to_export_is_said_plainly(env):
    _, gateway = env
    reply = gateway.action(USER, "export:xlsx")[0]
    assert isinstance(reply, Message) and "нечего" in reply.text


def test_shortlist_is_one_message_with_file_and_cart_buttons(env):
    api, gateway = env
    gateway.text(USER, "Школа, кабинет информатики")
    session = api.engine.session(USER, "telegram")
    replies = api.engine.shortlist(session, "дай списком 5 позиций", 5)

    assert len(replies) == 1 and isinstance(replies[0], Message)
    actions = [button.action for row in replies[0].keyboard.rows for button in row]
    assert {"export:xlsx", "export:docx", "add_all"} <= set(actions)
    assert session.profile.shortlist and session.profile.export == "shortlist"

    added = gateway.action(USER, "add_all")[0]
    assert "Добавил в корзину" in added.text


def test_citation_follows_the_point_named_in_the_reason():
    first = SimpleNamespace(item_code="1.5.1.35", citation="позиция 1.5.1.35 — приказ № 1057")
    named = SimpleNamespace(item_code="1.14.2.7.2.3", citation="позиция 1.14.2.7.2.3 — приказ № 1057")
    item = SimpleNamespace(norm_mappings=(first, named), reason="пункт 1.14.2.7.2.3, приказ № 1057")
    assert selection.citation(item) == named.citation


# --- Присланный файл ------------------------------------------------------------------------------


def _docx_with_lines(path, lines):
    body = "<w:br/>".join(f"<w:t xml:space=\"preserve\">{line}</w:t>" for line in lines)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r>{body}</w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", xml)
    return path


def test_word_file_with_a_pasted_list_gives_order_lines(tmp_path):
    path = _docx_with_lines(
        tmp_path / "Заказ.docx",
        [
            "Заказ на Элтик",
            "Предварительная комплектация — кабинет логопеда (приказ № 1057, раздел 1.13.3)",
            "1.13.3.3.27 Логопедические зонды — набор, 1 шт.",
            "1.13.3.3.16–1.13.3.3.18 Разрезные сюжетные картинки — 4 комплекта",
        ],
    )
    document = WordOrderParser().parse(path)
    items, _ = OrderNormalizer().normalize(document)

    assert [item.norm_item for item in items] == ["1.13.3.3.27", "1.13.3.3.16"]
    assert [item.quantity for item in items] == [1, 4]


def test_word_file_without_any_list_explains_why(tmp_path):
    document = WordOrderParser().parse(_docx_with_lines(tmp_path / "x.docx", ["просто письмо"]))
    assert document.warnings and document.warnings[0].code == "NO_TABLES"


# --- «Начать заново» ------------------------------------------------------------------------------


def test_restart_from_the_keyboard_asks_first_and_clears_everything(env):
    api, gateway = env
    gateway.text(USER, "Школа, кабинет информатики")
    confirm = gateway.text(USER, "Начать заново")[0]
    assert "restart_yes" in [button.action for row in confirm.keyboard.rows for button in row]

    again = gateway.text(USER, "/start")[0]
    assert "restart_yes" in [button.action for row in again.keyboard.rows for button in row]

    gateway.action(USER, "restart_yes")
    session = api.engine.session(USER, "telegram")
    assert session.profile.is_empty


def test_first_start_greets_without_a_question(env):
    _, gateway = env
    greeting = gateway.text(USER, "/start")[0]
    actions = [button.action for row in greeting.keyboard.rows for button in row]
    assert "restart_yes" not in actions


# --- Telegram: разметка ---------------------------------------------------------------------------


def test_ip_like_point_is_code_and_links_stay_intact():
    pytest.importorskip("aiogram")
    from adapters.telegram.bot import render_text

    assert "<code>1.5.1.35</code>" in render_text("позиция 1.5.1.35 — приказ № 1057")
    assert 'href="https://1.2.3.4/x"' in render_text("[сайт](https://1.2.3.4/x)")


def test_markdown_table_becomes_lines():
    pytest.importorskip("aiogram")
    from adapters.telegram.bot import render_text

    html = render_text("Сводный список:\n| № | Товар | Цена |\n|---|-------|:----:|\n| 1 | Метроном | 3 397 ₽ |")
    assert "|" not in html
    assert "• 1 — Метроном — 3 397 ₽" in html
    assert "Товар — Цена" not in html


def test_card_drops_the_norm_boilerplate_when_the_basis_is_shown():
    pytest.importorskip("aiogram")
    from adapters.telegram.bot import render_card
    from core.ui import ProductCard
    from test_telegram import product

    description = (
        "Набор зондов для постановки звуков.\n\n"
        "Соответствует Приказу №1057 от 25 декабря 2024 г\n"
        '"ОБ УТВЕРЖДЕНИИ ПЕРЕЧНЯ СРЕДСТВ ОБУЧЕНИЯ"\nМИНИСТЕРСТВА ПРОСВЕЩЕНИЯ РОССИЙСКОЙ ФЕДЕРАЦИИ'
    )
    card = ProductCard(product=product(description=description), citation="позиция 1.13.3.3.27 — приказ № 1057")
    rendered = render_card(card)
    assert "Набор зондов" in rendered and "Соответствует Приказу" not in rendered
