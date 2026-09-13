"""EPIC 4, этап 3: одно состояние каталога, закрепление версии на ход, горячая замена.

Снимки версий и указатель — во временной папке; модель и сеть не нужны.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from pathlib import Path

import pytest

from catalog.current import (
    CatalogPointerError,
    Pointer,
    resolve_catalog,
    snapshot_path,
    write_pointer,
)
from catalog.models import Product
from catalog.runtime import CatalogRuntime, CatalogRuntimeState
from catalog.search import CatalogIndex
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from norms import items as norm_items
from orders.service import OrderService
from orders.sinks import JsonlSink

ROOT = Path(__file__).resolve().parents[1]


def record(code: str, price: int, root: str = "ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА") -> dict:
    return {
        "sku_1c": code,
        "name": f"Мяч {code}",
        "url": None,
        "short_url": None,
        "price": price,
        "currency": "RUB",
        "in_stock": 3,
        "category_paths": [[root, "12.04 Мячи"]],
        "description": "",
        "kit_contents": [],
        "norms": [],
        "bitrix_id": None,
    }


def publish(kb_dir: Path, version: str, *records: dict) -> str:
    """Снимок версии и указатель на него — как после применения."""
    import hashlib

    path = snapshot_path(kb_dir, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    write_pointer(kb_dir, Pointer(version, sha))
    return sha


@pytest.fixture(autouse=True)
def no_norm_texts(monkeypatch):
    monkeypatch.setattr(norm_items, "load", lambda *_a, **_kw: {})


@pytest.fixture
def kb(tmp_path) -> Path:
    kb_dir = tmp_path / "kb"
    publish(kb_dir, "V1", record("S1", 100), record("S2", 200, root="ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ"))
    return kb_dir / "products.jsonl"


def make_engine(tmp_path: Path, runtime: CatalogRuntime | CatalogIndex) -> DialogEngine:
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    return DialogEngine(runtime, storage, OrderService(storage, JsonlSink(tmp_path / "o.jsonl")), settings)


# --- Одно состояние ------------------------------------------------------------


def test_engine_keeps_index_catalog_and_roots_compatible(tmp_path):
    index = CatalogIndex([Product.from_dict(record("S1", 100))])
    engine = make_engine(tmp_path, index)

    assert engine.index is index and engine.catalog is engine.catalog
    assert engine.catalog.get_by_article("S1") is engine.index.get("S1")
    assert engine.roots == ["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"] and engine.catalog_version is None

    replaced = CatalogIndex([Product.from_dict(record("S9", 900, root="ШКОЛА"))])
    old_state = engine.runtime.current()
    engine.index = replaced

    # Индекс, сервис и разделы сменились вместе, прежнее состояние не тронуто.
    assert engine.index is replaced and engine.catalog.get_by_article("S9") is not None
    assert engine.roots == ["ШКОЛА"]
    assert old_state.index is index and old_state.roots == ("ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА",)
    with pytest.raises(dataclasses.FrozenInstanceError):
        old_state.index = replaced


def test_open_reads_pointer_and_verifies_snapshot(kb):
    runtime = CatalogRuntime.open(kb)

    assert (runtime.state.version, len(runtime.state.index.products)) == ("V1", 2)
    assert runtime.state.snapshot_path == snapshot_path(kb.parent, "V1")


def test_open_refuses_broken_pointer(kb):
    write_pointer(kb.parent, Pointer("V2", "0" * 64))
    with pytest.raises(CatalogPointerError, match="снимка"):
        CatalogRuntime.open(kb)

    publish(kb.parent, "V2", record("S1", 120))
    snapshot_path(kb.parent, "V2").write_text(json.dumps(record("S1", 999)) + "\n", encoding="utf-8")
    with pytest.raises(CatalogPointerError, match="повреждён"):
        CatalogRuntime.open(kb)


def test_pointer_appearing_after_legacy_start_is_picked_up(tmp_path):
    kb = tmp_path / "kb" / "products.jsonl"
    kb.parent.mkdir()
    kb.write_text(json.dumps(record("S1", 100)) + "\n", encoding="utf-8")
    runtime = CatalogRuntime.open(kb)
    assert runtime.state.version is None

    publish(kb.parent, "baseline", record("S1", 100))

    assert runtime.refresh(wait=True) and runtime.state.version == "baseline"


# --- Одна версия на ход --------------------------------------------------------


def test_turn_sees_one_version_even_if_new_one_is_applied_mid_turn(tmp_path, kb, monkeypatch):
    runtime = CatalogRuntime.open(kb)
    engine = make_engine(tmp_path, runtime)
    seen: list[tuple] = []
    switch = {"now": True}

    def turn(_user, _channel, _action):
        seen.append((engine.catalog_version, engine.index.get("S1").price, engine.catalog.get_product("S1").price))
        if switch.pop("now", False):
            publish(kb.parent, "V2", record("S1", 120))
            assert runtime.refresh(wait=True)  # новая версия применена посреди хода
        seen.append((engine.catalog_version, engine.index.get("S1").price, engine.catalog.get_product("S1").price))
        return []

    monkeypatch.setattr(engine, "_handle_action", turn)

    engine.handle_action("u1", "telegram", "cart")
    assert runtime.current().version == "V2"
    engine.handle_action("u1", "telegram", "cart")

    assert seen == [("V1", 100, 100), ("V1", 100, 100), ("V2", 120, 120), ("V2", 120, 120)]


def test_old_state_serves_while_new_one_builds(tmp_path, kb, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    calls: list[str | None] = []

    def slow(snapshot):
        calls.append(snapshot.version)
        started.set()
        assert release.wait(5)
        return CatalogRuntimeState.load(snapshot)

    runtime = CatalogRuntime(CatalogRuntimeState.load(resolve_catalog(kb)), kb, loader=slow)
    engine = make_engine(tmp_path, runtime)
    prices: list[int] = []
    monkeypatch.setattr(engine, "_handle_text", lambda *_a: prices.append(engine.index.get("S1").price) or [])

    publish(kb.parent, "V2", record("S1", 120))
    engine.handle_text("u1", "web", "мяч")  # запускает фоновую сборку
    assert started.wait(5)
    engine.handle_text("u1", "web", "мяч")  # сборка идёт — вторую не запускает
    assert runtime.current().version == "V1"

    release.set()
    runtime.join(5)
    engine.handle_text("u1", "web", "мяч")

    assert prices == [100, 100, 120]
    assert calls == ["V2"] and runtime.reloads == 1


def test_failed_reload_keeps_current_state_and_waits_for_new_pointer(kb):
    runtime = CatalogRuntime.open(kb)
    publish(kb.parent, "V2", record("S1", 120))
    snapshot_path(kb.parent, "V2").write_text(json.dumps(record("S1", 999)) + "\n", encoding="utf-8")

    assert not runtime.refresh(wait=True)
    assert runtime.state.version == "V1" and runtime.state.index.get("S1").price == 100
    assert runtime._pending() is None  # ту же пару «версия, sha256» повторно не собирает

    publish(kb.parent, "V3", record("S1", 130))
    assert runtime.refresh(wait=True) and runtime.state.index.get("S1").price == 130


def test_watcher_reloads_without_restart(kb):
    runtime = CatalogRuntime.open(kb)
    runtime.start_watching(0.05)
    try:
        publish(kb.parent, "V2", record("S1", 120))
        deadline = time.monotonic() + 5
        while runtime.current().version != "V2" and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        runtime.stop()
    assert runtime.current().version == "V2" and runtime.current().index.get("S1").price == 120


def test_widget_reports_version_and_reloads_on_next_message(tmp_path, kb, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    assert fastapi
    from fastapi.testclient import TestClient

    from web import app as web_app

    settings = Settings(
        kb_path=str(kb),
        storage_path=str(tmp_path / "vdm.sqlite3"),
        orders_jsonl_path=str(tmp_path / "orders.jsonl"),
        orders_xlsx_dir=str(tmp_path / "orders"),
        dialog_log_enabled=False,
        media_enabled=False,
        media_dir=str(tmp_path / "media"),
        cloudru_api_key="",
        openrouter_api_key="",
    )
    engines: list[DialogEngine] = []
    real = web_app.build_engine
    monkeypatch.setattr(web_app, "build_engine", lambda s, **kw: engines.append(real(s, **kw)) or engines[-1])
    client = TestClient(web_app.create_app(settings))

    body = client.get("/health").json()
    assert (body["catalog_version"], body["products"]) == ("V1", 2)

    publish(kb.parent, "V2", record("S1", 120))
    session = client.post("/widget/session", json={}).json()["session_id"]
    client.post("/widget/message", json={"session_id": session, "text": "мяч"})
    engines[0].runtime.join(5)

    body = client.get("/health").json()
    assert (body["catalog_version"], body["products"]) == ("V2", 1)


def test_pointer_replace_retries_windows_permission_error(tmp_path, monkeypatch):
    """На Windows `os.replace` отказывает, пока указатель открыт читателем: короткий повтор."""
    import os

    from catalog import current

    real = os.replace
    attempts = {"n": 0}

    def busy(source, target):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("файл занят")
        real(source, target)

    monkeypatch.setattr(current.os, "replace", busy)
    monkeypatch.setattr(current, "_REPLACE_PAUSE", 0)
    current.write_pointer(tmp_path, Pointer("V1", "a" * 64))

    assert attempts["n"] == 3 and current.read_pointer(tmp_path) == Pointer("V1", "a" * 64)
    assert list(tmp_path.glob(".current.*.tmp")) == []


# --- Единая точка чтения каталога ----------------------------------------------


def test_no_direct_catalog_readers_outside_resolver():
    """Каталог читают только через резолвер: прямые загрузчики остаются лишь в repository.py."""
    forbidden = re.compile(r"\b(load_index|load_products|from_path)\(|\bsync_to_kb\(")
    allowed = {
        "src/catalog/repository.py": {"load_index", "load_products", "from_path"},
        "src/media/sync.py": {"sync_to_kb"},
        # Legacy-переливка без указателя — только внутри _sync_media.
        "run.py": {"sync_to_kb"},
    }
    offenders = []
    for path in [ROOT / "run.py", *sorted((ROOT / "src").rglob("*.py"))]:
        relative = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in forbidden.finditer(line):
                name = (match.group(1) or "sync_to_kb")
                if name not in allowed.get(relative, set()):
                    offenders.append(f"{relative}:{number}: {line.strip()}")
    assert offenders == []

    run_py = (ROOT / "run.py").read_text(encoding="utf-8")
    assert run_py.count("sync_to_kb(") == 1
