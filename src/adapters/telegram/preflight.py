"""Проверка перед запуском Telegram-бота — без опроса и без вывода секретов.

    python run.py telegram --check

- токен задан под именем `TELEGRAM_BOT_TOKEN`, Telegram его принимает, какой это бот;
- webhook не установлен: бот работает опросом, и webhook забрал бы у него сообщения;
- модель настроена: без неё консультант и продажник не работают, бот отвечает поиском;
- Mini App: адрес https, страница открывается, за ней отвечает Core API, и сервер
  проверяет вход по токену бота.

Токен не печатается: ни в строке результата, ни в тексте ошибки, ни адресом webhook.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from core.config import Settings

# Метод, адрес, тело — ответ: код и байты.
Fetch = Callable[[str, str, bytes | None], tuple[int, bytes]]
TIMEOUT = 15


@dataclass(frozen=True)
class Check:
    title: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "ok" if self.ok else "!!"
        return f"[{mark}] {self.title}" + (f" — {self.detail}" if self.detail else "")


def settings_checks(settings: Settings) -> list[Check]:
    checks = []
    if "TELEGRAM_TOKEN" in settings.ignored_env:
        checks.append(
            Check(
                "TELEGRAM_TOKEN",
                False,
                "прежнее имя больше не читается: токен нового бота — в TELEGRAM_BOT_TOKEN, "
                "старую строку удалите из .env",
            )
        )
    token = bool(settings.telegram_token)
    checks.append(Check("TELEGRAM_BOT_TOKEN", token, "задан" if token else "не задан"))
    checks.append(
        Check(
            "Модель",
            settings.llm_enabled,
            f"провайдер {settings.llm_provider}"
            if settings.llm_enabled
            else "ключа нет — консультант и продажник не работают, бот отвечает поиском",
        )
    )
    return checks


async def bot_checks(token: str) -> list[Check]:
    from aiogram import Bot
    from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
    from aiogram.utils.token import TokenValidationError

    from adapters.telegram.bot import _session

    try:
        bot = Bot(token, session=_session())
    except TokenValidationError:
        return [Check("Токен", False, "не похож на токен от @BotFather")]
    try:
        me = await bot.get_me()
        hook = await bot.get_webhook_info()
    except TelegramUnauthorizedError:
        return [Check("Токен", False, "Telegram его не принял — скопируйте токен нового бота заново")]
    except TelegramNetworkError:
        return [Check("Связь с Telegram", False, "api.telegram.org не отвечает")]
    finally:
        await bot.session.close()
    return [
        Check("Токен", True, f"бот @{me.username}, id {me.id}"),
        # Адрес webhook не печатаем: в нём бывает секретный путь.
        Check(
            "Webhook",
            not hook.url,
            "не установлен — опрос получит сообщения"
            if not hook.url
            else "установлен: опрос не получит сообщений, снимите его методом deleteWebhook",
        ),
    ]


def miniapp_checks(url: str, fetch: Fetch | None = None) -> list[Check]:
    fetch = fetch or _fetch
    if not url:
        return [Check("Mini App", False, "TELEGRAM_MINIAPP_URL не задан — кнопка «Приложение» не появится")]
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        return [Check("Mini App: адрес", False, "нужен публичный https://…/miniapp — Telegram не открывает http")]
    checks = [Check("Mini App: адрес", True, url)]

    def endpoint(path: str) -> str:
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    try:
        status, body = fetch("GET", url, None)
        checks.append(
            Check("Mini App: страница", status == 200 and b"telegram-web-app.js" in body, f"HTTP {status}")
        )
        status, body = fetch("GET", endpoint("/api/health"), None)
        health = _json(body)
        checks.append(
            Check(
                "Mini App: Core API",
                status == 200 and health.get("status") == "ok",
                f"HTTP {status}, каталог {health.get('catalog_version', '?')}, товаров {health.get('products', '?')}",
            )
        )
        # Заведомо неверная подпись: сервер с токеном бота отвечает INVALID_INIT_DATA,
        # сервер без токена — UNSUPPORTED_CREDENTIALS. Сессия при этом не создаётся.
        probe = json.dumps({"credentials": {"type": "telegram_init_data", "value": "hash=0"}}).encode()
        status, body = fetch("POST", endpoint("/api/sessions"), probe)
        code = _json(body).get("error", {}).get("code")
        checks.append(
            Check(
                "Mini App: вход по токену бота",
                code in ("INVALID_INIT_DATA", "INIT_DATA_EXPIRED"),
                "сервер проверяет подпись Telegram"
                if code in ("INVALID_INIT_DATA", "INIT_DATA_EXPIRED")
                else f"сервер ответил {code or status}: на нём не задан TELEGRAM_BOT_TOKEN",
            )
        )
    except OSError as exc:
        checks.append(Check("Mini App: сервер", False, f"не отвечает ({type(exc).__name__})"))
    return checks


async def run(settings: Settings, fetch: Fetch | None = None) -> list[Check]:
    checks = settings_checks(settings)
    if settings.telegram_token:
        checks += await bot_checks(settings.telegram_token)
    checks += miniapp_checks(settings.telegram_miniapp_url, fetch)
    return checks


async def main() -> int:
    from observability import redact

    settings = Settings.from_env()
    redact.install(settings.secret_values)
    checks = await run(settings)
    for check in checks:
        print(redact.redact(check.line()))
    failed = sum(not check.ok for check in checks)
    print("\nГотово к запуску: python run.py telegram" if not failed else f"\nНе готово: замечаний {failed}")
    return 1 if failed else 0


def _fetch(method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
    headers = {"User-Agent": "vdm-bot-preflight"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)  # noqa: S310 — адрес проверен: только https
    try:
        with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
