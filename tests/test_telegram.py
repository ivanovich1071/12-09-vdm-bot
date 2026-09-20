import pytest

pytest.importorskip("aiogram")

import asyncio  # noqa: E402

from aiogram.exceptions import (  # noqa: E402
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from adapters.telegram.bot import (  # noqa: E402
    CALLBACK_LIMIT,
    MESSAGE_LIMIT,
    RetryOnNetworkError,
    TurnLocks,
    _reply,
    fit,
    persistent_keyboard,
    render_card,
    render_list_item,
    render_text,
    split_text,
    to_markup,
)
from catalog.models import Product  # noqa: E402
from core.ui import Button, Keyboard, Message, ProductCard  # noqa: E402


def product(**kw):
    raw = {
        "sku_1c": "S1",
        "name": "Мяч баскетбольный",
        "price": 908,
        "currency": "RUB",
        "in_stock": 4,
        "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"]],
        "description": "",
        "kit_contents": [],
        "norms": [],
        "bitrix_id": None,
        "url": None,
        "short_url": None,
    }
    raw.update(kw)
    return Product.from_dict(raw)


def test_short_text_is_untouched():
    assert fit("<b>Мяч</b>") == "<b>Мяч</b>"


def test_long_text_fits_the_limit():
    assert len(fit("а" * 5000)) <= MESSAGE_LIMIT


def test_truncation_closes_open_tags():
    """Регрессия: незакрытый <b> заставляет Telegram отклонить всё сообщение."""
    result = fit("<b>" + "а" * 5000)
    assert result.count("<b>") == result.count("</b>")
    assert len(result) <= MESSAGE_LIMIT


def test_truncation_never_leaves_half_a_tag():
    # Обрезка приходится ровно на середину тега.
    text = "а" * (MESSAGE_LIMIT - 30) + "<b>хвост</b>" + "б" * 100
    result = fit(text)
    assert "<" not in result[result.rfind(">") + 1 :]
    assert len(result) <= MESSAGE_LIMIT


def test_model_markdown_becomes_telegram_html():
    """Живой прогон 14.09: «### Предварительная комплектация» и «**Мат**» пришли бы звёздочками."""
    text = "### Предварительная комплектация\n1. **Мат гимнастический** — 2 шт. <для кувырков> & прыжков"
    assert render_text(text) == (
        "<b>Предварительная комплектация</b>\n"
        "1. <b>Мат гимнастический</b> — 2 шт. &lt;для кувырков&gt; &amp; прыжков"
    )
    assert render_text("5 * 3 ** 2") == "5 * 3 ** 2"
    link = "1. **[Шведская стенка](https://vdm.ru/catalog/a.html?x=1&y=2)** — 19 141 ₽"
    assert render_text(link) == (
        '1. <b><a href="https://vdm.ru/catalog/a.html?x=1&amp;y=2">Шведская стенка</a></b> — 19 141 ₽'
    )


def test_long_answer_is_split_by_lines_not_cut():
    """Предварительная комплектация консультанта длиннее 4096 знаков — ни одна строка не теряется."""
    lines = [f"{number}. 1.5.1.{number} Мат гимнастический — 2 шт., для кувырков" for number in range(1, 160)]
    text = "\n".join(lines)
    parts = split_text(text)

    assert len(parts) > 1 and all(len(part) <= MESSAGE_LIMIT for part in parts)
    assert "\n".join(parts) == text
    assert split_text("коротко") == ["коротко"]
    assert all(len(part) <= MESSAGE_LIMIT for part in split_text("слово " * 2000))


def test_special_characters_are_escaped_in_card():
    card = ProductCard(product=product(name='Набор "Мама & сын" <малый>'))
    rendered = render_card(card)
    assert "&amp;" in rendered and "&lt;малый&gt;" in rendered
    assert "<малый>" not in rendered


def test_card_shows_price_stock_and_citation():
    card = ProductCard(product=product(), citation="позиция 1.7.11 — приказ № 838")
    rendered = render_card(card)
    assert "908 ₽" in rendered and "в наличии 4 шт." in rendered
    assert "позиция 1.7.11" in rendered


def test_card_without_price_says_on_request():
    assert "цена по запросу" in render_card(ProductCard(product=product(price=None)))


def test_list_item_is_compact():
    rendered = render_list_item(ProductCard(product=product(description="ж" * 3000)))
    assert len(rendered) < 200


def test_link_button_becomes_url_button():
    keyboard = Keyboard().row(Button("Открыть на сайте", "card:S1", url="https://vdm.ru/x"))
    markup = to_markup(keyboard)
    assert markup.inline_keyboard[0][0].url == "https://vdm.ru/x"


def test_overlong_callback_is_dropped_not_sent_broken():
    keyboard = Keyboard().row(Button("Кнопка", "add:" + "я" * CALLBACK_LIMIT))
    assert to_markup(keyboard) is None


def test_empty_keyboard_gives_no_markup():
    assert to_markup(None) is None
    assert to_markup(Keyboard()) is None


# --- Полная карточка «как на сайте» -------------------------------------------


def test_description_is_not_cut_in_the_middle():
    """Регрессия: описание резалось на 400 символах, у половины каталога — по слову."""
    text = "Комплект дидактических пособий. " * 40
    card = render_card(ProductCard(product=product(description=text)))

    assert text.strip() in card
    assert "…" not in card


def test_card_shows_country_and_certificate():
    """Страну и сертификат в закупке для сада и школы спрашивают всерьёз."""
    card = render_card(
        ProductCard(
            product=product(
                attributes={"Код": "0Э-00005662", "Страна": "Китай", "Сертификат": "ЕАС"}
            )
        )
    )

    assert "Страна: Китай" in card and "Сертификат: ЕАС" in card
    # Код 1С уже выведен отдельной строкой — второй раз он не нужен.
    assert card.count("0Э-00005662") == 0
    assert "Код 1С: S1" in card


def test_whole_kit_is_listed():
    card = render_card(ProductCard(product=product(kit_contents=[f"поз. {i}" for i in range(12)])))

    assert "поз. 11" in card


def test_long_card_still_fits_the_message_limit():
    card = render_card(ProductCard(product=product(description="Очень длинно. " * 900)))

    assert len(fit(card)) <= MESSAGE_LIMIT


# --- Чем отправлять снимок ------------------------------------------------------


class FakeStorage:
    def __init__(self, known=None):
        self.known = known or {}
        self.saved = []

    def telegram_photo(self, path):
        return self.known.get(path)

    def save_telegram_photo(self, path, sku_1c, file_id):
        self.saved.append((path, sku_1c, file_id))


def test_known_file_id_is_reused(tmp_path):
    from adapters.telegram.bot import _photo

    path = tmp_path / "1.jpg"
    path.write_bytes(b"jpeg")
    card = ProductCard(product=product(), image="https://vdm.ru/1.jpg", image_path=str(path))

    assert _photo(card, FakeStorage({str(path): "AgACAgIAAx"})) == "AgACAgIAAx"


def test_local_file_beats_the_address(tmp_path):
    """Telegram не может забрать картинку с vdm.ru сам — файл ему нужнее адреса."""
    from aiogram.types import FSInputFile

    from adapters.telegram.bot import _photo

    path = tmp_path / "1.jpg"
    path.write_bytes(b"jpeg")
    card = ProductCard(product=product(), image="https://vdm.ru/1.jpg", image_path=str(path))

    assert isinstance(_photo(card, FakeStorage()), FSInputFile)


def test_address_is_the_last_resort(tmp_path):
    from adapters.telegram.bot import _photo

    card = ProductCard(
        product=product(), image="https://vdm.ru/1.jpg", image_path=str(tmp_path / "нет.jpg")
    )

    assert _photo(card, FakeStorage()) == "https://vdm.ru/1.jpg"


def test_card_without_any_photo_gives_none():
    from adapters.telegram.bot import _photo

    assert _photo(ProductCard(product=product()), FakeStorage()) is None


# --- Обработчик сообщения ------------------------------------------------------


class FakeBot:
    """Бот, у которого индикатор набора всегда обрывается по сети."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_chat_action(self, chat_id, action):  # noqa: ANN001
        raise TelegramNetworkError(method=None, message="ClientConnectorError")

    async def send_message(self, chat_id, text, reply_markup=None):  # noqa: ANN001
        self.sent.append(text)


class FakeEngine:
    storage = None


async def test_broken_typing_indicator_does_not_swallow_the_answer():
    """Регрессия 28.08: бот молчал на сообщения, хотя ответ был готов.

    Сорванный `send_chat_action` уносил с собой весь обработчик — со стороны это
    выглядело как зависший бот.
    """
    bot = FakeBot()
    await _reply(bot, 1, FakeEngine(), lambda: [Message("Готовый ответ")])

    assert bot.sent == ["Готовый ответ"]


async def test_failure_inside_the_core_still_gets_a_human_answer():
    bot = FakeBot()

    def broken():
        raise RuntimeError("что-то сломалось в ядре")

    await _reply(bot, 1, FakeEngine(), broken)

    assert bot.sent and "Повторите" in bot.sent[0]


async def test_long_answer_does_not_block_the_event_loop():
    """Ход с обращением к модели идёт минуты — всё это время бот обязан жить."""
    import time

    bot = FakeBot()
    ticks = 0

    async def other_work():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1

    await asyncio.gather(
        _reply(bot, 1, FakeEngine(), lambda: (time.sleep(0.2), [Message("Готово")])[1]),
        other_work(),
    )

    assert ticks == 5, "цикл событий стоял, пока считался ответ"
    assert bot.sent == ["Готово"]


async def test_send_is_retried_when_the_link_breaks():
    """Регрессия 28.08: ответ был готов, но не доходил — канал до Telegram рвётся."""
    calls = 0

    async def make_request(bot, method):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TelegramNetworkError(method=None, message="ClientConnectorError")
        return "доставлено"

    middleware = RetryOnNetworkError(attempts=3, pause=0.01)

    assert await middleware(make_request, None, object()) == "доставлено"
    assert calls == 3


async def test_hopeless_link_gives_up_instead_of_retrying_forever():
    async def always_broken(bot, method):  # noqa: ANN001
        raise TelegramNetworkError(method=None, message="ClientConnectorError")

    middleware = RetryOnNetworkError(attempts=2, pause=0.01)

    with pytest.raises(TelegramNetworkError):
        await middleware(always_broken, None, object())


# --- Зависания: повторное нажатие, недоступное сообщение, лимит частоты -------


async def test_second_tap_does_not_start_a_second_turn():
    """Два нажатия подряд считались одновременно и писали в одну сессию поверх друг друга."""
    turns = TurnLocks()

    async with turns.wait("42"):
        assert turns.busy("42") is True
        assert turns.busy("43") is False, "чужой разговор ждать не должен"

    assert turns.busy("42") is False


async def test_undeliverable_answer_does_not_lose_the_handler():
    """Кнопка под сообщением старше двух суток: ответ посчитан, отправка не вышла.

    Раньше `_reply` ловил вокруг отправки только сетевой обрыв, и всё остальное
    уносило обработчик — человек не получал ничего и не узнавал почему.
    """

    class Unavailable(FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None):  # noqa: ANN001
            raise TelegramBadRequest(method=None, message="message to edit not found")

    await _reply(Unavailable(), 1, FakeEngine(), lambda: [Message("Ответ")])


async def test_rate_limit_is_waited_out_not_dropped():
    """429 на середине ответа: раньше хвост выдачи пропадал молча."""
    calls = 0

    async def make_request(bot, method):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TelegramRetryAfter(method=None, message="Flood control exceeded", retry_after=0)
        return "доставлено"

    middleware = RetryOnNetworkError(attempts=3, pause=0.01)

    assert await middleware(make_request, None, object()) == "доставлено"
    assert calls == 2


async def test_service_calls_are_not_retried():
    """Подтверждение нажатия и «печатает» устаревают быстрее, чем дойдёт повтор."""
    calls = 0

    class AnswerCallbackQuery:
        pass

    async def make_request(bot, method):  # noqa: ANN001
        nonlocal calls
        calls += 1
        raise TelegramNetworkError(method=None, message="ClientConnectorError")

    middleware = RetryOnNetworkError(attempts=3, pause=0.01)

    with pytest.raises(TelegramNetworkError):
        await middleware(make_request, None, AnswerCallbackQuery())
    assert calls == 1


# --- Mini App в постоянной клавиатуре -----------------------------------------


def test_miniapp_button_appears_only_for_https():
    """Telegram открывает Mini App только по HTTPS — по http кнопка бесполезна."""
    titles = lambda markup: [b.text for row in markup.keyboard for b in row]  # noqa: E731

    assert "Приложение" not in titles(persistent_keyboard())
    assert "Приложение" not in titles(persistent_keyboard("http://127.0.0.1:8000/miniapp"))

    markup = persistent_keyboard("https://bot.example/miniapp")
    button = [b for row in markup.keyboard for b in row if b.text == "Приложение"][0]
    assert button.web_app.url == "https://bot.example/miniapp"



# --- Транзит до Telegram ------------------------------------------------------


def test_direct_session_stays_direct():
    from adapters.telegram.bot import _session

    session = _session()
    assert session._connector_init["family"] != 0  # принудительный IPv4 на месте
    assert "proxy_type" not in session._connector_init


def test_session_goes_through_the_transit_when_it_is_set():
    pytest.importorskip("aiohttp_socks")
    from adapters.telegram.bot import _session

    session = _session("socks5://bot:s3cret@transit.example:1080")

    # Коннектор транзита aiogram собирает из этих полей; IPv4 при этом не теряется.
    assert session._connector_init["host"] == "transit.example"
    assert session._connector_init["port"] == 1080
    assert session._connector_init["family"] != 0


def test_transit_address_is_shown_without_the_password():
    from adapters.telegram.bot import _proxy_label

    assert _proxy_label("socks5://bot:s3cret@transit.example:1080") == "transit.example:1080"
    assert _proxy_label("http://transit.example:3128") == "http://transit.example:3128"


def test_preflight_names_the_transit_but_not_its_password():
    from adapters.telegram.preflight import settings_checks
    from core.config import Settings

    settings = Settings(telegram_token="1:x", telegram_proxy="socks5://bot:s3cret@transit.example:1080")
    line = " ".join(check.line() for check in settings_checks(settings))

    assert "transit.example:1080" in line and "s3cret" not in line


def test_transit_address_is_a_secret_for_the_log():
    """В адресе стоит пароль, а адрес попадает в текст сетевой ошибки aiohttp."""
    from core.config import Settings

    settings = Settings(telegram_proxy="socks5://bot:s3cret@transit.example:1080")
    assert settings.telegram_proxy in settings.secret_values
