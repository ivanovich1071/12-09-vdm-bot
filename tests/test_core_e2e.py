"""Финальная проверка ядра (NEXT-1…3): сквозной сценарий без Telegram, только через Core API.

ЗАДАЧА → ProcurementTask → норматив → подбор → спецификация → Excel / Word → загрузка →
сопоставление → цена → наличие → норматив → оценка → предзаказ → менеджер.

И отдельно — что ядро не тянет за собой каналы: ни модулей Telegram в импортах,
ни aiogram в загруженных модулях процесса.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from test_core_api import CUSTOMER, MANAGER_KEY, build, ok  # noqa: E402

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("fmt", ["xlsx", "docx"])
def test_user_task_to_confirmed_preorder_without_telegram(tmp_path, fmt):
    api = build(tmp_path)
    client = api.client
    session = ok(client.post("/api/sessions"), 201)
    headers = {"X-Session-Id": session["session_id"]}

    # Задача и норматив.
    task = ok(client.post("/api/procurement/tasks", json={"text": "Детский сад, по приказу 1057 пункт 1.5.1"}, headers=headers), 201)
    task_id = task["task_id"]
    assert (task["data"]["institution_type"], task["data"]["norm_document"], task["data"]["norm_item"]) == ("preschool", "order_1057", "1.5.1")

    # Подбор.
    selection = ok(client.post("/api/procurement/select", json={"task_id": task_id}, headers=headers))
    assert selection["data"]["norm"]["status"] == "RESOLVED" and selection["data"]["status"] == "FOUND"
    items = selection["data"]["items"]
    assert all(item["norm_status"] == "NORM_OK" and item["quantity_source"] == "norm" for item in items)
    ok(client.post(f"/api/procurement/tasks/{task_id}/choose", json={"product_ids": [i["product_id"] for i in items]}, headers=headers))

    # Спецификация и документ.
    spec = ok(client.post("/api/procurement/specification", json={"task_id": task_id}, headers=headers), 201)
    spec_version = spec["catalog_version"]
    document = client.get(f"/api/procurement/specifications/{spec['data']['id']}/export?format={fmt}", headers=headers)
    assert document.status_code == 200 and document.headers["X-Catalog-Version"] == spec_version

    # Загрузка готового документа как заказа и проверка.
    upload = client.post(
        f"/api/orders/upload?filename=spec.{fmt}&institution_type=preschool&norm_document=order_1057",
        content=document.content,
        headers={**headers, "Content-Type": "application/octet-stream"},
    )
    order = ok(upload, 201)
    evaluation = ok(client.post(f"/api/orders/{order['data']['id']}/evaluate", headers=headers))
    assert evaluation["catalog_version"] == spec_version
    for line in evaluation["data"]["items"]:
        assert line["match_status"] == "MATCHED_EXACT"
        assert line["price_status"] == "PRICE_OK"
        assert line["availability"] in ("AVAILABLE", "NOT_AVAILABLE")
        assert line["norm_status"] == "NORM_OK"
    assert evaluation["data"]["summary"]["current_amount"] == spec["data"]["totals"]["amount"]

    # Предзаказ, согласие, менеджер.
    preorder = ok(client.post("/api/preorders", json={"source": "uploaded_order", "source_id": order["data"]["id"]}, headers=headers), 201)
    preorder_id = preorder["data"]["id"]
    ok(client.post(f"/api/sessions/{session['session_id']}/consent", json={"granted": True}))
    sent = ok(client.post(f"/api/preorders/{preorder_id}/send", json={"customer": CUSTOMER}, headers=headers))
    assert sent["data"]["status"] == "SENT_TO_MANAGER"

    manager = {"X-Manager-Key": MANAGER_KEY, "X-Manager-Actor": "manager"}
    ok(client.post(f"/api/manager/preorders/{preorder_id}/review", headers=manager))
    confirmed = ok(client.post(f"/api/manager/preorders/{preorder_id}/confirm", json={"comment": "ok"}, headers=manager))
    history = [event["status"] for event in confirmed["data"]["history"]]
    assert history == [
        "DRAFT", "IMPORTED", "MATCHED", "PRICE_CHECKED", "READY_FOR_MANAGER",
        "SENT_TO_MANAGER", "MANAGER_REVIEW", "CONFIRMED",
    ]
    assert confirmed["data"]["catalog_version"] == spec_version


CORE_MODULES = (
    "procurement.service",
    "order_import.service",
    "preorder.service",
    "norms.selector",
    "documents.exporters",
    "core_api.facade",
    "core_api.http",
)


def test_core_imports_no_channel_modules():
    """В отдельном процессе: после импорта ядра ни aiogram, ни адаптеров, ни web в памяти нет."""
    code = (
        "import sys\n"
        f"for name in {CORE_MODULES!r}: __import__(name)\n"
        "loaded = [m for m in sys.modules if m.split('.')[0] in {'aiogram', 'adapters', 'web'}]\n"
        "print(','.join(loaded))\n"
    )
    # Окружение чистое: только то, без чего интерпретатор не стартует на Windows.
    env = {key: value for key, value in os.environ.items() if key in {"SYSTEMROOT", "PATH", "TEMP", "TMP"}}
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
