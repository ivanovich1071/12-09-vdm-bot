"""Регрессии ночного прогона 16→17.09 (`data/qa/night-3`, 50 сценариев, и `data/qa/fix-4`).

Что чиним:
- модель писала вызов инструмента текстом, и он уходил клиенту: голый JSON
  `{"name":"handoff_to_manager",…}` (сц. 50 — снимок экрана от заказчика), «Функция
  вызывается:» с тремя блоками (сц. 47), «*Вызываю инструменты для проверки.*» (сц. 48);
- слово «скачала» в прошедшем времени читалось как просьба прислать файл: три хода подряд
  «Пришлю комплектацию по разделу 1.14 файлом. В каком виде?» на вопрос о сроке (сц. 3);
- срок от оформления до счёта и график поставок по этапам оставались без ответа — самая
  частая претензия судьи (сц. 2, 3, 10, 11, 44, 45, 47);
- «Не вижу телефона» на реплику про другое: 21 раз за ночь, в том числе на вопрос
  «когда менеджер свяжется?» (сц. 10);
- заголовки зон без единого пункта под ними: проверка вырезала строки списка (сц. 2);
- «всё передал менеджеру» без заявки — шесть диалогов;
- «Пункт перечня: 1.1.1.1» проходил мимо проверки оснований (сц. 3);
- названный товар прятался за типом учреждения: школьный кабинет ИЗО не увидел ни одного
  из 27 мольбертов каталога (сц. 18);
- непарные «**» доходили до клиента (сц. 5, 49).
"""

from __future__ import annotations

import pytest

from adapters.telegram.bot import render_text
from adapters.telegram.gateway import TelegramGateway
from agent.routing import _asks_export
from agent.verify import (
    claims_handoff,
    looks_like_tool_call,
    norm_refs_in,
    promises_goods,
    without_tool_calls,
    without_unverified,
)
from core import intent
from core.ui import Message
from core_fixtures import procurement_service
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

SCREENSHOT = (
    "```json\n"
    "{\n"
    '  "name": "handoff_to_manager",\n'
    '  "parameters": {\n'
    '    "reason": "Запрос на 10 парт для детей 5-6 лет, поставка в Минск за неделю."\n'
    "  }\n"
    "}\n"
    "```"
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


@pytest.fixture
def env(tmp_path):
    api = build(tmp_path)
    return api, TelegramGateway(api.core, 20 * 1024 * 1024)


# --- Вызов инструмента, написанный текстом ---------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        SCREENSHOT,
        'Функция вызывается:\n\n```json\n{"code":"2.15.4","document":"838"}\n```',
        "*Вызываю инструменты для проверки.* Добавляю в корзину...",
        "Давайте уточним подбор через инструмент.",
        "Проверяю товары: Городки, Конусы...",
    ],
)
def test_a_tool_call_written_as_text_is_a_false_promise(said):
    assert looks_like_tool_call(said), "пересказ вызова не распознан"
    assert promises_goods(said), "подбора не было — это ложное обещание"


def test_an_ordinary_answer_is_not_taken_for_a_tool_call():
    said = "Мат детский — 8 164 ₽, под заказ. Показать ещё варианты?"
    assert not looks_like_tool_call(said)
    assert without_tool_calls(said) == said


def test_the_json_from_the_screenshot_never_reaches_the_client(engine):  # noqa: F811
    """Снимок экрана 17.09: клиент увидел тело вызова handoff_to_manager вместо ответа."""
    with FakeCloudRu([answer(SCREENSHOT), answer(SCREENSHOT)]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, "передайте мой запрос менеджеру")

    said = texts(replies)
    assert "handoff_to_manager" not in said and "```" not in said
    assert '"name"' not in said and '"parameters"' not in said


def test_an_unclosed_json_block_leaves_nothing_behind():
    """Сц. 47: три блока подряд, последний модель не закрыла."""
    said = (
        "Функция вызывается:\n\n```json\n"
        '{"name":"find_by_norm_code","arguments":{"code":"1.14.5.1.4"}}\n```\n\n```json\n'
        '{"name":"find_by_norm_code","arguments":{"code":"1.14.5.1.5"}}'
    )
    assert without_tool_calls(said) == ""


def test_the_tool_name_is_cut_out_of_a_mixed_answer():
    said = "Парт для 5–6 лет в каталоге нет.\n```json\n{\"name\":\"handoff_to_manager\"}\n```\nСвязать с менеджером?"
    kept = without_tool_calls(said)
    assert "handoff_to_manager" not in kept
    assert "Парт для 5–6 лет в каталоге нет." in kept and "Связать с менеджером?" in kept


# --- Файл, который уже скачали ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "asks"),
    [
        ("хорошо, скачала. а сколько по времени от оформления до счёта?", False),
        ("меня интересует срок от оформления до счёта. excel я уже скачала", False),
        ("файл получила, спасибо", False),
        ("пришлите список в excel", True),
        ("сохраните комплектацию в файл", True),
        ("мне нужна спецификация для бухгалтерии в Excel и счёт от менеджера", True),
        ("выгрузи весь каталог в json", False),
    ],
)
def test_a_downloaded_file_is_not_a_new_request(said, asks):
    assert _asks_export(said) is asks


# --- Срок до счёта и график поставок ---------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "сколько по времени от оформления до счёта?",
        "мне нужен примерный график поставок по этапам",
        "когда менеджер свяжется?",
        "какой срок поставки?",
    ],
)
def test_questions_only_the_manager_can_answer_are_recognised(said):
    assert intent.asks_deadline(said)


def test_a_question_about_goods_is_not_a_question_about_deadlines():
    assert not intent.asks_deadline("покажите первые три позиции из раздела 2.12")
    assert not intent.asks_deadline("нужны мольберты для 30 детей")
    # Срок службы — свойство товара, о нём отвечает карточка, а не менеджер.
    assert not intent.asks_deadline("какой срок службы у этого мата?")
    assert not intent.asks_deadline("какой гарантийный срок?")


ROUTE = (
    '{"branch":"sell","stage":"presentation","objection":"none",'
    '"objection_handled":true,"ready_to_see":true,"facts":{}}'
)


def test_the_deadline_question_is_answered_by_the_manager_not_by_a_file(env):
    """Сц. 3: на вопрос о сроке бот трижды подряд предлагал скачать комплектацию."""
    api, gateway = env
    with FakeCloudRu([answer(ROUTE), answer("Пришлю файл.")]) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "хорошо, скачала. а сколько по времени от оформления до счёта?")

    said = texts(replies)
    assert "менеджер" in said.lower(), "о сроке должен ответить менеджер"
    assert "В каком виде?" not in said, "файл уже скачан — второй раз его не предлагаем"
    assert "manager" in actions(replies)
    assert api.engine.session(USER, CHANNEL).route.get("fallback") == "deadline"


# --- Ожидание контакта -----------------------------------------------------------------------


def waiting_for_contact(api, gateway) -> None:  # noqa: ANN001
    """Собранный предзаказ, который ждёт имя и телефон."""
    api.storage.record_consent(USER, "telegram", "test", "granted")
    gateway.action(USER, "add:B1")
    gateway.action(USER, "checkout")
    assert USER in gateway._awaiting_contact


def test_the_contact_wait_does_not_swallow_a_question(env):
    """Сц. 10: «когда менеджер свяжется?» получило в ответ «Не вижу телефона»."""
    api, gateway = env
    waiting_for_contact(api, gateway)

    assert "Не вижу телефона" not in texts(gateway.text(USER, "хорошо, а когда менеджер свяжется?"))


def test_the_contact_button_caption_is_not_a_missing_phone(env):
    """Подпись кнопки пришла текстом: номера в ней нет, но это не отказ человека."""
    api, gateway = env
    waiting_for_contact(api, gateway)

    said = "\n".join(reply.text for reply in gateway.text(USER, "Отправить контакт"))
    assert "Не вижу телефона" not in said
    assert "имя и телефон" in said


def test_a_name_without_a_phone_is_still_asked_again(env):
    """Человек диктует имя — просьба о телефоне остаётся в силе."""
    api, gateway = env
    waiting_for_contact(api, gateway)

    said = "\n".join(reply.text for reply in gateway.text(USER, "Мария Иванова"))
    assert "Не вижу телефона" in said


# --- Кнопка менеджера доводит до заявки ------------------------------------------------------


def test_the_manager_button_leads_to_a_request_when_the_cart_is_full(env):
    """Пять ночных диалогов кончились телефоном менеджера и без заявки."""
    _, gateway = env
    gateway.action(USER, "add:B1")
    replies = gateway.action(USER, "manager")

    assert "checkout" in actions(replies), "из контактов менеджера нет пути к заявке"
    assert "/order" not in texts(replies), "командой в Telegram не нажать — нужна кнопка"


def test_the_manager_button_with_an_empty_cart_offers_to_collect_one(env):
    _, gateway = env
    replies = gateway.action(USER, "manager")

    assert "checkout" not in actions(replies), "оформлять нечего"
    assert "подобрать" in texts(replies).lower()


# --- «Передал менеджеру» без заявки ----------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "claimed"),
    [
        ("Всё передал менеджеру — теперь под ответом появится кнопка.", True),
        ("Я передал все параметры менеджеру: ДОО, спальня 3–4 года.", True),
        ("Хотите, чтобы я передал вопрос менеджеру?", False),
        ("Могу передать заявку менеджеру — скажите, когда.", False),
        ("Менеджер получит заявку, когда вы оставите имя и телефон.", False),
    ],
)
def test_a_handoff_is_claimed_only_when_it_happened(said, claimed):
    assert claims_handoff(said) is claimed


# --- Заголовок без пунктов и пункт перечня ---------------------------------------------------


def test_a_heading_without_items_is_not_shown():
    """Сц. 2: человек получил названия зон и пустоту под ними."""
    said = (
        "Групповое помещение 5–6 лет (раздел 1.14.6)\n"
        "- Кровать детская — 9 999 ₽\n"
        "- Шкаф для одежды — 27 405 ₽\n"
        "\n"
        "Скажите, с чего начнём."
    )
    assert without_unverified(said, set(), set()) == "Скажите, с чего начнём."


def test_a_heading_keeps_its_place_while_one_item_is_left():
    said = "Спальня\n- Кровать детская — 9 999 ₽\n- Матрас — 1 500 ₽"
    kept = without_unverified(said, {9999}, set())
    assert kept.startswith("Спальня") and "Матрас" not in kept


def test_a_point_of_the_list_with_an_extra_word_is_checked():
    """Сц. 3: «Пункт перечня: 1.1.1.1» проверку оснований обходил."""
    assert (None, "1.1.1.1") in norm_refs_in("- Столы детские\n  - Пункт перечня: 1.1.1.1")
    found = norm_refs_in("Пункт перечня: 1.1.1.1, пункт 2.1.4 приказа 838")
    assert found == {(None, "1.1.1.1"), ("order_838", "2.1.4")}


# --- Названный товар не прячется за типом учреждения -----------------------------------------


def test_a_named_product_is_found_outside_the_clients_audience(tmp_path):
    """Сц. 18: школьному кабинету ИЗО каталог не показал ни одного мольберта из 27."""
    service = procurement_service(tmp_path)
    task = service.create_task(
        "user-1", "test", text=None, fields={"institution_type": "школа", "query": "ковёр"}
    )
    result = service.select(task.id, "user-1")

    assert [item.product_id for item in result.items] == ["Z1"], "садовская позиция потерялась"
    assert any(notice.code == "AUDIENCE_WIDENED" for notice in result.warnings)


def test_the_audience_still_ranks_first_when_it_has_the_goods(tmp_path):
    """Расширение — только когда по словам запроса в своём разделе пусто."""
    service = procurement_service(tmp_path)
    task = service.create_task(
        "user-1", "test", text=None, fields={"institution_type": "школа", "query": "ноутбук"}
    )
    result = service.select(task.id, "user-1")

    assert [item.product_id for item in result.items] == ["I1"]
    assert not any(notice.code == "AUDIENCE_WIDENED" for notice in result.warnings)


# --- Разметка ---------------------------------------------------------------------------------


def test_stray_asterisks_do_not_reach_the_client():
    assert render_text("Цена **543 ₽**, под заказ") == "Цена <b>543 ₽</b>, под заказ"
    assert "*" not in render_text("Итого:**  ")
    # Одиночная звёздочка — размер: «146*32*132».
    assert render_text("Шкаф 146*32*132 см") == "Шкаф 146*32*132 см"
