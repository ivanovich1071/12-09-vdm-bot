"""Разговор с запущенным ботом через настоящий Telegram — пользовательским аккаунтом (Telethon).

Бот ботом не пишет — Bot API этого не позволяет, — поэтому тестировщик входит в Telegram как человек:
api_id и api_hash с my.telegram.org и один вход по коду. Сессия лежит в `data/qa/tester.session`: это
ключ от аккаунта, наружу его не передавать.

Когда ход закончен. Бот, пока думает, каждые несколько секунд шлёт «печатает…», поэтому ждём, пока идёт
печать, а после сообщений — `quiet` секунд тишины. Что пришло позже, дописывается к прошлому ходу.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from qa.models import BotMessage

RESTART_LABEL = "Начать заново"
RESTART_CONFIRM = "Да, начать заново"
# Сколько ждать первого сообщения после «печатает…» — бот обновляет его примерно раз в 4–5 секунд.
TYPING_GRACE = 12.0
RECENT = 40


@dataclass
class Exchange:
    messages: list[BotMessage] = field(default_factory=list)
    seconds: float = 0.0
    timed_out: bool = False
    late: list[BotMessage] = field(default_factory=list)


class BotChat:
    def __init__(self, client: Any, bot: Any, *, quiet: float = 8.0, timeout: float = 300.0) -> None:
        self.client = client
        self.bot = bot
        self.quiet = quiet
        self.timeout = timeout
        self._events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._recent: dict[int, Any] = {}

    @classmethod
    async def connect(cls, session: str, api_id: int, api_hash: str, bot_username: str, **options: float) -> BotChat:
        from telethon import TelegramClient, events

        client = TelegramClient(session, api_id, api_hash)
        # Первый запуск спрашивает в терминале телефон и код из Telegram — вводит их человек.
        await client.start()
        bot = await client.get_entity(bot_username)
        chat = cls(client, bot, **options)
        client.add_event_handler(chat._on_message, events.NewMessage(chats=bot, incoming=True))
        client.add_event_handler(chat._on_edit, events.MessageEdited(chats=bot, incoming=True))
        client.add_event_handler(chat._on_typing, events.UserUpdate(chats=bot))
        return chat

    async def close(self) -> None:
        await self.client.disconnect()

    async def restart(self) -> Exchange:
        """«Начать заново» с нижней клавиатуры и подтверждение — чистый разговор перед сценарием."""
        first = await self.send(RESTART_LABEL)
        message, _ = self._button(RESTART_CONFIRM)
        if message is None:
            return first
        return await self.click(RESTART_CONFIRM)

    async def send(self, text: str) -> Exchange:
        late = self._drain()
        started = time.monotonic()
        await self.client.send_message(self.bot, text)
        exchange = await self._collect(started, first_timeout=self.timeout)
        exchange.late = late
        return exchange

    async def click(self, label: str) -> Exchange:
        message, position = self._button(label)
        if message is None:
            # Надписи нет под сообщениями — это кнопка нижней клавиатуры: она приходит боту текстом.
            return await self.send(label)
        late = self._drain()
        started = time.monotonic()
        answer = await message.click(*position)
        exchange = await self._collect(started, first_timeout=min(60.0, self.timeout))
        notice = getattr(answer, "message", None)
        if notice:
            exchange.messages.insert(0, BotMessage(id=0, text=f"[уведомление] {notice}"))
        exchange.late = late
        return exchange

    # --- События ---------------------------------------------------------------------------------

    async def _on_message(self, event: Any) -> None:
        await self._events.put(("message", event.message))

    async def _on_edit(self, event: Any) -> None:
        await self._events.put(("edit", event.message))

    async def _on_typing(self, event: Any) -> None:
        if getattr(event, "typing", False):
            await self._events.put(("typing", None))

    async def _collect(self, started: float, first_timeout: float) -> Exchange:
        views: dict[int, BotMessage] = {}
        first_at: float | None = None
        typing_at: float | None = None
        last_activity = started
        deadline = started + self.timeout
        while True:
            now = time.monotonic()
            if views:
                limit = last_activity + self.quiet
            elif typing_at is not None:
                limit = max(started + first_timeout, typing_at + TYPING_GRACE)
            else:
                limit = started + first_timeout
            wait = min(limit, deadline) - now
            if wait <= 0:
                break
            try:
                kind, message = await asyncio.wait_for(self._events.get(), timeout=wait)
            except TimeoutError:
                continue
            last_activity = time.monotonic()
            if kind == "typing":
                typing_at = last_activity
                continue
            first_at = first_at or last_activity
            self._remember(message)
            edited = kind == "edit" or message.id in views
            views[message.id] = self._view(message, edited)
        return Exchange(
            messages=list(views.values()),
            seconds=round((first_at or time.monotonic()) - started, 1),
            timed_out=not views,
        )

    def _drain(self) -> list[BotMessage]:
        late: list[BotMessage] = []
        while not self._events.empty():
            kind, message = self._events.get_nowait()
            if kind == "typing":
                continue
            self._remember(message)
            view = self._view(message, kind == "edit")
            view.late = True
            late.append(view)
        return late

    def _remember(self, message: Any) -> None:
        self._recent[message.id] = message
        while len(self._recent) > RECENT:
            self._recent.pop(next(iter(self._recent)))

    def _button(self, label: str) -> tuple[Any | None, tuple[int, int]]:
        wanted = label.strip().lower()
        for message in reversed(list(self._recent.values())):
            for i, row in enumerate(_inline_rows(message)):
                for j, button in enumerate(row):
                    if button.text.strip().lower() == wanted:
                        return message, (i, j)
        return None, (0, 0)

    @staticmethod
    def _view(message: Any, edited: bool) -> BotMessage:
        document = getattr(message, "document", None)
        file = message.file if document is not None else None
        return BotMessage(
            id=message.id,
            text=message.message or "",
            buttons=[button.text for row in _inline_rows(message) for button in row],
            file=file.name if file is not None else None,
            file_size=file.size if file is not None else None,
            photo=bool(getattr(message, "photo", None)),
            edited=edited,
        )


def _inline_rows(message: Any) -> list[list[Any]]:
    """Кнопки под сообщением. Нижняя клавиатура («Каталог», «Начать заново») сюда не входит."""
    markup = getattr(message, "reply_markup", None)
    if markup is None or type(markup).__name__ != "ReplyInlineMarkup":
        return []
    return [list(row.buttons) for row in markup.rows]
