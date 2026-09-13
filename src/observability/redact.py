"""Секреты не попадают в журнал.

Токен бота стоит в адресе каждого запроса к Telegram: `https://api.telegram.org/bot<токен>/…`.
aiogram кладёт текст ошибки aiohttp в `TelegramNetworkError`, а у `ClientResponseError`
в этом тексте адрес целиком — вместе с токеном. Бот пишет такие ошибки в журнал
(«Связь с Telegram потеряна …»), и журнал становится местом утечки.

Маскируем на входе в журнал, а не в каждом месте, где что-то пишется: фабрика записей
видит все записи процесса — наши, aiogram, uvicorn — раньше любого обработчика.
Скрываются значения из настроек (токен, ключи Core API и моделей) и всё, что похоже
на токен Telegram, в том числе чужой — например, прежнего бота, оставшегося в `.env`.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable

MASK = "<скрыто>"
# Токен Telegram: числовой id бота, двоеточие и 35 символов base64url. Не `\b`: в адресе
# `…/bot<токен>/…` перед цифрами стоит буква, и границы слова там нет.
TELEGRAM_TOKEN = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")
# Короткие значения не маскируем: «1234» в настройке превратил бы в звёздочки полжурнала.
MIN_SECRET_LENGTH = 8

_secrets: set[str] = set()
_lock = threading.Lock()
_installed = False


def redact(text: str, secrets: Iterable[str] | None = None) -> str:
    known = _secrets if secrets is None else {s for s in secrets if s and len(s) >= MIN_SECRET_LENGTH}
    for secret in sorted(known, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return TELEGRAM_TOKEN.sub(MASK, text)


def install(secrets: Iterable[str] = ()) -> None:
    """Включить маскирование для всего процесса. Повторный вызов только добавляет значения."""
    global _installed
    with _lock:
        _secrets.update(s for s in secrets if s and len(s) >= MIN_SECRET_LENGTH)
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202 — подпись фабрики logging
            record = previous(*args, **kwargs)
            scrub(record)
            return record

        logging.setLogRecordFactory(factory)
        _installed = True


def scrub(record: logging.LogRecord) -> None:
    """Замаскировать запись на месте: сообщение, аргументы, трассировку.

    Аргументы не склеиваются с сообщением: форматтер uvicorn разбирает их по местам.
    Заменяется только аргумент, в тексте которого нашёлся секрет, — исключение,
    переданное как `%s`, становится строкой без токена.
    """
    if isinstance(record.msg, str):
        record.msg = redact(record.msg)
    if isinstance(record.args, tuple):
        record.args = tuple(_clean(arg) for arg in record.args)
    elif isinstance(record.args, dict):
        record.args = {key: _clean(value) for key, value in record.args.items()}
    if record.exc_info and not record.exc_text:
        record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


def _clean(value: object) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value)
    cleaned = redact(text)
    return value if cleaned == text else cleaned
