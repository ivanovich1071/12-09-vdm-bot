"""EPIC 4, этап 4: старые источники каталога — ingest, media, реестр — через версии.

Команды `run.py` вызываются как функции; данные — во временной папке.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalog.current import file_sha256, read_pointer, snapshot_path
from core.config import Settings
from core.storage import Storage
from test_catalog_versions import BASE, make_env

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("run_cli", ROOT / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(tmp_path):
    environment = make_env(tmp_path)
    yield environment
    environment.versions.close()
    environment.imports.repository.close()


def settings_for(env) -> Settings:
    return Settings(
        kb_path=str(env.kb),
        catalog_db_path=str(env.tmp / "catalog.sqlite3"),
        uploads_dir=str(env.tmp / "uploads"),
    )


def ingest_args(env, legacy: bool) -> argparse.Namespace:
    return argparse.Namespace(source=str(env.tmp / "Pricelist-base.xlsx"), out=str(env.kb.parent), legacy=legacy)


def test_ingest_refuses_when_catalog_is_versioned(env, cli):
    env.service.init("admin")
    before = (env.kb.read_bytes(), (env.kb.parent / "current").read_bytes())

    with pytest.raises(SystemExit, match="import-1c"):
        cli._ingest(ingest_args(env, legacy=False))

    assert (env.kb.read_bytes(), (env.kb.parent / "current").read_bytes()) == before


def test_ingest_legacy_rewrites_only_legacy_file(env, cli, capsys):
    env.service.init("admin")
    env.service.publish_media({"P02": ["https://vdm.ru/c.jpg"]}, {}, "admin")
    pointer = read_pointer(env.kb.parent)
    current = snapshot_path(env.kb.parent, pointer.version).read_bytes()

    cli._ingest(ingest_args(env, legacy=True))

    assert read_pointer(env.kb.parent) == pointer
    assert snapshot_path(env.kb.parent, pointer.version).read_bytes() == current
    rebuilt = {r["sku_1c"]: r for r in map(json.loads, env.kb.read_text(encoding="utf-8").splitlines())}
    # Фото переносятся из текущей версии, а не из устаревшего legacy-файла.
    assert rebuilt["P02"]["images"] == ["https://vdm.ru/c.jpg"]
    assert "бот продолжает работать с версией" in capsys.readouterr().out
    assert not list(env.kb.parent.glob(".*.tmp"))


def test_ingest_without_pointer_keeps_old_behaviour(env, cli):
    cli._ingest(ingest_args(env, legacy=False))

    assert read_pointer(env.kb.parent) is None
    records = [json.loads(line) for line in env.kb.read_text(encoding="utf-8").splitlines()]
    assert len(records) == len(BASE) and records[0]["images"] == ["https://vdm.ru/a/1.jpg"]


def test_media_command_creates_version_only_on_change(env, cli):
    env.service.init("admin")
    storage = Storage(env.tmp / "media.sqlite3")
    engine = SimpleNamespace(storage=storage)
    storage.save_media("P03", ["https://vdm.ru/d.jpg"], source="card")
    storage.save_attributes("P03", {"Страна": "Россия"})

    message = cli._sync_media(engine, settings_for(env))

    version = read_pointer(env.kb.parent).version
    assert message == f"Создана и применена версия {version}."
    assert env.versions.get(version).source == "media"
    card = {r["sku_1c"]: r for r in env.records()}["P03"]
    assert (card["images"], card["attributes"], card["price"]) == (["https://vdm.ru/d.jpg"], {"Страна": "Россия"}, 103)

    assert "Изменений нет" in cli._sync_media(engine, settings_for(env))
    assert read_pointer(env.kb.parent).version == version


def test_media_command_without_pointer_uses_legacy_sync(env, cli):
    storage = Storage(env.tmp / "media.sqlite3")
    storage.save_media("P04", ["https://vdm.ru/e.jpg"], source="card")
    before = file_sha256(env.kb)

    message = cli._sync_media(SimpleNamespace(storage=storage), settings_for(env))

    assert message.startswith("перелито в базу знаний") and file_sha256(env.kb) != before
    assert read_pointer(env.kb.parent) is None


def test_price_change_does_not_refetch_or_drop_photos(env):
    """Приёмка п. 70: цена 12 500 → 13 700, старая версия в истории, фото на месте."""
    base = [(code, name, 12500 if code == "P00" else price, stock) for code, name, price, stock in BASE]
    environment = make_env(env.tmp / "photos", products=base)
    environment.service.init("admin")
    record = environment.upload([(c, n, 13700 if c == "P00" else p, s) for c, n, p, s in base])

    [row] = [r for r in environment.imports.diff_rows(record.id) if r.sku_1c == "P00"]
    assert (row.old_price, row.new_price, row.price_delta, row.price_delta_pct) == (12500, 13700, 1200, 9.6)
    version = environment.service.approve(record.id, "manager")

    card = {r["sku_1c"]: r for r in environment.records()}["P00"]
    assert (card["price"], card["images"]) == (13700, ["https://vdm.ru/a/1.jpg"])
    history = environment.versions.product_history("P00")
    assert [(h["version"], h["price"], h["changed_fields"]) for h in history] == [
        ("baseline", 12500, []),
        (version.version, 13700, ["price"]),
    ]
