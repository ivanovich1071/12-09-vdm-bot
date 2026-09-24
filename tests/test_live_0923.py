"""Регрессии живого прогона 23.09 (`data/qa/live-0923-manual`, 25 сценариев).

Каждый тест — находка прогона: составная реплика, потерянный субъект экспорта,
имена файлов китов 838, шаблонные хвосты, карточки мимо списка, возраст.
"""

from __future__ import annotations

import pytest

from agent.agent import _about_deadline_only
from agent.routing import _also_asks_other, _asks_export
from core.ui import Message
from test_agent import (  # noqa: F401 — engine: фикстура
    CHANNEL,
    USER,
    FakeCloudRu,
    answer,
    attach,
    client,
    engine,
)


def texts(replies) -> str:  # noqa: ANN001
    return "\n".join(reply.text for reply in replies if isinstance(reply, Message))


def actions(replies) -> list[str]:  # noqa: ANN001
    return [
        button.action
        for reply in replies
        for row in (getattr(reply, "keyboard", None).rows if getattr(reply, "keyboard", None) else [])
        for button in row
    ]


def kit() -> dict:
    """Комплектация раздела 2.20 приказа 838 — как её оставляет в профиле консультант."""
    return {
        "document": "order_838",
        "code": "2.20",
        "title": "Кабинет технологии",
        "positions": [
            {"code": "2.20.1", "title": "Станок фрезерный", "quantity": "1 шт."},
            {"code": "2.20.2", "title": "Верстак", "quantity": "2 шт."},
        ],
    }


# --- Пакет A: составная реплика доезжает целиком ---------------------------------------------


TURN9 = "Тогда давайте ваши. Пришлите спецификацию Excel, счёт и срок поставки"


def test_excel_invoice_deadline_in_one_turn(engine):  # noqa: F811
    """Ход 9 из десяти диалогов: «Excel + счёт + срок» отвечал только файлом, срок — после повтора."""
    engine.session(USER, CHANNEL).profile.remember_kit(kit())
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, TURN9)
    said = texts(replies)
    assert "Собираю файл в Excel" in said, "файл должен уйти сразу — формат назван"
    assert "менеджер" in said.lower(), "срок отвечает менеджер — нота в том же ходу"
    assert "Счёт на оплату" in said, "счёт из той же реплики не должен выпадать"
    assert "export:xlsx" in actions(replies) and "manager" in actions(replies)
    assert not cloud.requests, "составная реплика файла и срока не стоит вызова модели"


def test_file_and_invoice_without_deadline(engine):  # noqa: F811
    """«Пришлите файл и счёт» без слова «срок»: счёт всё равно отвечен нотой."""
    engine.session(USER, CHANNEL).profile.remember_kit(kit())
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, "Пришлите спецификацию Excel и счёт")
    said = texts(replies)
    assert "Собираю файл в Excel" in said
    assert "Счёт на оплату" in said
    assert "export:xlsx" in actions(replies)
    assert not cloud.requests


def test_goods_and_file_both_answered(engine):  # noqa: F811
    """Сц. 5/9: «добавьте … и пришлите файл» — экспорт забирал реплику, добавление терялось."""
    engine.session(USER, CHANNEL).profile.remember_kit(kit())
    with FakeCloudRu([answer("Коврики: в наличии три расцветки.")]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, "Добавьте коврики, посмотрите что есть, и пришлите файл")

    said = texts(replies)
    assert "Коврики" in said, "подбор не должен пропасть за файлом"
    assert "файлом" in said and "В каком виде?" in said, "файл — префиксом того же хода"


def test_goods_and_deadline_both_answered(engine):  # noqa: F811
    """Сц. 7: «цвет берёза. срок поставки» — цвет терялся, отвечал только шаблон срока."""
    engine.session(USER, CHANNEL).profile.remember_kit(kit())
    with FakeCloudRu([answer("Цвет берёза есть у трёх моделей станков.")]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, "Цвет берёза. Какой срок поставки?")

    said = texts(replies)
    assert "берёза" in said.lower(), "ответ о товаре не должен съедаться шаблоном срока"
    assert "менеджер" in said.lower(), "срок отвечает менеджер — нота в конце"


def test_pure_deadline_still_answers_by_template(engine):  # noqa: F811
    """Чистый вопрос о сроке отвечает шаблоном ядра, без вызова модели."""
    with FakeCloudRu([]) as cloud:
        attach(engine, client(cloud.base_url))
        replies = engine.handle_text(USER, CHANNEL, "Какой срок поставки?")

    said = texts(replies)
    assert "менеджер" in said.lower()
    assert "manager" in actions(replies), "шаблон зовёт нажать кнопку — кнопка должна быть"
    assert engine.session(USER, CHANNEL).route.get("fallback") == "deadline"
    assert not cloud.requests, "шаблон срока не должен стоить вызова модели"


@pytest.mark.parametrize(
    ("said", "only"),
    [
        ("Какой срок поставки?", True),
        ("хорошо, скачала. а сколько по времени от оформления до счёта?", True),
        ("Спасибо! Когда будет отгрузка?", True),
        ("Нужно успеть до 10 декабря. Возрасты групп уточните, что обязательно сейчас?", False),
        ("Цвет берёза. Срок поставки какой?", False),
        ("Пришлите единый стандарт комплектации. И график поставок по этапам?", False),
    ],
)
def test_deadline_dominance(said: str, only: bool):
    assert _about_deadline_only(said) is only


@pytest.mark.parametrize(
    ("said", "export", "other"),
    [
        (TURN9, True, False),
        ("сохраните спецификацию в Excel", True, False),
        ("Добавьте коврики, посмотрите что есть, и пришлите файл", True, True),
        ("Оформите заказ и пришлите файл спецификации", True, True),
    ],
)
def test_export_hijack_guard(said: str, export: bool, other: bool):
    """Экспорт забирает реплику целиком только когда в ней нет других просьб."""
    assert _asks_export(said) is export
    assert _also_asks_other(said) is other
