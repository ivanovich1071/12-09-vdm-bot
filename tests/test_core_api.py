"""NEXT-3 Core API: контракт ответа и ошибок, сессии, закупка, заказ, предзаказ, менеджер, ПДн."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from catalog.runtime import CatalogRuntime  # noqa: E402
from core.config import Settings  # noqa: E402
from core.dialog import DialogEngine  # noqa: E402
from core.errors import Unauthorized  # noqa: E402
from core.storage import Storage  # noqa: E402
from core_api.composition import build_core  # noqa: E402
from core_api.facade import CoreApi  # noqa: E402
from core_fixtures import norm_items, state  # noqa: E402
from norms.repository import FileNormRepository  # noqa: E402
from orders.service import OrderService  # noqa: E402
from orders.sinks import JsonlSink  # noqa: E402
from test_order_core import HEADER, xlsx  # noqa: E402
from web import app as web_app  # noqa: E402

KEY = "adapter-key"
MANAGER_KEY = "manager-key"
ENVELOPE = {"schema", "status", "request_id", "session_id", "task_id", "catalog_version", "norm_version", "data", "warnings", "errors"}
CUSTOMER = {"name": "Контактное лицо", "phone": "+7 900 000-00-00", "organization": "МБДОУ № 5"}


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, preorder) -> None:  # noqa: ANN001
        self.sent.append(preorder.id)


class SignedData:
    """Подписанные данные публичного клиента — проверяет адаптер канала, не API."""

    channel = "miniapp_test"

    def verify(self, value: str) -> str:
        if value != "signed:42":
            raise Unauthorized("Подпись не сошлась.", code="INVALID_CREDENTIALS")
        return "user-42"


def build(tmp_path: Path, **overrides) -> SimpleNamespace:
    settings = Settings(
        storage_path=str(tmp_path / "vdm.sqlite3"),
        uploads_dir=str(tmp_path / "uploads"),
        kb_path=str(tmp_path / "kb" / "products.jsonl"),
        preorders_dir=str(tmp_path / "preorders"),
        orders_jsonl_path=str(tmp_path / "orders.jsonl"),
        core_api_key=KEY,
        core_manager_key=MANAGER_KEY,
    )
    for name, value in overrides.items():
        setattr(settings, name, value)
    storage = Storage(settings.storage_path)
    engine = DialogEngine(CatalogRuntime(state()), storage, OrderService(storage, JsonlSink(tmp_path / "o.jsonl")), settings)
    notifier = Recorder()
    core = CoreApi(
        build_core(settings, engine, norms=FileNormRepository(norm_items()), notifier=notifier),
        {"test_signature": SignedData()},
    )
    client = TestClient(web_app.create_app(settings, engine=engine, core=core), raise_server_exceptions=False)
    return SimpleNamespace(client=client, core=core, engine=engine, storage=storage, runtime=engine.runtime, notifier=notifier, settings=settings)


@pytest.fixture
def api(tmp_path):
    return build(tmp_path)


def ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == ENVELOPE and body["status"] == "ok" and body["errors"] == []
    return body


def error(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"schema", "status", "request_id", "error"} and body["status"] == "error"
    assert set(body["error"]) == {"code", "message", "details"} and body["error"]["code"] == code
    return body["error"]


def session(api, **body) -> dict:
    return {"X-Session-Id": ok(api.client.post("/api/sessions", json=body), 201)["session_id"]}


def task(api, headers, text: str) -> str:
    return ok(api.client.post("/api/procurement/tasks", json={"text": text}, headers=headers), 201)["task_id"]


def chosen_spec(api, headers, text="Детский сад, по приказу 1057 пункт 1.5.1") -> tuple[str, dict]:
    task_id = task(api, headers, text)
    selection = ok(api.client.post("/api/procurement/select", json={"task_id": task_id}, headers=headers))["data"]
    ids = [item["product_id"] for item in selection["items"]]
    ok(api.client.post(f"/api/procurement/tasks/{task_id}/choose", json={"product_ids": ids}, headers=headers))
    return task_id, ok(api.client.post("/api/procurement/specification", json={"task_id": task_id}, headers=headers), 201)


# --- Контракт ----------------------------------------------------------------------------


def test_success_envelope(api):
    response = api.client.get("/api/catalog/status", headers={"X-Request-Id": "req-1"})
    body = ok(response)
    assert body["schema"] == "vdm.core.v1" and body["request_id"] == "req-1" and response.headers["X-Request-Id"] == "req-1"
    assert body["catalog_version"] == "2026-09-13-001" and body["norm_version"].startswith("norms-")
    assert body["data"]["products"] == 15 and body["data"]["norm_items_loaded"] is True


def test_error_contract(api):
    error(api.client.post("/api/procurement/tasks", json={"text": "школа"}), 401, "SESSION_NOT_FOUND")
    headers = session(api)
    details = error(api.client.post("/api/dialogue/message", json={"text": ""}, headers=headers), 422, "VALIDATION_ERROR")["details"]
    assert details["fields"][0]["location"] == ["body", "text"]
    error(api.client.post("/api/procurement/tasks", json={"text": "x", "owner": "someone"}, headers=headers), 422, "VALIDATION_ERROR")
    error(api.client.get("/api/nope"), 404, "NOT_FOUND")
    error(api.client.delete("/api/catalog/status"), 405, "METHOD_NOT_ALLOWED")


def test_existing_endpoints_keep_their_contract(api):
    health = api.client.get("/health").json()
    assert set(health) == {"status", "products", "catalog_version", "catalog_sha256", "llm", "order_sink"}
    assert api.client.get("/media/NOPE").json() == {"detail": "Снимок не собран"}
    widget = api.client.post("/widget/session").json()
    assert len(widget["session_id"]) == 32 and widget["responses"][0]["type"] == "text"
    assert "X-Request-Id" not in api.client.get("/health").headers


# --- Сессии --------------------------------------------------------------------------------


def test_sessions(api):
    anonymous = ok(api.client.post("/api/sessions"), 201)["data"]
    assert anonymous["origin"] == "anonymous" and anonymous["user_ref"] == anonymous["id"]
    assert anonymous["consent"]["active"] is False and "Редакция" in anonymous["consent"]["text"]

    error(api.client.post("/api/sessions", json={"channel": "bot", "user_ref": "777"}), 401, "API_KEY_REQUIRED")
    error(api.client.post("/api/sessions", json={"channel": "bot", "user_ref": "777"}, headers={"X-Core-Api-Key": "wrong"}), 401, "INVALID_API_KEY")
    adapter = ok(api.client.post("/api/sessions", json={"channel": "bot", "user_ref": "777"}, headers={"X-Core-Api-Key": KEY}), 201)
    again = ok(api.client.post("/api/sessions", json={"channel": "bot", "user_ref": "777"}, headers={"X-Core-Api-Key": KEY}), 201)
    assert adapter["session_id"] == again["session_id"] and adapter["data"]["origin"] == "adapter"

    signed = ok(api.client.post("/api/sessions", json={"credentials": {"type": "test_signature", "value": "signed:42"}}), 201)["data"]
    assert (signed["channel"], signed["user_ref"], signed["origin"]) == ("miniapp_test", "user-42", "test_signature")
    error(api.client.post("/api/sessions", json={"credentials": {"type": "test_signature", "value": "forged"}}), 401, "INVALID_CREDENTIALS")
    error(api.client.post("/api/sessions", json={"credentials": {"type": "unknown", "value": "x"}}), 400, "UNSUPPORTED_CREDENTIALS")

    ok(api.client.get(f"/api/sessions/{anonymous['id']}"))
    error(api.client.get("/api/sessions/" + "0" * 32), 401, "SESSION_NOT_FOUND")


def test_adapter_key_not_configured(tmp_path):
    api = build(tmp_path, core_api_key="", core_manager_key="")
    error(api.client.post("/api/sessions", json={"channel": "bot", "user_ref": "1"}, headers={"X-Core-Api-Key": "x"}), 503, "CORE_API_KEY_NOT_CONFIGURED")
    error(api.client.get("/api/manager/preorders", headers={"X-Manager-Key": "x"}), 503, "MANAGER_KEY_NOT_CONFIGURED")


# --- Диалог и корзина -----------------------------------------------------------------------


def test_dialogue_and_cart(api):
    headers = session(api)
    body = ok(api.client.post("/api/dialogue/message", json={"text": "Школа, кабинет информатики"}, headers=headers))
    types = [item["type"] for item in body["data"]["responses"]]
    assert "product_list" in types and body["catalog_version"] == "2026-09-13-001"
    listing = next(item for item in body["data"]["responses"] if item["type"] == "product_list")
    assert isinstance(listing["items"][0]["product"]["price"], int | None)

    ok(api.client.post("/api/dialogue/action", json={"action": "add:I1"}, headers=headers))
    ok(api.client.post("/api/cart/items", json={"product_id": "I3", "quantity": 2}, headers=headers))
    cart = ok(api.client.get("/api/cart", headers=headers))["data"]
    assert {item["product_id"]: item["quantity"] for item in cart["items"]} == {"I1": 1, "I3": 2}
    assert cart["total"] == 50000 + 18000

    spec = ok(api.client.post("/api/cart/specification", headers=headers), 201)
    assert spec["data"]["totals"]["amount"] == 68000
    assert {item["quantity_source"] for item in spec["data"]["items"]} == {"user"}
    ok(api.client.post("/api/cart/items", json={"product_id": "I3", "quantity": 0}, headers=headers))
    assert [i["product_id"] for i in ok(api.client.get("/api/cart", headers=headers))["data"]["items"]] == ["I1"]
    error(api.client.post("/api/cart/items", json={"product_id": "NOPE", "quantity": 1}, headers=headers), 404, "PRODUCT_NOT_FOUND")


# --- Закупка ----------------------------------------------------------------------------------


def test_procurement_over_http(api):
    headers = session(api)
    task_id = task(api, headers, "Школа, кабинет информатики")
    selection = ok(api.client.post("/api/procurement/select", json={"task_id": task_id}, headers=headers))
    assert selection["task_id"] == task_id and len(selection["data"]["items"]) == 3
    assert selection["catalog_version"] == "2026-09-13-001" and selection["data"]["has_more"]

    updated = ok(api.client.patch(f"/api/procurement/tasks/{task_id}", json={"fields": {"budget": 100000}}, headers=headers))
    assert updated["data"]["budget"] == 100000 and "owner" not in updated["data"]
    error(api.client.patch(f"/api/procurement/tasks/{task_id}", json={"fields": {"phone": "+7"}}, headers=headers), 400, "UNKNOWN_FIELD")
    error(api.client.post("/api/procurement/specification", json={"task_id": task_id, "items": [{"product_id": "NOPE"}]}, headers=headers), 400, "UNKNOWN_PRODUCT")


def test_specification_versions_and_export(api):
    headers = session(api)
    _, spec = chosen_spec(api, headers)
    spec_id = spec["data"]["id"]
    assert spec["catalog_version"] == spec["data"]["catalog_version"] == "2026-09-13-001"
    assert ok(api.client.get(f"/api/procurement/specifications/{spec_id}/check", headers=headers))["data"]["status"] == "CURRENT"

    exported = api.client.get(f"/api/procurement/specifications/{spec_id}/export?format=xlsx", headers=headers)
    assert exported.status_code == 200 and exported.content.startswith(b"PK")
    assert exported.headers["X-Catalog-Version"] == "2026-09-13-001" and spec_id in exported.headers["Content-Disposition"]
    assert api.client.get(f"/api/procurement/specifications/{spec_id}/export?format=docx", headers=headers).content.startswith(b"PK")
    error(api.client.get(f"/api/procurement/specifications/{spec_id}/export?format=pdf", headers=headers), 400, "UNSUPPORTED_FORMAT")

    api.runtime.replace(state("2026-09-13-002", B2={"price": 9000}))
    check = ok(api.client.get(f"/api/procurement/specifications/{spec_id}/check", headers=headers))["data"]
    assert check["status"] == "CATALOG_CHANGED" and check["changes"][0]["new"] == 9000
    revised = ok(api.client.post(f"/api/procurement/specifications/{spec_id}/revise", headers=headers), 201)
    assert revised["data"]["parent_id"] == spec_id and revised["catalog_version"] == "2026-09-13-002"
    error(api.client.post(f"/api/procurement/specifications/{spec_id}/revise", headers=headers), 409, "SPECIFICATION_NOT_DRAFT")


def test_resources_are_isolated_between_sessions(api):
    owner, stranger = session(api), session(api)
    task_id, spec = chosen_spec(api, owner)
    error(api.client.get(f"/api/procurement/tasks/{task_id}", headers=stranger), 404, "TASK_NOT_FOUND")
    error(api.client.get(f"/api/procurement/specifications/{spec['data']['id']}", headers=stranger), 404, "SPECIFICATION_NOT_FOUND")


def test_product_card(api):
    headers = session(api)
    task_id = task(api, headers, "Детский сад, спортзал")
    body = ok(api.client.get(f"/api/products/B2?task_id={task_id}", headers=headers))
    product = body["data"]
    assert (product["article"], product["price"], product["availability"]) == ("B2", 8164, "AVAILABLE")
    assert product["norm_mappings"][0]["item_code"] == "1.5.1.7" and product["rooms"] == ["спортивный зал"]
    assert body["catalog_version"] == "2026-09-13-001"
    error(api.client.get("/api/products/NOPE", headers=headers), 404, "PRODUCT_NOT_FOUND")


# --- Заказ и предзаказ ---------------------------------------------------------------------


def upload(api, headers, rows, **params):
    query = "&".join(f"{k}={v}" for k, v in {"filename": "order.xlsx", **params}.items())
    return api.client.post(f"/api/orders/upload?{query}", content=xlsx(rows), headers={**headers, "Content-Type": "application/octet-stream"})


def test_order_to_preorder_over_http(api):
    headers = session(api)
    order = ok(upload(api, headers, [HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "800"]], institution_type="preschool", norm_document="order_1057"), 201)
    order_id = order["data"]["id"]
    assert order["data"]["items"][0]["quantity"] == 2 and "storage_path" not in order["data"]["source_file"]
    evaluation = ok(api.client.post(f"/api/orders/{order_id}/evaluate", headers=headers))
    assert evaluation["data"]["items"][0]["price_status"] == "PRICE_CHANGED" and evaluation["catalog_version"] == "2026-09-13-001"
    assert ok(api.client.get(f"/api/orders/{order_id}/evaluation", headers=headers))["data"]["id"] == evaluation["data"]["id"]

    preorder = ok(api.client.post("/api/preorders", json={"source": "uploaded_order", "source_id": order_id}, headers=headers), 201)["data"]
    assert preorder["status"] == "READY_FOR_MANAGER" and preorder["is_final_order"] is False
    send = f"/api/preorders/{preorder['id']}/send"
    error(api.client.post(send, json={"customer": CUSTOMER}, headers=headers), 403, "CONSENT_REQUIRED")
    session_id = headers["X-Session-Id"]
    assert ok(api.client.post(f"/api/sessions/{session_id}/consent", json={"granted": True}))["data"]["consent"]["active"] is True
    error(api.client.post(send, json={"customer": {"name": "Без телефона"}}, headers=headers), 400, "CUSTOMER_INCOMPLETE")
    sent = ok(api.client.post(send, json={"customer": CUSTOMER}, headers=headers))["data"]
    assert sent["status"] == "SENT_TO_MANAGER" and api.notifier.sent == [preorder["id"]]

    history = ok(api.client.get("/api/history", headers=headers))["data"]
    assert history["orders"][0]["id"] == order_id and history["preorders"][0]["status"] == "SENT_TO_MANAGER"


def test_preorder_from_specification_and_errors(api):
    headers = session(api)
    _, spec = chosen_spec(api, headers)
    preorder = ok(api.client.post("/api/preorders", json={"source": "specification", "source_id": spec["data"]["id"]}, headers=headers), 201)
    assert preorder["data"]["totals"]["amount"] == spec["data"]["totals"]["amount"]
    error(api.client.post("/api/preorders", json={"source": "cart", "source_id": "x"}, headers=headers), 422, "VALIDATION_ERROR")
    order = ok(upload(api, headers, [HEADER, ["1", "", "Телескоп космический", "1", "1"]]), 201)["data"]["id"]
    error(api.client.post("/api/preorders", json={"source": "uploaded_order", "source_id": order}, headers=headers), 409, "EVALUATION_REQUIRED")
    error(upload(api, headers, [HEADER], filename="virus.exe"), 400, "UNSUPPORTED_FILE_TYPE")


def test_upload_size_limit(tmp_path):
    api = build(tmp_path, order_upload_max_mb=1)
    headers = session(api)
    big = b"PK" + b"0" * (2 * 1024 * 1024)
    response = api.client.post("/api/orders/upload?filename=big.xlsx", content=big, headers={**headers, "Content-Type": "application/octet-stream"})
    assert error(response, 400, "UPLOAD_REJECTED")["details"]["reason"] == "too_large"


# --- Менеджер ---------------------------------------------------------------------------


def sent_preorder(api) -> tuple[dict, str]:
    headers = session(api)
    _, spec = chosen_spec(api, headers)
    preorder = ok(api.client.post("/api/preorders", json={"source": "specification", "source_id": spec["data"]["id"]}, headers=headers), 201)["data"]
    ok(api.client.post(f"/api/sessions/{headers['X-Session-Id']}/consent", json={"granted": True}))
    ok(api.client.post(f"/api/preorders/{preorder['id']}/send", json={"customer": CUSTOMER}, headers=headers))
    return headers, preorder["id"]


def test_manager_flow(api):
    _, preorder_id = sent_preorder(api)
    # Заголовки HTTP — только ASCII: менеджер называется логином.
    manager = {"X-Manager-Key": MANAGER_KEY, "X-Manager-Actor": "maria.ivanova"}
    error(api.client.get("/api/manager/preorders"), 403, "INVALID_MANAGER_KEY")
    queue = ok(api.client.get("/api/manager/preorders", headers=manager))["data"]["preorders"]
    assert [p["id"] for p in queue] == [preorder_id]
    ok(api.client.post(f"/api/manager/preorders/{preorder_id}/review", headers=manager))
    ok(api.client.post(f"/api/manager/preorders/{preorder_id}/items/1/quantity", json={"quantity": 5}, headers=manager))
    confirmed = ok(api.client.post(f"/api/manager/preorders/{preorder_id}/confirm", json={"comment": "Согласовано"}, headers=manager))["data"]
    assert confirmed["status"] == "CONFIRMED" and confirmed["history"][-1]["actor"] == "maria.ivanova"
    error(api.client.post(f"/api/manager/preorders/{preorder_id}/reject", json={"reason": "поздно"}, headers=manager), 409, "PREORDER_TRANSITION_NOT_ALLOWED")
    ok(api.client.post("/api/manager/recodings", json={"old_sku": "OLD1", "new_sku": "B2"}, headers=manager), 201)
    kinds = {d["kind"] for d in ok(api.client.get("/api/manager/decisions", headers=manager))["data"]["decisions"]}
    assert kinds == {"quantity", "approval", "recoding"}
    assert ok(api.client.post("/api/manager/notifications/retry", headers=manager))["data"]["count"] == 0


# --- ПДн ------------------------------------------------------------------------------------


def test_personal_data_through_core_api(api):
    headers, preorder_id = sent_preorder(api)
    session_id = headers["X-Session-Id"]
    exported = ok(api.client.get(f"/api/sessions/{session_id}/data"))["data"]["data"]
    assert exported["preorders"][0]["customer"]["phone"] == CUSTOMER["phone"] and exported["consents"]

    ok(api.client.delete(f"/api/sessions/{session_id}/data"))
    manager = {"X-Manager-Key": MANAGER_KEY}
    kept = ok(api.client.get(f"/api/manager/preorders/{preorder_id}", headers=manager))["data"]
    assert kept["customer"] is None and kept["items"]
    error(api.client.get(f"/api/sessions/{session_id}"), 401, "SESSION_NOT_FOUND")


# --- Независимость от каналов ----------------------------------------------------------------


def test_core_api_has_no_channel_logic():
    root = Path(__file__).parents[1] / "src" / "core_api"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("aiogram", "adapters", "web")), f"{path.name}: {node.module}"
            if isinstance(node, ast.Import):
                assert not any(alias.name.startswith(("aiogram", "adapters", "web")) for alias in node.names)
            if isinstance(node, ast.Compare):
                constants = [c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                assert not {"telegram", "max", "web", "widget"} & set(constants), f"{path.name}: ветвление по каналу"
