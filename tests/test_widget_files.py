"""Файлы в виджете: скачивание одноразовой ссылкой и заказ файлом.

Раньше кнопки «Скачать Excel/Word» в виджете отвечали заглушкой «пришлю в
Telegram-боте», а анонимной сессии боту писать некуда. Теперь файл собирает то же
ядро, что для Telegram, и отдаётся ссылкой прямо в браузер.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from test_core_api import build  # noqa: E402
from test_order_core import HEADER, xlsx  # noqa: E402


def _buttons(body: dict) -> list[dict]:
    return [
        button
        for response in body["responses"]
        for row in response.get("actions", [])
        for button in row
    ]


def _with_shortlist(env, session_id: str) -> None:
    """Подбор состоялся: профиль помнит список на выгрузку.

    Сам подбор и заполнение shortlist покрывают тесты диалога; здесь проверяется
    доставка файла в веб-канал.
    """
    session = env.engine.session(session_id, "web")
    session.profile.shortlist = ["B1"]
    session.profile.export = "shortlist"


def test_export_after_selection_gives_one_time_link(tmp_path):
    env = build(tmp_path)
    session_id = env.client.post("/widget/session").json()["session_id"]
    _with_shortlist(env, session_id)

    body = env.client.post("/widget/action", json={"session_id": session_id, "action": "export:xlsx"}).json()

    url = next(b["url"] for b in _buttons(body) if (b.get("url") or "").startswith("/widget/download/"))
    file = env.client.get(url)
    assert file.status_code == 200
    assert file.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
    assert file.headers["content-disposition"].startswith("attachment")
    assert file.content.startswith(b"PK")  # настоящая книга xlsx, а не заглушка
    # Ссылка одноразовая: второй запрос уже не находит токен.
    assert env.client.get(url).status_code == 404


def test_export_docx_after_selection(tmp_path):
    env = build(tmp_path)
    session_id = env.client.post("/widget/session").json()["session_id"]
    _with_shortlist(env, session_id)

    body = env.client.post("/widget/action", json={"session_id": session_id, "action": "export:docx"}).json()

    url = next(b["url"] for b in _buttons(body) if (b.get("url") or "").startswith("/widget/download/"))
    file = env.client.get(url)
    assert file.status_code == 200
    assert file.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml")


def test_export_without_selection_says_nothing_to_save(tmp_path):
    env = build(tmp_path)
    session_id = env.client.post("/widget/session").json()["session_id"]

    body = env.client.post("/widget/action", json={"session_id": session_id, "action": "export:xlsx"}).json()

    assert any("Сохранять пока нечего" in r["text"] for r in body["responses"] if r["type"] == "text")
    assert not [b for b in _buttons(body) if (b.get("url") or "").startswith("/widget/download/")]


def test_upload_order_file_is_checked_like_in_telegram(tmp_path):
    env = build(tmp_path)
    session_id = env.client.post("/widget/session").json()["session_id"]
    content = xlsx([HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "908"]])

    response = env.client.post(
        "/widget/upload",
        data={"session_id": session_id},
        files={"file": ("заказ.xlsx", content, "application/octet-stream")},
    )

    assert response.status_code == 200, response.text
    [reply] = response.json()["responses"]
    assert "Проверил заказ «заказ.xlsx»: позиций 1" in reply["text"]
    actions = [button["action"] for row in reply["actions"] for button in row]
    # Дальше — корзина и файлы, как в чате бота: предзаказ в виджете идёт через корзину.
    assert "order_cart:1" in actions and "export:xlsx" in actions


def test_upload_unknown_item_is_reported(tmp_path):
    env = build(tmp_path)
    session_id = env.client.post("/widget/session").json()["session_id"]
    content = xlsx([HEADER, ["1", "", "Телескоп космический", "1", "1"]])

    response = env.client.post(
        "/widget/upload",
        data={"session_id": session_id},
        files={"file": ("заказ.xlsx", content, "application/octet-stream")},
    )

    assert response.status_code == 200, response.text
    [reply] = response.json()["responses"]
    assert "не найдено в каталоге" in reply["text"]


def test_upload_rejects_empty_file(tmp_path):
    env = build(tmp_path)
    session_id = env.client.post("/widget/session").json()["session_id"]

    response = env.client.post(
        "/widget/upload",
        data={"session_id": session_id},
        files={"file": ("пустой.xlsx", b"", "application/octet-stream")},
    )

    assert response.status_code == 400


def test_upload_rejects_bad_session(tmp_path):
    env = build(tmp_path)
    content = xlsx([HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "908"]])

    response = env.client.post(
        "/widget/upload",
        data={"session_id": "не-hex"},
        files={"file": ("заказ.xlsx", content, "application/octet-stream")},
    )

    assert response.status_code == 422


def test_session_returns_telegram_deeplink(tmp_path):
    env = build(tmp_path, telegram_bot_url="https://t.me/elti_bot")
    body = env.client.post("/widget/session").json()
    assert body["telegram_url"] == f"https://t.me/elti_bot?start={body['session_id']}"


def test_session_without_bot_url_has_no_telegram_link(tmp_path):
    env = build(tmp_path)
    body = env.client.post("/widget/session").json()
    assert body["telegram_url"] == ""
