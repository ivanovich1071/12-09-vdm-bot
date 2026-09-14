"""NEXT-4.1: консультант и продавец поверх Procurement Core, «Оформить» — в предзаказ ядра.

Проверяется то, что нашёл прогон 13.09 на живой модели: по слову «группа» ход уходил
продавцу, продавец обещал подбор и не вызывал его, страховка дописывала выдачу по
помещению, а «Оформить» вела в прежнюю анкету из шести шагов. Модель подменена
сервером из `test_agent`, каталог и нормативная база — синтетические (`core_fixtures`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("aiogram")
pytest.importorskip("fastapi")

from adapters.telegram.bot import _publish_miniapp  # noqa: E402
from adapters.telegram.gateway import ContactRequest, FileReply, TelegramGateway  # noqa: E402
from agent.routing import CONSULT, SELL, Orchestrator, by_rules  # noqa: E402
from agent.verify import promises_goods  # noqa: E402
from core import intent  # noqa: E402
from core.profile import DialogProfile  # noqa: E402
from core.ui import Message, ProductCard, ProductList  # noqa: E402
from test_agent import FakeCloudRu, answer, attach, client, tool_call  # noqa: E402
from test_core_api import build  # noqa: E402

USER = "700"
CHANNEL = "telegram"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path):
    api = build(tmp_path)
    return api, TelegramGateway(api.core, 20 * 1024 * 1024)


def actions(reply) -> list[str]:  # noqa: ANN001
    keyboard = getattr(reply, "keyboard", None)
    return [button.action for row in (keyboard.rows if keyboard else []) for button in row]


def dialog(api):  # noqa: ANN001, ANN201
    return api.engine.session(USER, CHANNEL)


def texts(replies) -> str:  # noqa: ANN001
    return " ".join(reply.text for reply in replies if isinstance(reply, Message))


# --- Кто отвечает ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Открываем новую группу в детском саду, с чего начать закупку?",
        "У нас детский сад, кабинет логопеда",
        "Нужно оснастить спортзал в саду, дети 3–7 лет",
        "Школа, кабинет информатики",
        "нужна мебель для младшей группы",
    ],
)
def test_describing_the_task_is_the_consultants_turn(text):
    decision = by_rules(text, DialogProfile())
    assert decision is not None and decision.branch == CONSULT


def test_group_or_room_is_not_a_product():
    assert intent.classify("открываем новую группу") == intent.TASK
    assert not intent.names_goods("группа, кабинет, зал, логопед, столовая, математика")


@pytest.mark.parametrize(
    "text",
    ["Мне нужны массажные мячи", "массажные мячи", "подберите мячи для зала", "покажите, что есть по спортзалу"],
)
def test_named_goods_or_a_request_go_to_the_salesman(text):
    decision = by_rules(text, DialogProfile())
    assert decision is not None and decision.branch == SELL and decision.ready_to_see


def test_agreement_is_decided_in_the_context_of_the_question():
    # Согласие посмотреть товары — явная просьба о каталоге, остальное решается в контексте.
    assert by_rules("да", DialogProfile(), "Подобрать варианты из каталога?").branch == SELL
    assert by_rules("да", DialogProfile(), "Вам для детского сада или для школы?") is None
    assert by_rules("да", DialogProfile(), "Рад помочь.").branch == CONSULT


def test_the_new_intent_decides_the_agent_in_both_directions():
    """ORCHESTRATOR.md, 14.09: консультация ↔ продажа — по новому намерению, а не по режиму."""
    from core.dialog import Session

    session = Session(user_id=USER, channel=CHANNEL)
    orchestrator = Orchestrator(None)

    first = orchestrator.decide(session, "что ты можешь подобрать для спорт зала в детском саду")
    assert first.branch == CONSULT and first.previous is None
    assert orchestrator.decide(session, "Какие требования к оборудованию?").branch == CONSULT
    to_sales = orchestrator.decide(session, "Хорошо. Покажите шведские стенки")
    assert to_sales.branch == SELL and to_sales.previous == CONSULT
    back = orchestrator.decide(session, "А что ещё нужно для полноценного спортзала?")
    assert back.branch == CONSULT and back.previous == SELL and back.intent == "FULL_EQUIPMENT_SET"
    assert session.profile.last_agent == CONSULT
    # Названный пункт перечня точен сам по себе — его разбирает продавец сразу.
    assert Orchestrator(None).decide(Session(user_id="p", channel=CHANNEL), "покажите позиции по 2.20.63").branch == SELL


def test_unclear_reply_stays_with_the_agent_who_led_the_dialog():
    from core.dialog import Session

    session = Session(user_id=USER, channel=CHANNEL)
    decision = Orchestrator(None).decide(session, "ну не знаю, а если по-другому")
    assert decision.branch == CONSULT and decision.source == "запасной"

    session.profile.last_agent = SELL
    assert Orchestrator(None).decide(session, "ну не знаю, а если по-другому").branch == SELL


def test_task_details_during_a_selection_are_left_to_the_model():
    profile = DialogProfile(offered=["B1"], stage="presentation")
    assert by_rules("у нас младшая группа, дети 3-4 лет", profile) is None


@pytest.mark.parametrize(
    ("answer_text", "promised"),
    [
        ("Сейчас подберу мячи для вашего зала.", True),
        ("Одну секунду, поищу в каталоге", True),
        ("Подберу варианты под ваш зал.", True),
        ("Хотите, подберу конкретные позиции?", False),
        ("Для какого помещения подбираем?", False),
        ("Когда уточните возраст детей, подберу точнее.", False),
        ("Если скажете бюджет, покажу варианты подешевле.", False),
        # Живой прогон 14.09 на OpenRouter, ответы консультанта:
        ("Пока я ищу, уточните возраст детей?", True),
        ("(Выполняю поиск товаров.)", True),
        ("Сейчас уточню, какие позиции есть в каталоге.", True),
        ("Сейчас выполню подбор массажных мячей по приказу № 1057. Один момент.", True),
        ("(Вызываю инструмент для поиска товаров.)", True),
    ],
)
def test_promise_of_a_selection_is_told_from_an_offer(answer_text, promised):
    assert promises_goods(answer_text) is promised


def test_service_code_lines_leave_no_empty_bullets():
    """Живой прогон 14.09: «- **Код 1С:** 42639» после вырезания кода оставлял пустой пункт «-»."""
    from agent.agent import _without_codes

    answer_text = "1. Шведская стенка (код 1С 42639)\n   - **Цена:** 19 141 ₽\n   - **Код 1С:** 42639\n2. Мат"
    assert _without_codes(answer_text) == "1. Шведская стенка\n   - **Цена:** 19 141 ₽\n2. Мат"


def test_need_description_is_answered_with_the_consultant_prompt(env):
    api, gateway = env
    with FakeCloudRu([answer("Какого возраста дети в новой группе?")]) as cloud:
        attach(api.engine, client(cloud.base_url))
        gateway.text(USER, "Открываем новую группу в детском саду, с чего начать закупку?")
        request = cloud.requests[-1]

    assert "AI-КОНСУЛЬТАНТ" in request["messages"][0]["content"]
    assert "search_products" not in {tool["function"]["name"] for tool in request.get("tools") or []}
    assert dialog(api).route["role"] == CONSULT


def test_request_without_an_object_is_not_a_product_query():
    """Живой прогон 14.09: «да, подберите варианты» искало товар «варианты»."""
    from procurement import discovery

    assert discovery.query_from_text("Да, подберите варианты.") == ""
    assert discovery.query_from_text("Давайте посмотрим ещё подходящие товары") == ""
    assert discovery.query_from_text("Покажите массажные мячи") == "массажные мячи"


def test_consultant_promise_is_removed_and_one_question_leads_on(env):
    """Живой прогон 14.09: консультант писал «сейчас проверю, какие позиции есть… одну минуту»."""
    api, gateway = env
    promise = answer(
        "Для физкультурного зала подберу оборудование по приказу 1057. "
        "Сейчас проверю, какие позиции есть. Одну минуту."
    )
    with FakeCloudRu([promise]) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Детский сад, физкультурный зал, дети 3–4 года")

    route = dialog(api).route
    assert route["role"] == CONSULT and route["false_promise"] is True
    assert "Одну минуту" not in texts(replies) and "Сейчас проверю" not in texts(replies)
    assert texts(replies).rstrip().endswith("Подобрать варианты из каталога?")
    assert not any(isinstance(reply, ProductList | ProductCard) for reply in replies)


def test_consultant_rewrite_keeps_the_consultants_tools(env):
    """Переписывание выдуманного основания — теми же инструментами, что у роли."""
    api, gateway = env
    script = [answer("Это пункт 9.9.9 приказа 1057."), answer("Такого пункта в данных нет.")]
    with FakeCloudRu(script) as cloud:
        attach(api.engine, client(cloud.base_url))
        # Описание задачи — ход консультанта по правилу, без справки по документу.
        gateway.text(USER, "У нас детский сад, кабинет логопеда")
        rewrite = cloud.requests[-1]

    assert len(cloud.requests) == 2, "выдуманный пункт должны были отправить на переписывание"
    assert "search_products" not in {tool["function"]["name"] for tool in rewrite.get("tools") or []}


def test_consultant_reply_has_no_cart_or_checkout(env):
    """14.09, заказчик: под ответом консультанта «Корзина» и «Оформить» не нужны."""
    api, gateway = env
    with FakeCloudRu([answer("Какого возраста дети занимаются у логопеда?")]) as cloud:
        attach(api.engine, client(cloud.base_url))
        # Описание задачи — ход консультанта по правилу, без маршрутизатора-модели.
        replies = gateway.text(USER, "У нас детский сад, кабинет логопеда")

    assert dialog(api).route["role"] == CONSULT
    assert not {"cart", "checkout"} & {action for reply in replies for action in actions(reply)}


def test_consultants_words_do_not_switch_the_dialog_to_the_salesman(env):
    """ORCHESTRATOR.md, раздел 17: фраза консультанта сама по себе переходом в продажу не является."""
    api, gateway = env
    handoff = answer(
        "Я понял задачу. Теперь передам её специалисту, который сможет подобрать "
        "конкретное оборудование и сформировать спецификацию."
    )
    with FakeCloudRu([handoff]) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Детский сад, физкультурный зал, дети 3–4 года")

    assert len(cloud.requests) == 1
    assert not any(isinstance(reply, ProductList | ProductCard) for reply in replies)
    assert dialog(api).route["role"] == CONSULT and dialog(api).profile.last_agent == CONSULT


def test_full_equipment_set_stays_with_the_consultant_and_returns_after_the_salesman(env):
    """Главная претензия 14.09: на комплектацию зала бот сразу выдавал 2–3 товара."""
    api, gateway = env
    listing = answer(
        "Предварительная комплектация по приказу № 1057, раздел 1.5 «Спортивный зал». "
        "Если комплектация подходит, могу показать конкретные товары из каталога — с какой позиции начать?"
    )
    with FakeCloudRu([listing]) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Подберите оборудование для спортзала детского сада")
        consult = cloud.requests[-1]

    route = dialog(api).route
    assert route["role"] == CONSULT and route["intent"] in {"FULL_EQUIPMENT_SET", "ROOM_CONFIGURATION"}
    assert "AI-КОНСУЛЬТАНТ" in consult["messages"][0]["content"]
    assert "Текущий запрос" in consult["messages"][0]["content"]
    assert not any(isinstance(reply, ProductList | ProductCard) for reply in replies)
    assert not {"cart", "checkout"} & {action for reply in replies for action in actions(reply)}

    script = [
        tool_call("search_products", {"query": "мяч"}),
        answer("Подойдёт Мяч баскетбольный № 3 (код 1С B1) — 908 ₽."),
    ]
    with FakeCloudRu(script) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Хорошо. Покажите мячи из каталога")
        sales = cloud.requests[-1]

    route = dialog(api).route
    assert route["role"] == SELL and route["previous_agent"] == CONSULT
    assert "AI-ПРОДАВЕЦ" in sales["messages"][0]["content"] and "вёл консультант" in sales["messages"][0]["content"]
    assert [card.product.sku_1c for card in replies if isinstance(card, ProductCard)] == ["B1"]

    with FakeCloudRu([answer("Кроме мячей для полноценного зала нужны маты и гимнастическая стенка. Уточнить возраст?")]) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "А что ещё нужно для полноценного спортзала?")

    route = dialog(api).route
    assert route["role"] == CONSULT and route["previous_agent"] == SELL and route["intent"] == "FULL_EQUIPMENT_SET"
    assert not any(isinstance(reply, ProductList | ProductCard) for reply in replies)


# --- Подбор — только Procurement Core -----------------------------------------------


def test_salesman_names_only_what_the_core_selected(env):
    api, gateway = env
    script = [
        tool_call("search_products", {"query": "мяч"}),
        answer("Подойдёт Мяч баскетбольный № 3 (код 1С B1) — 908 ₽."),
    ]
    with FakeCloudRu(script) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Детский сад, спортивный зал. Нужен мяч")
        tool_result = [m for m in cloud.requests[-1]["messages"] if m.get("role") == "tool"][0]

    task = api.core.services.procurement.get_task(dialog(api).profile.procurement_task_id, USER)
    products = json.loads(tool_result["content"])["products"]
    assert {p["sku_1c"] for p in products} <= set(task.shown_products)
    assert all(p["reason"] for p in products) and task.institution_type == "preschool"
    assert [card.product.sku_1c for card in replies if isinstance(card, ProductCard)] == ["B1"]


def test_named_goods_are_not_replaced_by_the_rooms_listing(env):
    """На «мяч» в спортзале — мяч из каталога, а не маты и доски того же раздела."""
    api, gateway = env
    replies = gateway.text(USER, "Детский сад, спортивный зал. Нужен мяч")  # модели нет — подбирает ядро

    listing = next(reply for reply in replies if isinstance(reply, ProductList))
    names = [card.product.name for card in listing.cards]
    assert names and all("мяч" in name.lower() for name in names)


def test_false_promise_is_replaced_by_the_cores_selection(env):
    api, gateway = env
    with FakeCloudRu([answer("Сейчас подберу мячи для вашего зала.")]) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Детский сад, спортивный зал. Нужен мяч")
        insisted = [m["content"] for m in cloud.requests[-1]["messages"] if m["role"] == "user"][-1]

    assert "Ты пообещал подобрать" in insisted, "модель должны были попросить подобрать в этом ходе"
    assert "Сейчас подберу" not in texts(replies)
    listing = next(reply for reply in replies if isinstance(reply, ProductList))
    task = api.core.services.procurement.get_task(dialog(api).profile.procurement_task_id, USER)
    assert {card.product.sku_1c for card in listing.cards} <= set(task.shown_products)
    assert all("мяч" in card.product.name.lower() for card in listing.cards)
    assert dialog(api).route["false_promise"] is True


def test_salesman_asked_again_selects_in_the_same_turn(env):
    api, gateway = env
    script = [
        answer("Сейчас поищу мячи."),
        tool_call("search_products", {"query": "мяч"}),
        answer("Нашёлся Мяч баскетбольный № 3 — 908 ₽."),
    ]
    with FakeCloudRu(script) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Детский сад, спортивный зал. Нужен мяч")

    assert [card.product.sku_1c for card in replies if isinstance(card, ProductCard)] == ["B1"]
    assert "false_promise" not in dialog(api).route


def test_question_about_shown_goods_is_not_a_new_selection(env):
    """Прогон 14.09: «чем эти мячи полезны?» подбирало товар по словам вопроса — шнур и корзину."""
    api, gateway = env
    gateway.text(USER, "Детский сад, спортивный зал. Нужен мяч")  # модели нет — подбирает ядро
    assert dialog(api).profile.offered

    with FakeCloudRu([answer("Сейчас подберу информацию по мячам.")]) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Чем этот мяч полезен детям 3–4 лет?")

    assert not any(isinstance(reply, ProductList | ProductCard) for reply in replies)
    assert "Подробнее" in texts(replies) and "Сейчас подберу" not in texts(replies)
    assert dialog(api).route["fallback"] == "question"


def test_tool_narration_on_an_objection_becomes_one_question(env):
    """Прогон 14.09: на «дорого» модель писала «(Вызываю инструмент…) (Ожидаю результатов…)»."""
    api, gateway = env
    gateway.text(USER, "Детский сад, спортивный зал. Нужен мяч")  # модели нет — подбирает ядро
    route = '{"branch":"sell","stage":"objection","objection":"price","objection_handled":false,"ready_to_see":false,"facts":{}}'
    junk = answer(
        "Извините за задержку. (Вызываю инструмент для поиска товаров.) "
        "(Ожидаю результатов поиска.) (Ответ будет точным и проверенным.)"
    )
    with FakeCloudRu([answer(route), junk]) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Дорого. На маркетплейсе такие вдвое дешевле.")

    text = texts(replies)
    assert "инструмент" not in text and "Ожидаю" not in text
    assert "лимит" in text and dialog(api).route["false_promise"] is True
    assert not any(isinstance(reply, ProductList | ProductCard) for reply in replies)


def test_promise_without_the_task_asks_what_is_missing(env):
    api, gateway = env
    with FakeCloudRu([answer("Сейчас подберу массажные мячи.")]) as cloud:
        attach(api.engine, client(cloud.base_url))
        replies = gateway.text(USER, "Мне нужны массажные мячи")

    assert not any(isinstance(reply, ProductList | ProductCard) for reply in replies)
    assert "для детского сада или для школы" in texts(replies)
    assert "Сейчас подберу" not in texts(replies)


# --- «Оформить» — предзаказ ядра -----------------------------------------------------


def test_checkout_goes_to_the_core_preorder_not_the_old_form(env):
    api, gateway = env
    gateway.action(USER, "add:B1")
    replies = gateway.action(USER, "checkout")

    spec, summary, consent = replies
    assert isinstance(spec, FileReply) and spec.filename.endswith(".xlsx")
    assert not any(action.startswith("po_spec:") for action in actions(spec)), "предзаказ уже создан"
    assert "Предварительный заказ" in summary.text and "Шаг 1 из" not in texts(replies)
    consent_action = next(action for action in actions(consent) if action.startswith("po_consent:"))
    [ask] = gateway.action(USER, consent_action)
    assert isinstance(ask, ContactRequest)

    [done] = gateway.contact(USER, "Проверка", "+7 900 000-00-01")
    assert "передан менеджеру" in done.text
    [preorder] = api.core.services.preorders.of_owner(USER)
    assert str(preorder.status) == "SENT_TO_MANAGER" and api.notifier.sent == [preorder.id]


def test_order_command_is_the_same_checkout(env):
    api, gateway = env
    [empty] = gateway.text(USER, "/order")
    assert "Корзина пуста" in empty.text

    gateway.action(USER, "add:B1")
    replies = gateway.text(USER, "/order")
    assert isinstance(replies[0], FileReply) and "Шаг 1 из" not in texts(replies)
    assert api.core.services.preorders.of_owner(USER)


def test_specification_is_built_in_the_dialogs_procurement_task(env):
    api, gateway = env
    gateway.text(USER, "Детский сад, спортивный зал. Нужен мяч")
    task_id = dialog(api).profile.procurement_task_id
    gateway.action(USER, "add:B1")
    gateway.action(USER, "checkout")

    [spec] = api.core.services.procurement.repository.specifications_of(USER)
    assert task_id and spec.task_id == task_id
    assert "совпадает с запросом" in spec.items[0].selection_reason


# --- Mini App ------------------------------------------------------------------------


async def test_miniapp_button_is_published_only_for_https():
    class Bot:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def set_chat_menu_button(self, **kwargs) -> None:  # noqa: ANN003
            self.calls.append(kwargs)

    bot = Bot()
    await _publish_miniapp(bot, "http://127.0.0.1:8000/miniapp")
    assert bot.calls == [], "Telegram не откроет Mini App по http"
    await _publish_miniapp(bot, "https://bot.example.test/miniapp")
    assert len(bot.calls) == 1


def test_miniapp_address_lives_only_in_the_configuration():
    """Смена домена — это `TELEGRAM_MINIAPP_URL`, а не правка кода.

    Ищутся настоящие хосты: пример `http://127.0.0.1:8000/miniapp` в комментарии и
    подсказка `https://…/miniapp` в проверке перед запуском конфигурацией не являются.
    """
    pattern = re.compile(r"https?://(?!127\.0\.0\.1|localhost)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+[^\s\"'<>]*/miniapp")
    hardcoded = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src").rglob("*")
        if path.suffix in {".py", ".html", ".js"} and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert hardcoded == []
