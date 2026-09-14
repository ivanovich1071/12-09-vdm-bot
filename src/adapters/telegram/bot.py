"""Адаптер Telegram.

Ничего не решает сам: переводит сообщения и нажатия в вызовы Core API (через
`TelegramGateway`), а ответы — в сообщения Telegram. Правила консультанта и
продажника, корзины, подбора, заказа и согласия живут в ядре, иначе каналы
разойдутся. Движок диалога, каталог и хранилище бот не собирает и не трогает.

    python run.py telegram          # бот
    python run.py telegram --check  # токен, webhook, Mini App — без запуска
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.types import (
    Message as TgMessage,
)

from adapters.telegram.gateway import ContactRequest, FileReply, TelegramGateway
from core.config import Settings
from core.ui import (
    Keyboard,
    Message,
    OrderSummary,
    ProductCard,
    ProductList,
    Response,
    price_text,
    stock_text,
)
from core_api.composition import build_core_api
from observability import redact

log = logging.getLogger(__name__)
CHANNEL = "telegram"

# Telegram обрезает callback_data на 64 байтах — коды 1С длиннее не бывают,
# но проверку оставляем, чтобы молча не терять кнопки.
CALLBACK_LIMIT = 64

# Подпись к фото Telegram ограничивает жёстче, чем обычное сообщение.
CAPTION_LIMIT = 1024

# Сообщение длиннее 4096 символов Telegram отклоняет целиком. На текущей выгрузке
# самая длинная карточка — 3490, но описания в следующей могут оказаться длиннее,
# и тогда бот молча перестанет отвечать по части товаров.
MESSAGE_LIMIT = 4096
TRUNCATED_TAIL = "\n\n… полное описание на сайте"


def fit(text: str) -> str:
    """Укладывает сообщение в лимит, не ломая разметку.

    Обрезка «в лоб» может разрубить тег пополам или оставить `<b>` незакрытым —
    тогда Telegram отклонит всё сообщение, а не просто покажет его без выделения.
    """
    if len(text) <= MESSAGE_LIMIT:
        return text

    budget = MESSAGE_LIMIT - len(TRUNCATED_TAIL)
    # Закрывающие теги тоже занимают место, а сколько их — известно только после
    # обрезки. Поэтому подбираем: обрезали, посчитали, при нехватке ужались.
    for _ in range(3):
        cut = _trim_partial_tag(text[:budget])
        closers = _closing_tags(cut)
        if len(cut) + len(closers) <= MESSAGE_LIMIT - len(TRUNCATED_TAIL):
            return cut + closers + TRUNCATED_TAIL
        budget -= len(closers)
    return _trim_partial_tag(text[:budget]) + TRUNCATED_TAIL


def split_text(text: str) -> list[str]:
    """Длинный ответ — несколькими сообщениями по границам строк, а не обрезкой.

    Предварительная комплектация консультанта бывает длиннее 4096 знаков, и обрезка
    с хвостом «полное описание на сайте» теряла половину списка. Текст ответа уже
    экранирован и тегов не содержит, поэтому резать по строкам безопасно.
    """
    if len(text) <= MESSAGE_LIMIT:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > MESSAGE_LIMIT:
            if current:
                parts.append(current)
                current = ""
            cut = line.rfind(" ", 0, MESSAGE_LIMIT)
            cut = cut if cut > 0 else MESSAGE_LIMIT
            parts.append(line[:cut])
            line = line[cut:].lstrip()
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MESSAGE_LIMIT:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _trim_partial_tag(text: str) -> str:
    """Убирает обрывок тега в конце: `…<b` или `…</`."""
    last_open = text.rfind("<")
    if last_open > text.rfind(">"):
        text = text[:last_open]
    return text.rstrip()


def _closing_tags(text: str) -> str:
    closers = ""
    for tag in ("b", "i", "u", "s", "code", "pre"):
        unclosed = text.count(f"<{tag}>") - text.count(f"</{tag}>")
        closers += f"</{tag}>" * max(0, unclosed)
    return closers


def to_markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    if keyboard is None or not keyboard.rows:
        return None
    rows = []
    for row in keyboard.rows:
        buttons = []
        for button in row:
            data = button.action.encode("utf-8")
            if button.url:
                buttons.append(InlineKeyboardButton(text=button.title, url=button.url))
            elif len(data) <= CALLBACK_LIMIT:
                buttons.append(
                    InlineKeyboardButton(text=button.title, callback_data=button.action)
                )
            else:
                log.warning("Кнопка «%s» пропущена: слишком длинное действие", button.title)
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# Команды в выпадающем меню у поля ввода. Сами команды Telegram принимает только
# латиницей — это ограничение платформы, а не выбор; подписи русские, и видит
# человек именно их. Меню разгружает окно диалога: постоянные кнопки «Корзина»
# и «Оформить» под каждым сообщением заказчик назвал перегрузом.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "начать заново"),
    ("help", "что я умею"),
    ("cart", "корзина"),
    ("spec", "спецификация из корзины"),
    ("preorders", "мои предзаказы"),
    ("order", "оформить предзаказ"),
    ("manager", "связаться с менеджером"),
)

# Постоянная клавиатура под полем ввода. Сам список команд Telegram рисует только
# столбцом — это UI клиента, раскладку задать нечем. Строку кнопок даёт вот эта
# клавиатура: три частых действия всегда под рукой, остальное остаётся в «Меню».
# Нажатие приходит обычным текстом, разбирает его ядро (`dialog.KEYBOARD_ACTIONS`).
PERSISTENT_BUTTONS: tuple[str, ...] = ("Каталог", "Моя корзина", "Менеджер")


def persistent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=title) for title in PERSISTENT_BUTTONS]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напишите, что нужно подобрать",
    )


def contact_keyboard() -> ReplyKeyboardMarkup:
    """Кнопка «Отправить контакт»: имя и телефон приходят от Telegram, их не нужно набирать."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def render_card(card: ProductCard) -> str:
    """Карточка так же полно, как на странице сайта.

    Описание не режем: раньше стояла обрезка в 400 символов, и текст обрывался на
    середине слова у половины каталога. В лимит сообщения его укладывает `fit`,
    и делает это по границе разметки, а не вслепую.
    """
    product = card.product
    lines = [
        f"<b>{_escape(product.name)}</b>",
        f"{price_text(product.price)} · {stock_text(product)}",
        f"Код 1С: {_escape(product.sku_1c)}",
    ]
    # Оснований у товара бывает несколько, и в спецификации важно видеть все:
    # один комплект закрывает и пункт про словарный запас, и пункт про РАС.
    if card.norms:
        lines.append("")
        lines.append("<b>Основание:</b>")
        lines += [f"• {_escape(line)}" for line in card.norms]
    elif card.citation:
        lines.append(f"Основание: {_escape(card.citation)}")

    # Код в характеристиках дублирует код 1С, он уже выведен строкой выше.
    extra = [(k, v) for k, v in product.attributes.items() if k.lower() != "код"]
    if extra:
        lines.append("")
        lines += [f"{_escape(name)}: {_escape(value)}" for name, value in extra]

    if product.description:
        lines.append("")
        lines.append(_escape(product.description))
    if product.kit_contents:
        lines.append("")
        lines.append("<b>Состав комплекта:</b>")
        lines += [f"• {_escape(item)}" for item in product.kit_contents]
    return "\n".join(lines)


def render_list_header(items: ProductList) -> str:
    """Только заголовок выдачи.

    Сами позиции уходят отдельными сообщениями: в Telegram у каждой нужны свои
    кнопки «В корзину» и «Подробнее», а перечислять их ещё и списком — значит
    показать пользователю одно и то же дважды.
    """
    return f"<b>{_escape(items.title)}</b>"


def render_list_item(card: ProductCard) -> str:
    product = card.product
    lines = [
        f"<b>{_escape(product.name)}</b>",
        f"{price_text(product.price)} · {stock_text(product)}",
    ]
    if card.citation:
        lines.append(_escape(card.citation))
    return "\n".join(lines)


def render_order(summary: OrderSummary) -> str:
    """Корзина: наименования текстом, номера строк совпадают с номерами кнопок.

    Кнопка Telegram вмещает два десятка символов, и товар в ней превращался в
    «1 × Сенсом...». В тексте место есть — здесь и стоят полное название, код 1С
    и нормативное основание, а кнопкам достаётся номер строки.
    """
    lines = ["<b>Ваш заказ</b>", ""]
    for number, line in enumerate(summary.lines, 1):
        lines.append(f"<b>{number}.</b> {_escape(line.name)}")
        tail = f"    {line.quantity} × {price_text(line.price)}"
        if line.sku_1c:
            tail += f" · код 1С {_escape(line.sku_1c)}"
        lines.append(tail)
        if line.norm_citation:
            lines.append(f"    {_escape(line.norm_citation)}")
    lines.append("")
    lines.append(f"<b>Итого: {price_text(summary.total)}</b>")
    if summary.note:
        lines.append("")
        lines.append(_escape(summary.note))
    return "\n".join(lines)


async def send(
    bot: Bot,
    chat_id: int,
    responses: list[Response],
    storage=None,  # noqa: ANN001
    origin: TgMessage | None = None,
    persistent: bool = False,
) -> None:
    for response in responses:
        markup = to_markup(getattr(response, "keyboard", None))

        # Постоянную клавиатуру Telegram принимает только вместо инлайн-кнопок и
        # только один раз: дальше она держится сама, под какими бы сообщениями ни
        # приходили инлайн-кнопки. Ставим её на приветствие и больше не трогаем.
        if persistent and isinstance(response, Message):
            markup = persistent_keyboard()
            persistent = False

        # Изменение количества правит то сообщение, под которым нажали кнопку.
        # Раньше каждое «+» присылало новую копию корзины, изменений в ней было
        # не разглядеть, и человек жал ещё раз — так в чате и появлялись пять
        # одинаковых карточек подряд.
        if getattr(response, "replace", False) and origin is not None:
            text = _replacement_text(response)
            if text is not None and await _edit(bot, origin, text, markup):
                continue

        if isinstance(response, Message):
            parts = split_text(render_text(response.text))
            for part in parts[:-1]:
                await bot.send_message(chat_id, part)
            await bot.send_message(chat_id, parts[-1], reply_markup=markup)
        elif isinstance(response, ProductCard):
            await _send_card(bot, chat_id, response, markup, storage)
        elif isinstance(response, ProductList):
            await bot.send_message(chat_id, fit(render_list_header(response)))
            for card in response.cards:
                await _send_card(bot, chat_id, card, to_markup(card.keyboard), storage, short=True)
            # Навигация по выдаче — последним сообщением, чтобы кнопки были под рукой.
            if markup is not None:
                await bot.send_message(chat_id, "Что дальше?", reply_markup=markup)
        elif isinstance(response, OrderSummary):
            await bot.send_message(chat_id, fit(render_order(response)), reply_markup=markup)
        elif isinstance(response, FileReply):
            from aiogram.types import BufferedInputFile

            await bot.send_document(
                chat_id,
                BufferedInputFile(response.content, filename=response.filename),
                caption=_escape(response.caption)[:CAPTION_LIMIT] or None,
                reply_markup=markup,
            )
        elif isinstance(response, ContactRequest):
            await bot.send_message(chat_id, _escape(response.text), reply_markup=contact_keyboard())


def _replacement_text(response: Response) -> str | None:
    if isinstance(response, Message):
        return fit(render_text(response.text))
    if isinstance(response, ProductCard):
        return fit(render_card(response))
    if isinstance(response, OrderSummary):
        return fit(render_order(response))
    return None


async def _edit(bot: Bot, origin: TgMessage, text: str, markup) -> bool:  # noqa: ANN001
    """Правка сообщения на месте. Возвращает, получилось ли.

    Не получиться может по-разному: сообщение слишком старое, это подпись к фото,
    или текст не изменился вовсе. Ни один из случаев не повод потерять ответ —
    поэтому при неудаче вызывающая сторона просто отправляет новое сообщение.
    """
    try:
        if origin.photo:
            await bot.edit_message_caption(
                chat_id=origin.chat.id,
                message_id=origin.message_id,
                caption=text[:CAPTION_LIMIT],
                reply_markup=markup,
            )
        else:
            await bot.edit_message_text(
                text,
                chat_id=origin.chat.id,
                message_id=origin.message_id,
                reply_markup=markup,
            )
        return True
    except TelegramBadRequest as exc:
        # «message is not modified» — состояние уже такое, какое просили. Для
        # человека это успех: ничего не изменилось и меняться не должно было.
        if "not modified" in str(exc):
            return True
        log.debug("Сообщение не удалось изменить: %s", exc)
        return False


async def _send_card(  # noqa: ANN001
    bot: Bot, chat_id: int, card: ProductCard, markup, storage=None, short: bool = False
) -> None:
    """Карточка с фото — одним сообщением, если подпись помещается.

    Длинное описание в подпись не влезает, поэтому такой товар показываем как
    фото и текст раздельно: обрезать состав комплекта хуже, чем разбить на два
    сообщения. Если картинка недоступна, шлём обычный текст — сорванная загрузка
    не должна лишать пользователя карточки.

    `short` — позиция в выдаче: название, цена, основание и снимок. Полное
    описание там ни к чему, для него есть кнопка «Подробнее».
    """
    text = render_list_item(card) if short else render_card(card)
    photo = _photo(card, storage)
    if photo is None:
        await bot.send_message(chat_id, fit(text), reply_markup=markup)
        return

    try:
        if len(text) <= CAPTION_LIMIT:
            sent = await bot.send_photo(chat_id, photo, caption=text, reply_markup=markup)
        else:
            sent = await bot.send_photo(chat_id, photo)
            await bot.send_message(chat_id, fit(text), reply_markup=markup)
    except TelegramBadRequest as exc:
        log.warning("Фото товара %s не отправилось: %s", card.product.sku_1c, exc)
        await bot.send_message(chat_id, fit(text), reply_markup=markup)
        return

    _remember_photo(storage, card, sent)


def _photo(card: ProductCard, storage):  # noqa: ANN001, ANN202 — тип зависит от aiogram
    """Чем отправлять снимок, от самого дешёвого способа к самому ненадёжному.

    Адрес на vdm.ru стоит последним не случайно: Telegram ходит за картинкой сам
    и с его серверов сайт заказчика не открывается — «failed to get HTTP URL
    content». Поэтому сначала идентификатор уже загруженного файла, потом наш
    собственный файл на диске, и только затем адрес.
    """
    from pathlib import Path

    from aiogram.types import FSInputFile

    if card.image_path and storage is not None:
        file_id = storage.telegram_photo(card.image_path)
        if file_id:
            return file_id
    if card.image_path and Path(card.image_path).is_file():
        return FSInputFile(card.image_path)
    return card.image


def _remember_photo(storage, card: ProductCard, sent) -> None:  # noqa: ANN001
    """Запоминаем файл на серверах Telegram: второй показ уйдёт без загрузки."""
    if storage is None or not card.image_path:
        return
    sizes = getattr(sent, "photo", None)
    if not sizes:
        return
    storage.save_telegram_photo(card.image_path, card.product.sku_1c, sizes[-1].file_id)


def build_dispatcher(gateway: TelegramGateway) -> Dispatcher:
    """Обработчики Telegram — все через Core API (`TelegramGateway`).

    Прежнего пути напрямую в движок диалога больше нет (NEXT-4.1): бот не может
    обойти Core API, даже случайно.
    """
    dispatcher = Dispatcher()

    @dispatcher.message(F.text)
    async def on_text(message: TgMessage, bot: Bot) -> None:
        # Команды разбирает ядро: /start одинаково начинает разговор заново и в
        # Telegram, и в виджете, и правило это должно жить в одном месте.
        user, chat, text = str(message.from_user.id), message.chat.id, message.text
        await _reply(
            bot,
            chat,
            gateway,
            lambda: gateway.text(user, text),
            # Строка кнопок ставится на приветствие: /start человек зовёт и в
            # начале разговора, и когда хочет начать заново.
            persistent=text.strip().lower().startswith("/start"),
        )

    @dispatcher.callback_query(F.data)
    async def on_callback(query: CallbackQuery, bot: Bot) -> None:
        user, chat, data = str(query.from_user.id), query.message.chat.id, query.data
        await _quietly(query.answer())
        await _reply(
            bot,
            chat,
            gateway,
            lambda: gateway.action(user, data),
            origin=query.message,
            # «Начать заново» кнопкой возвращает то же приветствие, что и /start,
            # — и строку кнопок вместе с ним.
            persistent=data == "restart_yes",
        )

    @dispatcher.message(F.document)
    async def on_document(message: TgMessage, bot: Bot) -> None:
        """Готовый заказ файлом: проверку делает ядро, адаптер только скачивает файл."""
        user, chat, document = str(message.from_user.id), message.chat.id, message.document
        if document.file_size and document.file_size > gateway.max_upload_bytes:
            limit = gateway.max_upload_bytes // (1024 * 1024)
            await _reply(bot, chat, gateway, lambda: [Message(f"Файл больше {limit} МБ — пришлите поменьше.")])
            return
        buffer = await bot.download(document)
        content = buffer.read() if buffer is not None else b""
        name = document.file_name or "order"
        await _reply(bot, chat, gateway, lambda: gateway.upload(user, name, content))

    @dispatcher.message(F.contact)
    async def on_contact(message: TgMessage, bot: Bot) -> None:
        contact = message.contact
        user, chat = str(message.from_user.id), message.chat.id
        # Контакт принимаем только свой: чужую визитку менеджеру не передаём.
        if contact.user_id is not None and contact.user_id != message.from_user.id:
            await _reply(bot, chat, gateway, lambda: [Message("Пришлите, пожалуйста, свой контакт.")])
            return
        name = " ".join(filter(None, (contact.first_name, contact.last_name)))
        await _reply(
            bot, chat, gateway, lambda: gateway.contact(user, name, contact.phone_number), persistent=True
        )

    return dispatcher


async def _reply(  # noqa: ANN001
    bot: Bot,
    chat_id: int,
    source,
    work,
    origin: TgMessage | None = None,
    persistent: bool = False,
) -> None:
    """Ответ на сообщение: считаем в отдельном потоке, показываем «печатает».

    `source` — шлюз: у него рендер берёт только кэш `file_id` снимков Telegram.

    Ядро диалога синхронное, а ход с обращением к модели занимает от минуты до
    трёх. Вызванное прямо в обработчике, оно вставало поперёк цикла событий: бот
    переставал опрашивать Telegram, не отвечал другим людям и вообще не подавал
    признаков жизни. Отсюда — отдельный поток.

    Индикатор «печатает» Telegram гасит через пять секунд, поэтому его приходится
    повторять всё время ожидания. Иначе три минуты тишины выглядят как зависание,
    чем они, собственно, и выглядели.
    """
    typing = asyncio.create_task(_keep_typing(bot, chat_id))
    try:
        responses = await asyncio.to_thread(work)
    except Exception:
        log.exception("Ход диалога не отработал")
        responses = [
            Message("Что-то пошло не так на моей стороне. Повторите, пожалуйста, вопрос.")
        ]
    finally:
        typing.cancel()

    if not responses:
        # Нажали надпись, а не кнопку — отвечать нечем и не нужно.
        return

    try:
        await send(bot, chat_id, responses, source.storage, origin, persistent=persistent)
    except TelegramNetworkError as exc:
        # Ответ уже посчитан, но связь оборвалась. Молчим в чат и остаёмся живыми:
        # опрос продолжится, а человек повторит вопрос.
        log.warning("Ответ не доставлен (%s)", exc)


class RetryOnNetworkError(BaseRequestMiddleware):
    """Повтор запроса к Telegram при обрыве связи.

    Канал до api.telegram.org с машины разработки рвётся регулярно, и до сих пор
    это било по самому больному месту: бот получал сообщение, считал ответ — и не
    мог его отправить. С точки зрения человека бот молчал, хотя работал.

    Повторяем всё, что уходит наружу, включая отправку сообщений и фотографий.
    Ошибки самого Telegram (неверный запрос, лимиты) сюда не попадают — их
    повторять бессмысленно, они не про связь.
    """

    def __init__(self, attempts: int = 3, pause: float = 2.0) -> None:
        self.attempts = attempts
        self.pause = pause

    async def __call__(self, make_request, bot, method):  # noqa: ANN001 — тип из aiogram
        pause = self.pause
        for attempt in range(1, self.attempts + 1):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError:
                if attempt == self.attempts:
                    raise
                log.info(
                    "%s не прошёл (попытка %s из %s), повтор через %.0f с",
                    type(method).__name__,
                    attempt,
                    self.attempts,
                    pause,
                )
                await asyncio.sleep(pause)
                pause *= 2
        raise AssertionError("недостижимо")  # pragma: no cover


async def _keep_typing(bot: Bot, chat_id: int) -> None:
    while True:
        await _quietly(bot.send_chat_action(chat_id, "typing"))
        await asyncio.sleep(4)


async def _quietly(coro) -> None:  # noqa: ANN001
    """Необязательное действие: индикатор набора, подтверждение нажатия.

    Раньше сорванный `send_chat_action` уносил с собой весь обработчик, и человек
    не получал ответа вовсе — при том что ответ уже был готов.
    """
    try:
        await coro
    except (TelegramNetworkError, TelegramBadRequest) as exc:
        log.debug("Служебный вызов не прошёл: %s", exc)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_MD_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)\"<>]+)\)")


def render_text(text: str) -> str:
    """Ответ модели для Telegram: экранирование и простая разметка в HTML.

    Живой прогон 14.09: консультант писал комплектацию с `###` и `**` вопреки промпту, и
    Telegram показал бы звёздочки и решётки как есть. Заголовок и жирный переводятся в
    `<b>` в пределах строки — поэтому разбивка ответа по строкам тег не разрывает.
    """
    html = _MD_HEADING.sub(r"<b>\1</b>", _escape(text))
    html = _MD_LINK.sub(r'<a href="\2">\1</a>', html)
    return _MD_BOLD.sub(r"<b>\1</b>", html)


def use_compatible_event_loop() -> None:
    """На Windows aiohttp не работает с циклом событий по умолчанию.

    Стандартный ProactorEventLoop роняет соединение с api.telegram.org ошибкой
    «превышен таймаут семафора» (WinError 121) ещё до отправки запроса, причём
    обычный синхронный запрос к тому же адресу проходит. Лечится переключением
    на SelectorEventLoop. На других системах ничего не меняем.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    # Токен стоит в адресе каждого запроса к Telegram и может оказаться в тексте ошибки —
    # маскируем его в журнале до того, как бот сделает первый запрос.
    redact.install(settings.secret_values)
    if "TELEGRAM_TOKEN" in settings.ignored_env:
        log.warning(
            "TELEGRAM_TOKEN больше не читается: токен бота — только в TELEGRAM_BOT_TOKEN. "
            "Удалите старую строку из .env."
        )
    if not settings.telegram_token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN — бот не запускается.")

    bot = Bot(settings.telegram_token, default=_default_properties(), session=_session())
    bot.session.middleware(RetryOnNetworkError())
    # Бот получает готовый Core API и сам не собирает ни движок, ни хранилище.
    gateway = TelegramGateway(
        build_core_api(settings, warm_llm=True), settings.order_upload_max_mb * 1024 * 1024
    )
    dispatcher = build_dispatcher(gateway)
    await _publish_commands(bot)
    await _publish_miniapp(bot, settings.telegram_miniapp_url)
    log.info("Telegram-бот запущен")
    await _poll_forever(dispatcher, bot)


async def _publish_commands(bot: Bot) -> None:
    """Список команд в меню у поля ввода.

    Меню — единственное место, где человек видит, что бот вообще что-то умеет
    помимо переписки. До сих пор про /start знали только те, кому сказали.
    """
    from aiogram.types import BotCommand

    commands = [BotCommand(command=name, description=title) for name, title in COMMANDS]
    try:
        await bot.set_my_commands(commands)
    except (TelegramNetworkError, TelegramBadRequest) as exc:
        # Меню — украшение; бот без него работает, а падать на старте из-за
        # оборвавшейся сети он не должен.
        log.warning("Меню команд не опубликовано: %s", exc)


async def _publish_miniapp(bot: Bot, url: str) -> None:
    """Кнопка меню «Приложение» — открывает Mini App поверх того же Core API."""
    if not url:
        return
    if not url.startswith("https://"):
        # Telegram открывает Mini App только по HTTPS. http://127.0.0.1:8000/miniapp годится
        # для проверки страницы в браузере разработчика, но не для кнопки в клиенте.
        log.warning("TELEGRAM_MINIAPP_URL не HTTPS — кнопка «Приложение» не публикуется.")
        return
    from aiogram.types import MenuButtonWebApp, WebAppInfo

    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=url)))
    except (TelegramNetworkError, TelegramBadRequest) as exc:
        log.warning("Кнопка Mini App не опубликована: %s", exc)


async def _poll_forever(dispatcher: Dispatcher, bot: Bot) -> None:
    """Опрос, переживающий обрывы связи.

    Внутри опроса aiogram сам повторяет неудачные запросы, но первый же обрыв
    на старте валил процесс целиком. На нестабильном канале это означало, что
    бот не поднимается вовсе. Неверный токен сюда не попадает: это отдельная
    ошибка, и её мы пропускаем наверх, чтобы не крутиться впустую.
    """
    pause = 5
    while True:
        try:
            await dispatcher.start_polling(bot)
            return
        except TelegramNetworkError as exc:
            log.warning("Связь с Telegram потеряна (%s). Повтор через %s с.", exc, pause)
            await asyncio.sleep(pause)
            pause = min(pause * 2, 60)


def _default_properties():  # noqa: ANN202 — тип зависит от версии aiogram
    from aiogram.client.default import DefaultBotProperties

    return DefaultBotProperties(parse_mode="HTML")


def _session():  # noqa: ANN202 — тип зависит от версии aiogram
    """Сессия, принудительно работающая по IPv4.

    api.telegram.org резолвится и в IPv6, но на сетях без реальной IPv6-связности
    соединение просто виснет до таймаута, и бот падает при старте. Оставляем IPv4:
    он доступен везде, где доступен Telegram.
    """
    import socket

    from aiogram.client.session.aiohttp import AiohttpSession

    session = AiohttpSession()
    # aiogram собирает коннектор из этого словаря — добавляем семейство адресов,
    # не трогая остальную его настройку (TLS, лимиты, кэш DNS).
    session._connector_init["family"] = socket.AF_INET
    return session


if __name__ == "__main__":
    use_compatible_event_loop()
    asyncio.run(main())
