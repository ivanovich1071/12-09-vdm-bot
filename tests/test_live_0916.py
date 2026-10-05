"""Регрессии ночного прогона 15.09 (`data/qa/night-2`, 50 сценариев «Ход N», пройдено 25).

Что чиним:
- ответ на нашу же просьбу переписать («Спасибо, что поправили. Переписываю…») уходил клиенту:
  26 сообщений в 17 диалогах из 25, а в двух ходах кроме извинения не было ничего;
- ход доходил до десяти вызовов модели — 79 секунд ожидания и 6.25 ₽ за диалог против 1.35 ₽,
  а когда ответ не подтверждался, человек получал «консультант временно недоступен»;
- модель перечисляла пункты приказа как товары: в тексте 2.14.1–2.14.3, карточками 2.14.106 (сц. 11);
- «Оформить. Согласен. Организация, контакт…» приводило к анкете от модели: за 25 диалогов
  ни одного предзаказа и ни одной непустой корзины;
- «нужна спецификация в Excel и счёт» упиралось в «Сохранять пока нечего» (11 диалогов из 25);
- карточка с длинным описанием уходила фотографией без подписи (33 сообщения без текста).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from adapters.telegram.bot import CAPTION_LIMIT, _send_card
from adapters.telegram.gateway import preorder_preview
from agent.agent import TURN_CALLS, SalesAgent
from agent.tools import ToolBox
from agent.verify import without_meta
from catalog.models import Product
from catalog.points import PointFinder
from catalog.search import CatalogIndex
from core import exports, intent
from core.ui import Message, ProductCard
from ingest.xlsx_reader import XlsxFile
from norms import items as norm_items
from order_import.normalizer import OrderNormalizer
from order_import.parsers import parser_for
from test_agent import (  # noqa: F401 — engine: фикстура
    CHANNEL,
    USER,
    FakeCloudRu,
    answer,
    attach,
    client,
    engine,
    ready,
    tool_call,
)
from test_core_api import build

PHOTO_EFFECT = "2.14.106 Установка для изучения фотоэффекта. Лабораторно-демонстрационный комплект"
POINTS_ANSWER = (
    "Вот первые три позиции раздела 2.14 приказа № 838:\n"
    "1. 2.14.1 — Стол лабораторный демонстрационный с надстройкой\n"
    "2. 2.14.2 — Стол лабораторный демонстрационный с розетками\n"
    "3. 2.14.3 — Стол ученический, регулируемый по высоте\n"
    "Этих позиций в каталоге не нашлось."
)


def texts(responses) -> str:  # noqa: ANN001
    return "\n".join(response.text for response in responses if isinstance(response, Message))


def actions(reply) -> list[str]:  # noqa: ANN001
    keyboard = getattr(reply, "keyboard", None)
    return [button.action for row in (keyboard.rows if keyboard else []) for button in row]


def catalog(*products: Product) -> SalesAgent:
    """Агент без диалога и модели: проверяем только сведение текста ответа с карточками."""
    agent = SalesAgent.__new__(SalesAgent)
    agent.engine = SimpleNamespace(index=CatalogIndex(list(products)))
    return agent


def product(sku: str, name: str, price: int, description: str = "") -> Product:
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "price": price,
            "currency": "RUB",
            "in_stock": 0,
            "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"]],
            "description": description,
            "kit_contents": [],
            "norms": [],
            "bitrix_id": None,
            "url": None,
            "short_url": None,
        }
    )


# --- Вежливость в ответ на нашу же жалобу ----------------------------------------------------


def test_apology_to_our_own_complaint_is_not_an_answer():
    assert without_meta("Понял, спасибо за замечание. Переписываю с опорой на инструмент.") == ""
    assert without_meta("Вы правы, спасибо. Переписываю строго по данным из инструментов") == ""
    assert without_meta("Спасибо, что поправили. В приказе это «Словари».") == "В приказе это «Словари»."
    # Благодарность с фактом — обычный ответ, его не трогаем.
    assert without_meta("Спасибо за уточнение: для детей 5–6 лет подойдёт раздел 1.14.5.").startswith("Спасибо")


def test_rewrite_that_is_only_an_apology_never_reaches_the_client(engine):  # noqa: F811
    script = [
        answer("Станок стоит 99 999 ₽ — это последняя цена."),
        answer("Вы правы, спасибо. Переписываю строго по данным из инструментов."),
    ]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        ready(engine)
        responses = engine.handle_text(USER, CHANNEL, "сколько стоит фрезерный станок?")

    said = texts(responses)
    assert "Переписываю" not in said and "спасибо" not in said.lower()
    assert "99 999" not in said


# --- Лимит вызовов модели на ход -------------------------------------------------------------


def test_after_the_call_limit_the_client_gets_the_verified_lines(engine):  # noqa: F811
    """Ход не уходит на второй круг вызовов, а неподтверждённое просто не показывается."""
    invented = (
        "Фрезерный станок с ЧПУ стоит 99 999 ₽ — это последняя цена.\n"
        "Станок ставится в кабинете технологии и работает от обычной розетки 220 В."
    )
    script = [tool_call("search_products", {"query": "станок"})] * 4 + [answer(invented)] * 6
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        ready(engine)
        responses = engine.handle_text(USER, CHANNEL, "сколько стоит фрезерный станок?")

    assert len(cloud.requests) <= TURN_CALLS + 1
    said = texts(responses)
    assert "99 999" not in said, "выдуманная цена показана человеку"
    assert "розетки" in said, "подтверждённый текст потерян — так и рождается «консультант недоступен»"


# --- Пункты приказа — не товары --------------------------------------------------------------


def test_points_of_the_order_do_not_pull_cards_of_other_products():
    agent = catalog(product("0Э-1", PHOTO_EFFECT, 40250))
    tools = ToolBox(None, None)
    tools.shown_skus = ["0Э-1"]

    assert agent._mentioned_skus(tools, POINTS_ANSWER) == []


def test_the_list_in_the_text_is_built_from_the_cards_that_follow():
    agent = catalog(product("0Э-1", PHOTO_EFFECT, 40250))
    tools = ToolBox(None, None)
    tools.shown_skus = ["0Э-1"]

    text = agent._with_catalog_positions(tools, POINTS_ANSWER)

    # К6.1 (план 05-10): пунктов 2.14.1-2.14.3 в каталоге нет — чужой раздел
    # («2.14.106 Установка для фотоэффекта») под текстом про столы не дописывается.
    assert "2.14.106" not in text and "40 250" not in text
    assert "товаров в каталоге нет" in text


def test_products_of_another_section_are_not_added_to_the_answer():
    agent = catalog(product("0Э-2", "2.20.63 Фрезерный станок с ЧПУ", 253000))
    tools = ToolBox(None, None)
    tools.shown_skus = ["0Э-2"]

    text = agent._with_catalog_positions(tools, POINTS_ANSWER)
    assert "Фрезерный станок" not in text
    assert "товаров в каталоге нет" in text


# --- «Оформить» словами ----------------------------------------------------------------------


def test_checkout_in_words_fills_the_cart_instead_of_a_form(engine):  # noqa: F811
    script = [answer("1. Название организации? 2. Контактное лицо? 3. Телефон?")]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        ready(engine)
        engine.session(USER, CHANNEL).profile.offered.append("S1")
        responses = engine.handle_text(USER, CHANNEL, "Оформить. Согласен, организация МБОУ СОШ № 5.")

    # Показанное само в корзину не падает: бот сначала перечисляет, что положит,
    # и только явное «Добавить и оформить» наполняет корзину.
    assert engine.storage.load_cart(USER).is_empty
    assert "order_shown" in actions(responses[0])
    assert "Название организации" not in texts(responses), "анкету пишет модель, а не ядро"

    engine.handle_action(USER, CHANNEL, "order_shown")
    cart = engine.storage.load_cart(USER)
    assert cart.count == 1 and cart.items[0].sku_1c == "S1"


def test_checkout_with_an_empty_cart_says_so_instead_of_asking_for_details(engine):  # noqa: F811
    script = [answer("1. Название организации? 2. Контактное лицо?")]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        ready(engine)
        responses = engine.handle_text(USER, CHANNEL, "Оформить заказ, мы согласны.")

    said = texts(responses)
    assert "корзина пуста" in said.lower()
    assert "Название организации" not in said


def test_words_about_checkout_in_a_question_stay_with_the_model(engine):  # noqa: F811
    script = [answer("Заказ оформляется так: собираем корзину, потом менеджер выставляет счёт.")]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        ready(engine)
        responses = engine.handle_text(USER, CHANNEL, "А как у вас оформить заказ?")

    assert "менеджер выставляет счёт" in texts(responses)


# --- Предзаказ по пунктам перечня, названным человеком ---------------------------------------

POINTS_ORDER = (
    "детей в группе 18, зал площадью 45 м.кв. из этого перечня все по 1 шт:\n"
    "- 2.20.63 Фрезерный станок с ЧПУ — 4 шт. — технология.\n"
    "- 2.20.99 Верстак ученический — 6 шт. — рабочее место.\n"
    "сформируй предзаказ"
)


def test_points_named_by_the_client_become_a_preorder(engine):  # noqa: F811
    """16.09: помещение названо, количество названо, «сформируй предзаказ» — и «корзина пуста»."""
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, POINTS_ORDER)

    cart = engine.storage.load_cart(USER)
    assert [(item.sku_1c, item.quantity) for item in cart.items] == [("S1", 1)]
    said = texts(responses)
    assert "корзина пуста" not in said.lower()
    assert "2.20.99" in said, "пункт без позиций в каталоге назван человеку, а не забыт"
    assert "checkout" in actions(responses[0])
    assert not cloud.requests, "оформление — работа ядра, модель тут не нужна"


def test_quantity_of_each_point_is_taken_from_the_message(engine):  # noqa: F811
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        engine.handle_text(USER, CHANNEL, "2.20.63 Фрезерный станок — 4 шт. Оформить.")

    cart = engine.storage.load_cart(USER)
    assert [(item.sku_1c, item.quantity) for item in cart.items] == [("S1", 4)]


def test_checkout_right_after_the_bot_listed_the_points(engine):  # noqa: F811
    """«Оформи» под присланной комплектацией: пункты берутся из последнего ответа бота."""
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        session = engine.session(USER, CHANNEL)
        session.remember("assistant", "Комплектация кабинета:\n- 2.20.63 Станок фрезерный — 1 шт.")
        responses = engine.handle_text(USER, CHANNEL, "сформируй предзаказ")

    assert engine.storage.load_cart(USER).count == 1
    assert "checkout" in actions(responses[0])


def test_points_without_products_do_not_become_an_empty_cart_message(engine):  # noqa: F811
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "Оформить 9.99.99 и 9.99.98, по 1 шт.")

    said = texts(responses)
    assert engine.storage.load_cart(USER).is_empty
    assert "9.99.99" in said and "9.99.98" in said
    assert "корзина пуста" not in said.lower()


def test_dates_in_the_message_are_not_taken_for_points():
    assert intent.listed_points("приказ от 25.12.2024 № 838, раздел 1.5") == []
    assert intent.listed_points("1.5.1.6 стенка — 4 шт.") == [("1.5.1.6", 4)]


# --- Спецификация, когда собирать ещё нечего -------------------------------------------------


def test_specification_request_without_a_list_is_answered_by_the_agent(engine):  # noqa: F811
    script = [answer("Спецификацию соберу, как только наберём позиции. Для какого кабинета подбираем?")]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "нужна спецификация в Excel и счёт")

    said = texts(responses)
    assert "Сохранять пока нечего" not in said
    assert "Для какого кабинета" in said


# --- Пункт без привязки: номер в названии и формулировка приказа ------------------------------


def registry(*items: tuple[str, str, str]) -> norm_items.ItemIndex:
    """Реестр пунктов приказа: код, формулировка, норма."""
    return norm_items.ItemIndex(
        {
            "order_1057": {
                code: norm_items.NormItem("order_1057", code, title, None, "Шт.", quantity)
                for code, title, quantity in items
            }
        }
    )


def finder(products: list, items: tuple[tuple[str, str, str], ...] = ()) -> PointFinder:
    return PointFinder(CatalogIndex(products), registry(*items), ("order_1057",), "preschool")


def test_point_without_a_registry_link_is_found_by_its_number_in_the_name():
    """Каталог заказчика назван по перечню: номер пункта стоит в самом названии товара."""
    found = finder([product("0Э-1", "1.5.1.5 Балансиры напольные разного типа", 92085)]).find("1.5.1.5")

    assert found is not None and found.how == "name_code" and found.confirmed


def test_point_is_found_by_the_wording_of_the_order():
    goods = [product("0Э-2", "SPR Мяч футбольный", 1053)]
    found = finder(goods, (("1.5.1.37", "Мяч футбольный, размер 2, 3", "2"),)).find("1.5.1.37")

    assert found is not None and found.how == "title"
    assert not found.confirmed, "подбор по словам показывается человеку как требующий проверки"


def test_a_product_of_another_point_is_not_a_replacement():
    """«Мат 1000×1000×80» не подменяется матом 2000×1100×80 из соседнего пункта."""
    goods = [product("0Э-3", "1.5.1.9 Мат гимнастический, длина 2000 мм, ширина 1100 мм", 8591)]
    items = (("1.5.1.8", "Мат гимнастический, длина 1000 мм, ширина 1000 мм, толщина 80 мм", "2"),)

    assert finder(goods, items).find("1.5.1.8") is None


def test_a_different_thing_with_the_same_words_is_not_a_replacement():
    goods = [product("0Э-4", "ФСИ Канат подвесной для лазания, длина 3 м", 4200)]
    items = (("1.5.1.63", "Тоннель для эстафет, длина 3 м, диаметр 90 см", "2"),)

    assert finder(goods, items).find("1.5.1.63") is None


def test_the_cart_says_which_positions_were_picked_by_wording(engine):  # noqa: F811
    """Пункт с привязкой реестра кладётся в корзину молча: проверять человеку нечего."""
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "Оформить 2.20.63 — 1 шт.")

    assert engine.storage.load_cart(USER).count == 1
    assert "по формулировке" not in texts(responses), "привязка реестра проверки не требует"
    assert not cloud.requests


# --- Единая таблица: выгрузка бота читается обратно как заказ ---------------------------------


def test_the_exported_sheet_is_read_back_as_an_order(engine, tmp_path):  # noqa: F811
    """Человек скачал комплектацию, проставил количество и прислал файл боту.

    До 16.09 колонки выгрузки назывались «Наименование по перечню» и «Кол-во по перечню» —
    разбор заказа таких не знал и терял количество: «Количество указано не у всех позиций».
    """
    session = engine.session(USER, CHANNEL)
    session.profile.remember_kit(
        {
            "document": "order_838",
            "code": "2.20",
            "title": "Кабинет технологии",
            "positions": [
                {"code": "2.20.63", "title": "Станок фрезерный с числовым программным управлением", "quantity": "1 Шт."},
                {"code": "2.20.64", "title": "Станок сверлильный настольный", "quantity": "2 Шт."},
            ],
        }
    )

    for fmt in ("xlsx", "docx"):
        file = exports.build(engine, session, fmt)
        assert file is not None
        path = tmp_path / file.filename
        path.write_bytes(file.content)

        items, warnings = OrderNormalizer().normalize(parser_for(file.filename).parse(path))

        assert [notice.code for notice in warnings] == []
        assert [(item.article, item.quantity, item.norm_item) for item in items] == [
            ("S1", 1, "2.20.63"),
            (None, 2, "2.20.64"),
        ], f"формат {fmt}: файл бота не читается его же разбором"
        assert items[0].price == 253000, "цена из файла нужна, чтобы увидеть, что она изменилась"


def test_the_sheet_says_how_each_position_was_picked(engine, tmp_path):  # noqa: F811
    session = engine.session(USER, CHANNEL)
    session.profile.remember_kit(
        {
            "document": "order_838",
            "code": "2.20",
            "title": "Кабинет технологии",
            "positions": [
                {"code": "2.20.63", "title": "Станок фрезерный с ЧПУ", "quantity": "1 Шт."},
                {"code": "2.20.64", "title": "Станок сверлильный настольный", "quantity": "2 Шт."},
            ],
        }
    )
    file = exports.build(engine, session, "xlsx")
    path = tmp_path / file.filename
    path.write_bytes(file.content)

    cells = [value for _, row in XlsxFile(path).numbered_rows(0) for value in row.values()]

    assert "по перечню" in cells and exports.NOT_IN_CATALOG in cells
    assert any(str(value).startswith("Чтобы заказать") for value in cells), "в файле сказано, как заказать"


SPORT_KIT = {
    "document": "order_1057",
    "code": "1.5.1",
    "title": "Спортивное оборудование и инвентарь",
    "positions": [
        {"code": "1.5.1.7", "title": "Мат гимнастический", "quantity": "1 Шт."},
        {"code": "1.5.1.13", "title": "Доска с ребристой поверхностью", "quantity": "2 Шт."},
        {"code": "1.5.1.33", "title": "Мяч для игр", "quantity": "4 Шт."},
    ],
}


def test_the_downloaded_sheet_comes_back_as_a_preorder(tmp_path):
    """Круг целиком: бот выгрузил комплектацию — человек прислал её обратно — бот собрал предзаказ."""
    from adapters.telegram.gateway import TelegramGateway

    api = build(tmp_path)
    gateway = TelegramGateway(api.core, 20 * 1024 * 1024)
    profile = api.engine.session(USER, "telegram").profile
    profile.norm_doc_ids.append("order_1057")
    profile.remember_kit(SPORT_KIT)

    excel = gateway.action(USER, "export:xlsx")[0]
    replies = gateway.upload(USER, excel.filename, excel.content)

    said = replies[0].text
    assert "Предзаказ на согласование" in said, said
    assert "Мат детский — 1 × 8 164 ₽" in said, "количество взято из файла, цена — из каталога"
    assert "Итого 3 позиции на 37 292 ₽" in said
    assert "po_order" in "".join(actions(replies[0])), "предзаказ оформляется кнопкой, без переписки"


def test_the_preorder_is_shown_before_the_contact_is_asked():
    """Состав предзаказа с ценами — в сообщении, а не только в файле после телефона."""
    matched = [
        {"line_no": 1, "name": "Шведская стенка", "quantity": 4, "current_price": 19141, "current_total": 76564},
        {"line_no": 2, "name": "Мат детский", "quantity": 2, "current_price": 8164, "current_total": 16328},
    ]

    preview = preorder_preview(matched)

    assert "Шведская стенка — 4 × 19 141 ₽ = 76 564 ₽" in preview
    assert "Итого 2 позиции на 92 892 ₽" in preview


# --- Карточка с фото всегда с подписью -------------------------------------------------------


class FakeBot:
    """Телеграм, который запоминает подпись к снимку."""

    def __init__(self) -> None:
        self.captions: list[str | None] = []
        self.messages: list[str] = []

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):  # noqa: ANN001
        self.captions.append(caption)
        return SimpleNamespace(photo=[])

    async def send_message(self, chat_id, text, reply_markup=None):  # noqa: ANN001
        self.messages.append(text)


def test_card_photo_is_never_sent_without_a_caption():
    bot = FakeBot()
    long_one = product(
        "0Э-3",
        "2.14.100 Набор демонстрационный по электрическому току в вакууме",
        28635,
        description="Состав комплекта. " * 120,
    )

    asyncio.run(_send_card(bot, 1, ProductCard(product=long_one, image="https://vdm.ru/p.jpg"), None))

    assert bot.captions and bot.captions[0], "фото ушло без подписи — это сообщение без текста"
    assert len(bot.captions[0]) <= CAPTION_LIMIT
    assert bot.messages, "полное описание должно прийти следующим сообщением"
