"""Проверка `initData` Telegram Mini App — вход публичного клиента в Core API.

Алгоритм — из документации Telegram (Validating data received via the Mini App):
секрет — HMAC-SHA256 токена бота с ключом «WebAppData», подпись — HMAC-SHA256
строки «ключ=значение», отсортированной по ключам, без поля `hash`.

`user_ref` — числовой идентификатор пользователя Telegram: тот же, что у бота. Mini App
и чат видят одну корзину и одно согласие.

Модуль чистый — без aiogram: его подключает веб-приложение, где бота нет.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from urllib.parse import parse_qsl

from core.errors import Unauthorized

CREDENTIALS_TYPE = "telegram_init_data"
# Сколько живут подписанные данные. Mini App открыт — данные те же; сутки с запасом.
MAX_AGE_SECONDS = 24 * 3600


class TelegramInitDataVerifier:
    channel = "telegram"

    def __init__(
        self, bot_token: str, max_age: int = MAX_AGE_SECONDS, clock: Callable[[], float] = time.time
    ) -> None:
        self._secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        self.max_age = max_age
        self.clock = clock

    def verify(self, value: str) -> str:
        pairs = dict(parse_qsl(value or "", keep_blank_values=True))
        received = pairs.pop("hash", "")
        check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
        expected = hmac.new(self._secret, check.encode(), hashlib.sha256).hexdigest()
        if not received or not hmac.compare_digest(expected, received):
            raise Unauthorized("Подпись данных Telegram не сошлась.", code="INVALID_INIT_DATA")
        auth_date = pairs.get("auth_date", "")
        if not auth_date.isdigit() or self.clock() - int(auth_date) > self.max_age:
            raise Unauthorized("Данные Telegram устарели — откройте приложение заново.", code="INIT_DATA_EXPIRED")
        try:
            user_id = json.loads(pairs.get("user", "{}")).get("id")
        except (json.JSONDecodeError, AttributeError):
            user_id = None
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise Unauthorized("В данных Telegram нет пользователя.", code="INVALID_INIT_DATA")
        return str(user_id)


def sign(bot_token: str, fields: dict[str, str]) -> str:
    """Подписать поля так же, как Telegram, — для тестов и локальной проверки."""
    from urllib.parse import urlencode

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})
