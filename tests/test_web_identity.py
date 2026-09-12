"""R1: посетитель виджета не действует от имени пользователя другого канала.

Корзина, согласие и удаление данных в хранилище привязаны к идентификатору
пользователя без канала. Идентификатор Telegram — число из 9–10 цифр. Пока
`/widget/message` и `/widget/action` принимали любую строку от восьми символов,
этим числом можно было очистить чужую корзину, дать за человека согласие на
обработку ПДн или стереть его данные. Формат hex32 проверялся только при
открытии сессии.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from catalog.models import Product  # noqa: E402
from catalog.search import CatalogIndex  # noqa: E402
from core.config import Settings  # noqa: E402
from core.dialog import DialogEngine  # noqa: E402
from core.storage import Storage  # noqa: E402
from orders.service import OrderService  # noqa: E402
from orders.sinks import JsonlSink  # noqa: E402
from web import app as web_app  # noqa: E402

TELEGRAM = "telegram"
# Так выглядит настоящий идентификатор пользователя Telegram.
TELEGRAM_USER = "123456789"


@pytest.fixture
def engine(tmp_path):
    index = CatalogIndex(
        [
            Product.from_dict(
                {
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
            )
        ]
    )
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    return DialogEngine(
        index, storage, OrderService(storage, JsonlSink(tmp_path / "o.jsonl")), settings
    )


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setattr(web_app, "build_engine", lambda _settings, **_kw: engine)
    return TestClient(web_app.create_app(engine.settings))


def test_widget_cannot_mutate_other_user_cart(client, engine):
    engine.handle_action(TELEGRAM_USER, TELEGRAM, "add:S1")

    for action in ("clear", "del:S1", "inc:S1", "dec:S1", "add:S1"):
        response = client.post(
            "/widget/action", json={"session_id": TELEGRAM_USER, "action": action}
        )
        assert response.status_code == 422, action

    cart = engine.storage.load_cart(TELEGRAM_USER)
    assert cart.count == 1


def test_widget_cannot_grant_consent_for_other_user(client, engine):
    engine.handle_action(TELEGRAM_USER, TELEGRAM, "add:S1")
    engine.handle_action(TELEGRAM_USER, TELEGRAM, "checkout")

    response = client.post(
        "/widget/action", json={"session_id": TELEGRAM_USER, "action": "consent_yes"}
    )

    assert response.status_code == 422
    assert engine.storage.active_consent(TELEGRAM_USER) is None


def test_widget_cannot_delete_other_user_data(client, engine):
    engine.handle_text(TELEGRAM_USER, TELEGRAM, "нужен мяч для детского сада")
    engine.handle_action(TELEGRAM_USER, TELEGRAM, "add:S1")
    engine.handle_action(TELEGRAM_USER, TELEGRAM, "checkout")
    engine.handle_action(TELEGRAM_USER, TELEGRAM, "consent_yes")

    response = client.post(
        "/widget/message", json={"session_id": TELEGRAM_USER, "text": "/delete_data"}
    )

    assert response.status_code == 422
    assert engine.storage.active_consent(TELEGRAM_USER) is not None
    assert not engine.storage.load_cart(TELEGRAM_USER).is_empty
    assert engine.storage.load_dialog_state(TELEGRAM_USER, TELEGRAM) is not None


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/widget/message", {"text": "/cart"}),
        ("/widget/message", {"text": "/my_data"}),
        ("/widget/action", {"action": "cart"}),
        ("/widget/action", {"action": "checkout"}),
        ("/widget/action", {"action": "confirm_order"}),
    ],
)
@pytest.mark.parametrize(
    "session_id",
    [
        TELEGRAM_USER,
        "12345678",
        "не-hex-идентификатор-посетителя",
        "12345678-1234-1234-1234-123456789012",
        "A" * 32,
        "0" * 31,
        "0" * 33,
    ],
)
def test_widget_rejects_identifier_not_issued_by_server(client, path, payload, session_id):
    response = client.post(path, json={"session_id": session_id, **payload})
    assert response.status_code == 422


def test_identifier_issued_by_server_still_works(client, engine):
    session_id = client.post("/widget/session").json()["session_id"]

    added = client.post("/widget/action", json={"session_id": session_id, "action": "add:S1"})
    replied = client.post("/widget/message", json={"session_id": session_id, "text": "/cart"})

    assert added.status_code == 200 and replied.status_code == 200
    assert engine.storage.load_cart(session_id).count == 1
    assert "order" in [item["type"] for item in replied.json()["responses"]]
