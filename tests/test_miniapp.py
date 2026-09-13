"""NEXT-4: Telegram Mini App — страница поверх Core API и вход подписанными данными Telegram."""

from __future__ import annotations

import json
import re
import time

import pytest

pytest.importorskip("fastapi")

from adapters.telegram.miniapp_auth import TelegramInitDataVerifier, sign  # noqa: E402
from core.errors import Unauthorized  # noqa: E402
from core_api.facade import DOWNLOAD_TTL  # noqa: E402
from test_core_api import build, chosen_spec, error, ok  # noqa: E402
from test_order_core import HEADER, xlsx  # noqa: E402

TOKEN = "123456:TEST-TOKEN"
SCREENS = ("Главная", "AI-подбор", "Норматив", "Результаты", "Корзина", "Спецификация",
           "Загрузка заказа", "Проверка", "Ошибки", "Предзаказ", "История")


def init_data(user_id: int = 42, auth_date: int | None = None, **extra: str) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAHdF6IQ", "user": json.dumps({"id": user_id, "first_name": "Иван"}), **extra}
    return sign(TOKEN, fields)


@pytest.fixture
def api(tmp_path):
    return build(tmp_path, telegram_token=TOKEN)


def test_page_uses_only_core_api(api):
    response = api.client.get("/miniapp")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    html = response.text
    assert "telegram-web-app.js" in html and "X-Session-Id" in html and "telegram_init_data" in html
    assert all(f">{title}<" in html or f'"{title}"' in html for title in SCREENS)
    assert "card:" in html  # карточка товара — экран без вкладки
    # Все обращения к серверу идут через один помощник `api(метод, путь)` — и все в /api.
    paths = re.findall(r'api\("(?:GET|POST|PATCH|DELETE)", "([^"]+)"', html)
    assert paths and all(path.startswith("/api/") for path in paths), paths
    assert html.count("fetch(") == 1
    for forbidden in ("products.jsonl", "sqlite", "norm_items"):
        assert forbidden not in html


def test_init_data_verifier():
    verifier = TelegramInitDataVerifier(TOKEN)
    assert verifier.verify(init_data(42)) == "42"
    tampered = init_data(42).replace("%22id%22%3A+42", "%22id%22%3A+43")
    with pytest.raises(Unauthorized) as failure:
        verifier.verify(tampered)
    assert failure.value.code == "INVALID_INIT_DATA"
    with pytest.raises(Unauthorized) as failure:
        TelegramInitDataVerifier(TOKEN, clock=lambda: time.time() + 2 * 86400).verify(init_data(42))
    assert failure.value.code == "INIT_DATA_EXPIRED"
    with pytest.raises(Unauthorized):
        TelegramInitDataVerifier("other:token").verify(init_data(42))
    with pytest.raises(Unauthorized):
        verifier.verify(sign(TOKEN, {"auth_date": str(int(time.time()))}))
    with pytest.raises(Unauthorized):
        verifier.verify("")


def test_miniapp_user_shares_cart_with_bot(api):
    api.engine.handle_action("42", "telegram", "add:I1")
    body = ok(api.client.post("/api/sessions", json={"credentials": {"type": "telegram_init_data", "value": init_data(42)}}), 201)
    assert (body["data"]["channel"], body["data"]["user_ref"]) == ("telegram", "42")
    cart = ok(api.client.get("/api/cart", headers={"X-Session-Id": body["session_id"]}))["data"]
    assert [item["product_id"] for item in cart["items"]] == ["I1"]
    error(api.client.post("/api/sessions", json={"credentials": {"type": "telegram_init_data", "value": init_data(42) + "0"}}), 401, "INVALID_INIT_DATA")


def telegram_session(api, user_id: int) -> dict:
    body = {"credentials": {"type": "telegram_init_data", "value": init_data(user_id)}}
    return {"X-Session-Id": ok(api.client.post("/api/sessions", json=body), 201)["session_id"]}


def test_session_secret_never_goes_into_a_url(api):
    """Идентификатор сессии — пропуск. В адресе он осел бы в журналах сервера и прокси."""
    html = api.client.get("/miniapp").text
    assert "/api/sessions/" not in html
    assert html.count("state.session.id") == 1 and 'headers["X-Session-Id"] = state.session.id' in html

    headers = telegram_session(api, 42)
    current = ok(api.client.get("/api/session", headers=headers))["data"]
    assert current["user_ref"] == "42" and current["consent"]["active"] is False
    assert ok(api.client.post("/api/session/consent", json={"granted": True}, headers=headers))["data"]["consent"]["active"]
    assert ok(api.client.get("/api/session/data", headers=headers))["data"]["data"]["consents"]
    error(api.client.get("/api/session"), 401, "SESSION_NOT_FOUND")


def test_telegram_users_do_not_reach_each_other(api):
    alice, bob = telegram_session(api, 1001), telegram_session(api, 1002)
    ok(api.client.post("/api/cart/items", json={"product_id": "I1", "quantity": 2}, headers=alice))
    spec = ok(api.client.post("/api/cart/specification", headers=alice), 201)["data"]
    preorder = ok(api.client.post("/api/preorders", json={"source": "specification", "source_id": spec["id"]}, headers=alice), 201)["data"]
    content = xlsx([HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "908"]])
    order = ok(api.client.post("/api/orders/upload?filename=o.xlsx", content=content, headers=alice), 201)["data"]
    ok(api.client.post(f"/api/orders/{order['id']}/evaluate", headers=alice))

    assert ok(api.client.get("/api/session", headers=bob))["data"]["user_ref"] == "1002"
    assert ok(api.client.get("/api/cart", headers=bob))["data"]["items"] == []
    assert ok(api.client.get("/api/history", headers=bob))["data"] == {"tasks": [], "specifications": [], "orders": [], "preorders": []}
    error(api.client.get(f"/api/procurement/specifications/{spec['id']}", headers=bob), 404, "SPECIFICATION_NOT_FOUND")
    error(api.client.get(f"/api/preorders/{preorder['id']}", headers=bob), 404, "PREORDER_NOT_FOUND")
    error(api.client.get(f"/api/orders/{order['id']}", headers=bob), 404, "ORDER_NOT_FOUND")
    customer = {"customer": {"name": "Боб", "phone": "+7 900 000-00-00"}}
    for response in (
        api.client.post(f"/api/procurement/specifications/{spec['id']}/export-link?format=xlsx", headers=bob),
        api.client.get(f"/api/procurement/specifications/{spec['id']}/export?format=docx", headers=bob),
        api.client.post(f"/api/preorders/{preorder['id']}/send", json=customer, headers=bob),
        api.client.get(f"/api/orders/{order['id']}/evaluation", headers=bob),
        api.client.post("/api/preorders", json={"source": "uploaded_order", "source_id": order["id"]}, headers=bob),
        api.client.post("/api/preorders", json={"source": "specification", "source_id": spec["id"]}, headers=bob),
    ):
        assert response.status_code == 404, response.text
    assert api.notifier.sent == []
    assert [item["product_id"] for item in ok(api.client.get("/api/cart", headers=alice))["data"]["items"]] == ["I1"]


def test_download_link_expires(api):
    headers = telegram_session(api, 42)
    _, spec = chosen_spec(api, headers)
    link = ok(api.client.post(f"/api/procurement/specifications/{spec['data']['id']}/export-link?format=docx", headers=headers), 201)["data"]
    assert headers["X-Session-Id"] not in link["url"]
    token = link["url"].rsplit("/", 1)[1]
    deadline, *rest = api.core._downloads[token]
    api.core._downloads[token] = (deadline - DOWNLOAD_TTL - 1, *rest)
    error(api.client.get(link["url"]), 404, "DOWNLOAD_NOT_FOUND")


def test_download_link_is_single_use(api):
    headers = {"X-Session-Id": ok(api.client.post("/api/sessions"), 201)["session_id"]}
    _, spec = chosen_spec(api, headers)
    link = ok(api.client.post(f"/api/procurement/specifications/{spec['data']['id']}/export-link?format=xlsx", headers=headers), 201)["data"]
    assert link["url"].startswith("/api/downloads/") and link["expires_in"] == 300
    first = api.client.get(link["url"])
    assert first.status_code == 200 and first.content.startswith(b"PK") and first.headers["X-Catalog-Version"] == spec["catalog_version"]
    error(api.client.get(link["url"]), 404, "DOWNLOAD_NOT_FOUND")
    error(api.client.post(f"/api/procurement/specifications/{spec['data']['id']}/export-link?format=xlsx"), 401, "SESSION_NOT_FOUND")
