import json

import pytest

from catalog.models import Product
from catalog.search import CatalogIndex
from core import intent
from core.config import Settings
from core.dialog import CART_PREVIEW_LINES, CHECKOUT_FIELDS, DialogEngine
from core.storage import Storage
from core.ui import Message, OrderSummary, ProductList
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"
USER = "u1"


def product(sku, name, price, norms=()):
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "price": price,
            "currency": "RUB",
            "in_stock": 3,
            "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"]],
            "description": "",
            "kit_contents": [],
            "norms": [
                {
                    "doc_id": "order_838",
                    "doc_citation": "приказ Минпросвещения России от 28.11.2024 № 838",
                    "item_code": code,
                    "item_title": None,
                    "source": "heading",
                    "confidence": 0.9,
                }
                for code in norms
            ],
            "bitrix_id": None,
            "url": f"https://vdm.ru/{sku}",
            "short_url": None,
        }
    )


@pytest.fixture
def engine(tmp_path):
    index = CatalogIndex(
        [
            product("S1", "Фрезерный станок с ЧПУ", 253000, norms=["2.20.63"]),
            product("S2", "Мяч баскетбольный", 908, norms=["1.7.11"]),
        ]
    )
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
    return DialogEngine(index, storage, orders, settings)


def fill_contacts(engine, values=("Школа 1", "Иванов", "+7 916 330-02-79", "-", "Москва", "-")):
    for value in values:
        engine.handle_text(USER, CHANNEL, value)


def test_start_greets_with_menu(engine):
    responses = engine.start(USER, CHANNEL)
    assert isinstance(responses[0], Message)
    assert responses[0].keyboard is not None


def test_search_returns_list_with_citation(engine):
    responses = engine.handle_text(USER, CHANNEL, "2.20.63")
    listing = [r for r in responses if isinstance(r, ProductList)][0]
    assert listing.cards[0].citation.startswith("позиция 2.20.63")


def test_add_and_quantity_changes_persist(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "inc:S1")
    cart = engine.storage.load_cart(USER)
    assert cart.count == 2 and cart.total == 506000


def test_remove_empties_cart(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    responses = engine.handle_action(USER, CHANNEL, "del:S1")
    assert isinstance(responses[0], Message)
    assert engine.storage.load_cart(USER).is_empty


def test_checkout_requires_consent_first(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    responses = engine.handle_action(USER, CHANNEL, "checkout")
    assert "персональных данных" in responses[0].text
    assert engine.storage.active_consent(USER) is None


def test_order_is_not_created_without_consent(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    # Пользователь пытается подтвердить заказ, минуя согласие.
    responses = engine.handle_action(USER, CHANNEL, "confirm_order")
    assert "персональных данных" in responses[0].text
    assert engine.storage.orders_of(USER) == []


def test_full_order_reaches_sink(engine, tmp_path):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    fill_contacts(engine)
    responses = engine.handle_action(USER, CHANNEL, "confirm_order")

    assert "принят" in responses[0].text
    rows = [json.loads(line) for line in (tmp_path / "orders.jsonl").read_text("utf-8").splitlines()]
    assert rows[0]["Наименование"] == "Фрезерный станок с ЧПУ"
    assert rows[0]["Нормативное основание"].startswith("позиция 2.20.63")
    assert engine.storage.load_cart(USER).is_empty


def test_checkout_asks_every_field(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    asked = []
    for value in ("Школа 1", "Иванов", "+7 916 330-02-79", "-", "Москва"):
        asked.append(engine.handle_text(USER, CHANNEL, value)[0].text)
    assert len(asked) == len(CHECKOUT_FIELDS) - 1


def test_incomplete_contacts_are_rejected(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    fill_contacts(engine, values=("-", "-", "-", "-", "-", "-"))
    assert engine.storage.orders_of(USER) == []


def test_cart_shows_price_note_when_price_missing(engine):
    engine.index.products[1] = product("S3", "Панель интерактивная", None)
    engine.index = CatalogIndex(engine.index.products)
    engine.handle_action(USER, CHANNEL, "add:S3")
    summary = engine.handle_action(USER, CHANNEL, "cart")[0]
    assert isinstance(summary, OrderSummary)
    assert "цена уточняется" in (summary.note or "")


def test_delete_data_anonymizes_orders_but_keeps_lines(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    fill_contacts(engine)
    engine.handle_action(USER, CHANNEL, "confirm_order")

    engine.handle_text(USER, CHANNEL, "/delete_data")

    assert engine.storage.orders_of(USER) == []
    anonymized = engine.storage.orders_of("deleted")
    assert anonymized and anonymized[0].customer.name == ""
    assert anonymized[0].items[0].sku_1c == "S1"
    assert engine.storage.active_consent(USER) is None


def test_unknown_command_does_not_crash(engine):
    assert "Такой команды нет" in engine.handle_text(USER, CHANNEL, "/nope")[0].text


@pytest.mark.parametrize(
    ("query", "expected"),
    [("2.20.63", "1 позиция"), ("мяч станок", "2 позиции")],
)
def test_result_title_uses_correct_plural(engine, query, expected):
    """Регрессия: заголовок выдачи писал «1 позиций»."""
    responses = engine.search(engine.session(USER, CHANNEL), query)
    assert expected in responses[0].title


def test_catalog_sections_fit_the_telegram_button_limit(engine):
    """Регрессия: раздел «ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838» терял кнопку.

    Telegram отводит под callback_data 64 байта, а кириллица занимает по два
    на символ — длинные названия разделов в них не помещались, и кнопка молча
    выбрасывалась при отрисовке.
    """
    responses = engine.handle_action(USER, CHANNEL, "catalog")
    buttons = [b for row in responses[0].keyboard.rows for b in row]

    assert buttons, "разделы каталога не показаны"
    for button in buttons:
        assert len(button.action.encode("utf-8")) <= 64, button.action


def test_section_starts_a_consultation_not_a_listing(engine):
    """14.09, заказчик: раздел «Оборудование для детского сада» сначала выясняет задачу, карточки — потом."""
    engine.handle_action(USER, CHANNEL, "catalog")

    responses = engine.handle_action(USER, CHANNEL, "root:0")

    assert len(responses) == 1 and not getattr(responses[0], "cards", None)
    assert responses[0].text.rstrip().endswith("?")
    assert engine.session(USER, CHANNEL).history[-1]["content"] == responses[0].text


def test_old_buttons_with_section_names_still_work(engine):
    """Кнопки в уже отправленных сообщениях должны пережить обновление бота."""
    root = engine.roots[0]

    responses = engine.handle_action(USER, CHANNEL, f"root:{root}")

    assert root.title() in responses[0].text


def test_unknown_section_says_so(engine):
    responses = engine.handle_action(USER, CHANNEL, "root:999")

    assert "нет" in responses[0].text.lower()


# --- Нормативная справка и память разговора ---------------------------------


def test_norm_question_is_answered_without_the_model(engine):
    """Провайдер недоступен, а «что значит приказ 838» обязано работать."""
    engine.agent = None
    responses = engine.handle_text(USER, CHANNEL, "что значит 838 приказ")

    assert isinstance(responses[0], Message)
    assert "28 ноября 2024" in responses[0].text
    assert "справка по данным каталога" in responses[0].text.lower()


def test_duty_question_gets_the_reference_not_a_product_list(engine):
    engine.agent = None
    responses = engine.handle_text(
        USER, CHANNEL, "по чему обязан укомплектовать садик по приказу 1057"
    )

    assert isinstance(responses[0], Message)
    assert "1057" in responses[0].text


def test_request_for_goods_by_norm_is_still_a_search(engine):
    engine.agent = None
    responses = engine.handle_text(USER, CHANNEL, "подбери оборудование по приказу 838")

    assert any(isinstance(r, ProductList) for r in responses)


def test_profile_survives_restart_of_the_process(engine, tmp_path):
    engine.agent = None
    engine.handle_text(USER, CHANNEL, "нужен спортзал в детском саду, дети 3-6 лет")

    restarted = DialogEngine(engine.index, engine.storage, engine.orders, engine.settings)
    profile = restarted.session(USER, CHANNEL).profile

    assert profile.room == "спортивный зал"
    assert profile.age == "3–6 лет"


def test_history_reaches_the_disk_masked(engine):
    engine.agent = None
    engine.handle_text(USER, CHANNEL, "мой телефон +7 916 330-02-79, нужен мяч")

    saved = engine.storage.load_dialog_state(USER, CHANNEL)

    assert "916" not in json.dumps(saved["history"], ensure_ascii=False)
    assert "[ТЕЛЕФОН_1]" in json.dumps(saved["history"], ensure_ascii=False)


def test_delete_data_wipes_the_conversation(engine):
    engine.agent = None
    engine.handle_text(USER, CHANNEL, "нужен спортзал в детском саду")
    engine.handle_text(USER, CHANNEL, "/delete_data")

    session = engine.session(USER, CHANNEL)

    assert session.history == []
    assert session.profile.is_empty
    assert engine.storage.load_dialog_state(USER, CHANNEL) is None


def test_norm_menu_offers_every_document_within_the_button_limit(engine):
    responses = engine.handle_action(USER, CHANNEL, "norms")
    buttons = [b for row in responses[0].keyboard.rows for b in row]

    assert any(b.action == "norm_doc:order_1057" for b in buttons)
    assert all(len(b.action.encode()) <= 64 for b in buttons)


def _order(engine, sku):
    engine.handle_action(USER, CHANNEL, f"add:{sku}")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    fill_contacts(engine)
    return engine.handle_action(USER, CHANNEL, "confirm_order")[0].text


def test_order_confirmation_names_working_hours_and_no_deadline(engine):
    """Формулировку и часы работы согласовал заказчик; сроки бот не называет."""
    text = _order(engine, "S1")
    assert "принят" in text and "Менеджер свяжется с вами в ближайшее время." in text
    assert "10:00–18:00 МСК" in text
    assert "рабочих дней" not in text


def test_small_order_warns_about_delivery_but_still_reaches_the_manager(engine):
    """3000 ₽ — порог доставки, а не заказа: заявка уходит при любой сумме."""
    text = _order(engine, "S2")  # мяч за 908 ₽

    assert "Доставка оформляется от 3 000 ₽" in text
    assert "самовывоз" in text and "vdm.ru/usloviya-raboty-/dostavka/" in text
    assert len(engine.storage.orders_of(USER)) == 1


def test_big_order_says_nothing_about_the_delivery_threshold(engine):
    text = _order(engine, "S1")  # станок за 253 000 ₽
    assert "Доставка оформляется" not in text


def test_manager_button_is_always_within_reach(engine):
    """Контакты в начале чата не спрашиваем — но уйти к человеку можно с первого экрана."""
    keyboard = engine.start(USER, CHANNEL)[0].keyboard
    actions = [button.action for row in keyboard.rows for button in row]
    assert "manager" in actions

# --- Показ карточки не ходит на сайт -------------------------------------------


def test_card_never_touches_the_site(engine, tmp_path):
    """Регрессия 19.09: «Подробнее» подвисала на десятки секунд.

    `_image()` и `photo_path()` шли на vdm.ru прямо в ходе диалога — с таймаутом,
    повторами и общим для процесса шлагбаумом в один запрос в секунду. Теперь
    снимка ждёт фоновый сборщик, а ход отвечает сразу.
    """
    from core.ui import ProductCard
    from media.fetcher import PageFetcher
    from media.files import PhotoStore
    from media.prefetch import MediaPrefetcher
    from media.service import MediaService

    class Tripwire(PageFetcher):
        def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            raise AssertionError("ход диалога сходил на сайт")

    fetcher = Tripwire()
    media = MediaService(
        engine.storage, fetcher, enabled=True, photos=PhotoStore(fetcher, tmp_path / "media")
    )
    media.prefetch = MediaPrefetcher(media)
    engine.media = media

    responses = engine.handle_action(USER, CHANNEL, "card:S2")

    card = [r for r in responses if isinstance(r, ProductCard)][0]
    assert card.product.sku_1c == "S2"
    assert media.prefetch.pending == 1, "товар без снимка должен уйти фоновому сборщику"


# --- Оформление без сюрпризов: анкета, счётчики файла -------------------------


def test_wizard_does_not_take_service_words_as_data(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    # «Оформить» в поле «организация» уезжало менеджеру в заявке (17.09).
    asked = engine.handle_text(USER, CHANNEL, "Оформить")[0].text
    assert "Название организации" in asked
    assert engine.session(USER, CHANNEL).customer.organization == ""


def test_wizard_reasks_bad_phone(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    engine.handle_text(USER, CHANNEL, "Школа 1")
    engine.handle_text(USER, CHANNEL, "Иванов")
    asked = engine.handle_text(USER, CHANNEL, "Иванова")[0].text
    assert "телефон" in asked.lower()
    asked = engine.handle_text(USER, CHANNEL, "+7 916 330-02-79")[0].text
    assert "E-mail" in asked
    assert engine.session(USER, CHANNEL).customer.phone == "+7 916 330-02-79"


def test_order_cart_counts_lines_like_the_preview(engine):
    """Превью проверки файла считает строки, «положил» — товары: цифры не должны спорить."""
    engine.session(USER, CHANNEL).profile.order = {
        "file": "заказ.xlsx",
        "positions": [
            {"sku": "S1", "quantity": 2},
            {"sku": "S1", "quantity": 1},
            {"sku": "НЕТ-В-КАТАЛОГЕ", "quantity": 1},
        ],
    }
    replies = engine.order_cart(engine.session(USER, CHANNEL), default=1)
    assert "2 из 3" in replies[0].text
    cart = engine.storage.load_cart(USER)
    assert cart.count == 2 and len(cart.items) == 1


# --- 21.09: полный перечень, а не коды из обрезанного ответа ---------------------------------


def kit(session, positions):
    session.profile.remember_kit(
        {"document": "order_838", "code": "2.20", "title": "Кабинет труда", "positions": positions}
    )


def test_full_kit_goes_to_cart_not_just_surviving_codes(engine):
    """«По этому списку сформируй предзаказ по 1 шт»: перечень из профиля целиком.

    21.09 комплектация логопеда на 88 позиций собралась восемью: ответ бота обрезан
    до 2500 знаков, и только уцелевшие коды доехали до корзины.
    """
    session = engine.session(USER, CHANNEL)
    kit(
        session,
        [
            {"code": "2.20.63", "title": "Фрезерный станок"},
            {"code": "1.7.11", "title": "Мяч баскетбольный"},
            {"code": "2.20.99", "title": "В каталоге товара нет"},
        ],
    )
    replies = engine.checkout_by_intent(
        session, "по этому списку сформируй предзаказ по 1 шт по каждой позиции"
    )
    cart = engine.storage.load_cart(USER)
    assert {item.sku_1c for item in cart.items} == {"S1", "S2"}
    assert "из 3" in replies[0].text
    assert "Без позиций остались пункты 2.20.99" in replies[0].text


def test_kit_used_only_while_it_is_the_last_thing_shown(engine):
    """Устаревшей комплектацией корзину не наполняем: после неё показывали другое."""
    session = engine.session(USER, CHANNEL)
    kit(session, [{"code": "2.20.63", "title": "Фрезерный станок"}])
    session.profile.export = "order"
    engine.checkout_by_intent(session, "оформи предзаказ")
    assert engine.storage.load_cart(USER).is_empty


def test_cart_citation_names_the_requested_point(tmp_path):
    """Основание позиции в корзине — запрошенный пункт, а не выбор по релевантности.

    21.09 песочница, взятая по пункту 1.13.3.2.3, уехала в заявку с подписью 1.13.2.3.6.
    """
    multi = product(
        "M1",
        "Интерактивная песочница",
        5000,
        norms=["2.20.63", "1.7.11"],
    )
    index = CatalogIndex([multi])
    storage = Storage(tmp_path / "t.sqlite3")
    engine = DialogEngine(
        index,
        storage,
        OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl")),
        Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl")),
    )
    session = engine.session(USER, CHANNEL)
    engine.checkout_by_intent(session, "1.7.11 мяч — сформируй предзаказ по 1 шт")
    cart = engine.storage.load_cart(USER)
    assert cart.count == 1
    assert "позиция 1.7.11" in (cart.items[0].norm_citation or "")


def test_complaint_about_positions_is_not_a_checkout_request():
    """«Проверь, ты выдаешь предзаказ только на 8 позиций» — вопрос, а не «оформить»."""
    assert not intent.asks_checkout(
        "в полном списке в файле 88 позиций - проверь , ты выдаешь предзаказ только на 8 позиций"
    )
    assert not intent.asks_checkout("почему предзаказ не на все позиции")
    assert intent.asks_checkout("сформируй предзаказ по 1 шт по каждой позиции")
    assert not intent.asks_order_checkout("проверь заказ, тут не всё")
    assert intent.asks_order_checkout("оформи всё из файла, все позиции по 1 шт")


def test_wizard_does_not_take_questions_as_data(engine):
    """Вопрос посреди анкеты названием организации не становится."""
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    replies = engine.handle_text(USER, CHANNEL, "а можно доставку в другой регион?")
    assert engine.session(USER, CHANNEL).customer.organization == ""
    assert any("Шаг 1" in r.text for r in replies if isinstance(r, Message))


def test_cart_preview_caps_lines(tmp_path):
    """Полная комплектация — до сотни строк: в превью первые, остальное — словами."""
    storage = Storage(tmp_path / "t.sqlite3")
    engine = DialogEngine(
        CatalogIndex([product(f"P{i}", f"Товар {i}", 100) for i in range(25)]),
        storage,
        OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl")),
        Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl")),
    )
    for i in range(25):
        engine.handle_action(USER, CHANNEL, f"add:P{i}")
    summary = engine.handle_action(USER, CHANNEL, "cart")[0]
    assert isinstance(summary, OrderSummary)
    assert len(summary.lines) == CART_PREVIEW_LINES
    assert "ещё 5" in (summary.note or "")
    assert summary.total == 2500


class RecordingSink:
    name = "recording"

    def __init__(self):
        self.extras = []

    def push(self, order, extras=()):
        self.extras.append(list(extras))


def test_submit_attaches_kit_file_for_the_manager(engine, tmp_path):
    """Заявка менеджеру несёт файл полного перечня, а не только позиции из корзины."""
    sink = RecordingSink()
    engine.orders = OrderService(engine.storage, sink)
    session = engine.session(USER, CHANNEL)
    kit(session, [{"code": "2.20.63", "title": "Фрезерный станок"}])
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    fill_contacts(engine)
    engine.handle_action(USER, CHANNEL, "confirm_order")
    assert sink.extras and sink.extras[0][0][0].endswith(".xlsx")


def test_empty_cart_offers_to_repeat_the_last_specification(tmp_path):
    """Пустая корзина — не тупик: последнюю спецификацию можно собрать заново."""
    from core_fixtures import procurement_service, products

    storage = Storage(tmp_path / "t.sqlite3")
    engine = DialogEngine(
        CatalogIndex(products()),
        storage,
        OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl")),
        Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl")),
    )
    engine.procurement = procurement_service(tmp_path)

    task = engine.procurement.create_task(USER, "telegram", text="Детский сад, по приказу 1057 пункт 1.5.1")
    selection = engine.procurement.select(task.id, USER)
    engine.procurement.choose(task.id, USER, [item.product_id for item in selection.items])
    engine.procurement.build_specification(task.id, USER, None)
    assert engine.procurement.repository.specifications_of(USER), "спецификация собрана"

    [empty] = engine.handle_action(USER, CHANNEL, "cart")
    assert "Собрать её заново" in empty.text
    assert any(button.action == "repeat_last_spec" for row in empty.keyboard.rows for button in row)

    engine.handle_action(USER, CHANNEL, "repeat_last_spec")
    cart = engine.storage.load_cart(USER)
    assert cart.count > 0


def test_web_render_strips_markdown():
    """Виджет не рисует markdown: звёздочки модели клиент видеть не должен (21.09)."""
    from web.render import to_json

    [data] = to_json([Message("раздел **2.20 «Кабинет труда»** и `код`")])
    assert data["text"] == "раздел 2.20 «Кабинет труда» и код"
