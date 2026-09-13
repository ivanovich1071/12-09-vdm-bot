"""NEXT-4.1: подготовка к живому Telegram.

Имя токена, секреты вне журнала, проверка перед запуском, граница адаптера и один
разговор, который ведут два процесса — бот и сервер Mini App.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

from adapters.telegram import preflight
from catalog.runtime import CatalogRuntime
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from core_fixtures import state
from observability import redact
from orders.service import OrderService
from orders.sinks import JsonlSink

ROOT = Path(__file__).parents[1]
# Похоже на токен Telegram, но собрано на лету: настоящих токенов в репозитории нет.
OLD_TOKEN = "1" * 10 + ":" + "x" * 35
NEW_TOKEN = "2" * 10 + ":" + "y" * 35
USER = "42"
CHANNEL = "telegram"


# --- Имя переменной ---------------------------------------------------------------------


def test_bot_token_is_read_only_from_the_new_name(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # .env разработчика не подмешивается
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN", OLD_TOKEN)

    settings = Settings.from_env()
    assert settings.telegram_token == "" and settings.ignored_env == ["TELEGRAM_TOKEN"]

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", NEW_TOKEN)
    settings = Settings.from_env()
    assert settings.telegram_token == NEW_TOKEN
    assert NEW_TOKEN in settings.secret_values and OLD_TOKEN not in settings.secret_values


# --- Секреты вне журнала ------------------------------------------------------------------


def test_token_inside_an_exception_argument_is_masked():
    """Так aiogram пишет обрыв связи: текст ошибки aiohttp с адресом запроса."""
    error = RuntimeError(f"502, message='Bad Gateway', url='https://api.telegram.org/bot{OLD_TOKEN}/getUpdates'")
    record = logging.LogRecord("adapters.telegram.bot", logging.WARNING, __file__, 1, "Связь потеряна (%s). Повтор через %s с.", (error, 5), None)

    redact.scrub(record)

    message = record.getMessage()
    assert OLD_TOKEN not in message and f"bot{redact.MASK}/getUpdates" in message
    assert record.args[1] == 5  # числа остаются числами: форматтеры разбирают аргументы по местам


def test_token_inside_a_traceback_is_masked():
    try:
        raise ValueError(f"https://api.telegram.org/file/bot{NEW_TOKEN}/documents/file_1.xlsx")
    except ValueError:
        record = logging.LogRecord("aiogram", logging.ERROR, __file__, 1, "Ход не отработал", None, sys.exc_info())

    redact.scrub(record)

    formatted = logging.Formatter().format(record)
    assert NEW_TOKEN not in formatted and "Traceback" in formatted and redact.MASK in formatted


def test_configured_keys_are_masked_even_when_they_do_not_look_like_tokens():
    assert redact.redact("X-Manager-Key: manager-key-0001", ["manager-key-0001", "short"]) == f"X-Manager-Key: {redact.MASK}"
    assert redact.redact("short stays", ["short"]) == "short stays"


def test_install_masks_records_of_every_logger(monkeypatch, caplog):
    original = logging.getLogRecordFactory()
    monkeypatch.setattr(redact, "_installed", False)
    monkeypatch.setattr(redact, "_secrets", set())
    try:
        redact.install(["core-api-key-0001"])
        redact.install(["core-api-key-0001"])  # повторный вызов не оборачивает фабрику дважды
        with caplog.at_level(logging.INFO):
            logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "10.0.0.1", "GET", "/x?key=core-api-key-0001", "1.1", 200)
            logging.getLogger("aiogram.event").warning("token %s", OLD_TOKEN)
    finally:
        logging.setLogRecordFactory(original)
    assert "core-api-key-0001" not in caplog.text and OLD_TOKEN not in caplog.text
    assert caplog.records[0].args[4] == 200


# --- Проверка перед запуском --------------------------------------------------------------


def pages(login_code: str = "INVALID_INIT_DATA"):
    site = {
        ("GET", "https://bot.example.org/miniapp"): (200, b'<script src="https://telegram.org/js/telegram-web-app.js"></script>'),
        ("GET", "https://bot.example.org/api/health"): (200, json.dumps({"status": "ok", "catalog_version": "v1", "products": 5936}).encode()),
        ("POST", "https://bot.example.org/api/sessions"): (401, json.dumps({"error": {"code": login_code}}).encode()),
    }
    return lambda method, url, body: site[(method, url)]


def test_preflight_checks_the_miniapp_server():
    url = "https://bot.example.org/miniapp"
    checks = preflight.miniapp_checks(url, pages())
    assert all(check.ok for check in checks), [check.line() for check in checks]
    assert "5936" in checks[2].detail

    without_token = pages("UNSUPPORTED_CREDENTIALS")
    assert not preflight.miniapp_checks(url, without_token)[-1].ok

    def offline(method, url, body):  # noqa: ANN001
        raise OSError("нет сети")

    assert not preflight.miniapp_checks(url, offline)[-1].ok
    assert not preflight.miniapp_checks("http://bot.example.org/miniapp")[0].ok
    assert not preflight.miniapp_checks("")[0].ok


def test_preflight_reports_legacy_name_and_malformed_token():
    checks = preflight.settings_checks(Settings(ignored_env=["TELEGRAM_TOKEN"]))
    assert [check.title for check in checks if not check.ok] == ["TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "Модель"]
    assert all(OLD_TOKEN not in check.line() for check in checks)

    pytest.importorskip("aiogram")
    [check] = asyncio.run(preflight.bot_checks("не-токен"))
    assert not check.ok and "BotFather" in check.detail


# --- Граница адаптера ---------------------------------------------------------------------

# Чего адаптер Telegram не импортирует: каталог, базы, нормы, подбор, заказы, агент.
FORBIDDEN = (
    "core.dialog", "core.app", "core.storage", "core.database", "catalog", "catalog_import",
    "catalog_versions", "procurement", "norms", "order_import", "preorder", "orders", "documents",
    "agent", "media", "sqlite3",
)


def imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


@pytest.mark.parametrize("name", ["bot.py", "gateway.py", "miniapp_auth.py", "preflight.py"])
def test_telegram_adapter_reaches_the_core_only_through_core_api(name):
    path = ROOT / "src" / "adapters" / "telegram" / name
    for module in imported_modules(path):
        assert not any(module == item or module.startswith(item + ".") for item in FORBIDDEN), f"{name}: {module}"
    text = path.read_text(encoding="utf-8")
    assert "products.jsonl" not in text and "norm_items" not in text


# --- Один разговор в двух процессах ---------------------------------------------------------


def two_processes(tmp_path: Path) -> tuple[DialogEngine, DialogEngine]:
    """Бот и сервер Mini App: у каждого свой движок и своё соединение, файл хранилища общий."""
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))

    def process() -> DialogEngine:
        storage = Storage(tmp_path / "shared.sqlite3")
        return DialogEngine(CatalogRuntime(state()), storage, OrderService(storage, JsonlSink(tmp_path / "o.jsonl")), settings)

    return process(), process()


def said(engine: DialogEngine) -> list[str]:
    return [item["content"] for item in engine.session(USER, CHANNEL).history if item["role"] == "user"]


def test_conversation_continued_in_the_other_process_is_not_lost(tmp_path):
    bot, miniapp = two_processes(tmp_path)
    bot.handle_text(USER, CHANNEL, "Здравствуйте")
    miniapp.handle_text(USER, CHANNEL, "нужен спортзал в детском саду, дети 3-6 лет")
    bot.handle_text(USER, CHANNEL, "а что есть из мячей?")

    expected = ["Здравствуйте", "нужен спортзал в детском саду, дети 3-6 лет", "а что есть из мячей?"]
    assert said(bot) == expected
    assert bot.session(USER, CHANNEL).profile.room == "спортивный зал"
    saved = bot.storage.load_dialog_state(USER, CHANNEL)
    assert [item["content"] for item in saved["history"] if item["role"] == "user"] == expected


def test_conversation_deleted_in_the_other_process_does_not_come_back(tmp_path):
    bot, miniapp = two_processes(tmp_path)
    bot.handle_text(USER, CHANNEL, "нужен спортзал в детском саду, дети 3-6 лет")
    miniapp.handle_text(USER, CHANNEL, "/delete_data")
    bot.handle_text(USER, CHANNEL, "мяч")

    session = bot.session(USER, CHANNEL)
    assert said(bot) == ["мяч"] and session.profile.room is None
    assert "спортзал" not in json.dumps(bot.storage.load_dialog_state(USER, CHANNEL), ensure_ascii=False)
