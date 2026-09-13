"""EPIC 4, этап 2: версии каталога — baseline, утверждение, применение, откат, восстановление.

Выгрузки синтетические, база знаний и базы SQLite — во временной папке.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catalog.current import (
    CatalogPointerError,
    Pointer,
    file_sha256,
    read_pointer,
    resolve_catalog,
    snapshot_path,
    write_pointer,
)
from catalog_import.files import FileStore
from catalog_import.models import ImportStatus
from catalog_import.repository import SqliteImportRepository
from catalog_import.service import CatalogImportService, ImportStateError
from catalog_versions import service as service_module
from catalog_versions.cards import MEDIA_FIELDS, forbidden_changes
from catalog_versions.lock import CatalogApplyLock, CatalogLockTimeout
from catalog_versions.models import ChangeStatus, VersionSource, VersionStatus
from catalog_versions.repository import SqliteVersionRepository
from catalog_versions.service import (
    LOCK_NAME,
    CatalogVersionError,
    CatalogVersionService,
    SafetyCheckFailed,
    Thresholds,
)
from ingest import build_kb
from test_catalog_import import BITRIX_HEADER, HEADER, KINDERGARTEN, write_xlsx

NAMES = ["Мяч", "Обруч", "Скакалка", "Кегли", "Конус", "Мат", "Скамья", "Канат", "Гантели", "Флажки", "Кубики", "Пирамида"]
BASE = [(f"P{number:02d}", name, 100 + number, 5) for number, name in enumerate(NAMES)]


class Crash(BaseException):
    """Падение процесса: обработчики `except Exception` его не ловят."""


class Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 9, 13, 10, 0, tzinfo=UTC)

    def __call__(self) -> str:
        self.moment += timedelta(seconds=1)
        return self.moment.isoformat(timespec="seconds")


def rows_for(products):
    return [
        HEADER,
        [KINDERGARTEN],
        ["12.04 Мячи"],
        *[
            [code, name, f"https://vdm.ru/{code}.html", None if price is None else str(price), str(stock)]
            for code, name, price, stock in products
        ],
    ]


@dataclass
class Env:
    tmp: Path
    kb: Path
    imports: CatalogImportService
    versions: SqliteVersionRepository
    service: CatalogVersionService
    clock: Clock
    uploads: int = 0

    def upload(self, products):
        self.uploads += 1
        source = write_xlsx(self.tmp / f"Pricelist-{self.uploads}.xlsx", [rows_for(products), [BITRIX_HEADER]])
        record = self.imports.upload(source, uploaded_by="manager")
        assert record.status is ImportStatus.PARSED
        return record

    def service_again(self, **kwargs) -> CatalogVersionService:
        """Новый процесс: те же база и папка, свежие объекты."""
        return CatalogVersionService(
            self.versions, self.imports, self.kb, self.tmp, clock=self.clock, lock_timeout=0.5, **kwargs
        )

    def records(self):
        return resolve_catalog(self.kb).records()


def make_env(tmp_path: Path, products=BASE, thresholds: Thresholds | None = None) -> Env:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = write_xlsx(tmp_path / "Pricelist-base.xlsx", [rows_for(products), [BITRIX_HEADER]])
    kb_dir = tmp_path / "kb"
    build_kb.build(source, kb_dir)
    kb = kb_dir / "products.jsonl"
    records = [json.loads(line) for line in kb.read_text(encoding="utf-8").splitlines()]
    records[0]["images"] = ["https://vdm.ru/a/1.jpg"]
    kb.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    clock = Clock()
    database = tmp_path / "catalog.sqlite3"
    versions = SqliteVersionRepository(database)
    imports = CatalogImportService(
        SqliteImportRepository(database),
        FileStore(tmp_path / "uploads"),
        current=lambda: resolve_catalog(kb),
        registry_path=kb_dir / "norms_1057.json",
        returning=versions.returning_codes,
        clock=clock,
    )
    service = CatalogVersionService(
        versions,
        imports,
        kb,
        tmp_path,
        thresholds=thresholds,
        registry_path=kb_dir / "norms_1057.json",
        clock=clock,
        lock_timeout=0.5,
    )
    return Env(tmp_path, kb, imports, versions, service, clock)


@pytest.fixture
def env(tmp_path):
    environment = make_env(tmp_path)
    yield environment
    environment.versions.close()
    environment.imports.repository.close()


def changed(products, **prices):
    return [(code, name, prices.get(code, price), stock) for code, name, price, stock in products]


def open_rows(env, sku):
    return [row for row in env.versions.product_history(sku) if row["valid_to"] is None]


# --- Baseline ------------------------------------------------------------------


def test_migration_0003_creates_version_tables(env):
    with closing(sqlite3.connect(env.versions.path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        columns = {row[1] for row in db.execute("PRAGMA table_info(catalog_versions)")}
        migrations = [row[0] for row in db.execute("SELECT version FROM schema_migrations")]
    assert {"catalog_versions", "product_versions"} <= tables
    assert {"version", "parent_version", "source", "created_at", "snapshot_path", "sha256", "counters", "status", "forced"} <= columns
    assert migrations == ["0001_catalog_import", "0002_catalog_diff", "0003_catalog_versions"]


def test_init_creates_baseline(env):
    legacy = env.kb.read_bytes()

    version = env.service.init("admin")

    pointer = json.loads((env.kb.parent / "current").read_text(encoding="utf-8"))
    assert pointer == {"schema": 1, "version": "baseline", "sha256": file_sha256(env.kb)}
    assert snapshot_path(env.kb.parent, "baseline").read_bytes() == legacy
    assert (version.version, version.source, version.status, version.parent_version) == (
        "baseline",
        VersionSource.BASELINE,
        VersionStatus.APPLIED,
        None,
    )
    assert version.snapshot_path == "kb/versions/baseline/products.jsonl"
    assert not Path(version.snapshot_path).is_absolute()
    assert version.counters["new"] == version.product_count == 12
    assert env.versions.open_history_count() == 12
    assert resolve_catalog(env.kb).version == "baseline"


def test_init_refuses_when_current_exists(env):
    env.service.init("admin")
    before = (env.kb.parent / "current").read_bytes()

    with pytest.raises(CatalogVersionError, match="не перезаписывает"):
        env.service.init("admin")
    assert (env.kb.parent / "current").read_bytes() == before


def test_approve_requires_baseline(env):
    record = env.upload(changed(BASE, P01=150))
    with pytest.raises(CatalogVersionError, match="catalog init"):
        env.service.approve(record.id, "manager")


# --- Утверждение и применение ----------------------------------------------------


def test_approve_applies_version_with_history(env):
    env.service.init("admin")
    record = env.upload(changed(BASE, P01=150))

    version = env.service.approve(record.id, "manager")

    assert (version.version, version.source, version.parent_version, version.import_id) == (
        "2026-09-13-001",
        VersionSource.ONE_C,
        "baseline",
        record.id,
    )
    assert version.counters["updated"] == 1 and version.counters["diff"]["updated"] == 1
    assert read_pointer(env.kb.parent) == Pointer(version.version, version.sha256)
    applied = env.imports.get(record.id)
    assert (applied.status, applied.version, applied.approved_by) == (ImportStatus.APPLIED, version.version, "manager")

    records = {r["sku_1c"]: r for r in env.records()}
    assert records["P01"]["price"] == 150
    # Фото из снимка перенесены, цена их не трогает.
    assert records["P00"]["images"] == ["https://vdm.ru/a/1.jpg"]
    history = env.versions.product_history("P01")
    assert [(row["version"], row["change_status"], row["price"]) for row in history] == [
        ("baseline", "NEW", 101),
        (version.version, "UPDATED", 150),
    ]
    assert history[0]["valid_to"] == history[1]["valid_from"] and history[1]["valid_to"] is None
    assert len(open_rows(env, "P02")) == 1 and open_rows(env, "P02")[0]["version"] == "baseline"
    with pytest.raises(ImportStateError):
        env.imports.rediff(record.id)


def test_same_file_again_changes_nothing(env):
    env.service.init("admin")
    record = env.upload(BASE)

    assert record.summary.diff["unchanged"] == 12 and record.summary.diff["updated"] == 0
    version = env.service.approve(record.id, "manager")
    assert (version.counters["updated"], version.counters["new"], version.counters["removed"]) == (0, 0, 0)


def test_stale_base_version_is_refused_until_rediff(env):
    env.service.init("admin")
    first = env.upload(changed(BASE, P01=150))
    second = env.upload(changed(BASE, P02=160))
    env.service.approve(first.id, "manager")

    with pytest.raises(CatalogVersionError, match="--rediff"):
        env.service.approve(second.id, "manager")
    assert env.imports.get(second.id).status is ImportStatus.PARSED

    refreshed = env.imports.rediff(second.id)
    assert refreshed.base_version == "2026-09-13-001"
    version = env.service.approve(second.id, "manager")
    records = {r["sku_1c"]: r for r in env.records()}
    # Второй файл — полная выгрузка: цена P01 в нём старая, и она возвращается.
    assert (records["P01"]["price"], records["P02"]["price"], version.parent_version) == (101, 160, "2026-09-13-001")


def test_changed_fingerprint_is_refused(env):
    env.service.init("admin")
    record = env.upload(changed(BASE, P01=150))
    (env.kb.parent / "norms_1057.json").write_text(
        json.dumps({"products": {"P03": [{"item_code": "2.4.1", "item_title": "Кегли"}]}}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(CatalogVersionError, match="Отпечаток diff"):
        env.service.approve(record.id, "manager")
    assert read_pointer(env.kb.parent).version == "baseline"
    assert not snapshot_path(env.kb.parent, "2026-09-13-001").exists()


def test_removed_share_threshold_requires_force(env):
    env.service.init("admin")
    record = env.upload(BASE[:10])  # исчезают 2 из 12 — 16.7 %

    with pytest.raises(SafetyCheckFailed, match="исчезает 16.7%"):
        env.service.approve(record.id, "manager")
    assert read_pointer(env.kb.parent).version == "baseline"

    version = env.service.approve(record.id, "manager", force=True)
    assert version.forced and version.counters["removed"] == 2
    removed = env.versions.product_history("P11")
    assert [row["change_status"] for row in removed] == ["NEW", "REMOVED"]
    assert removed[-1]["valid_to"] is None and removed[-1]["name"] == "Пирамида"


def test_price_threshold_requires_force_and_is_configurable(tmp_path):
    prices = {code: price * 2 for code, _name, price, _stock in BASE[:4]}  # 4 из 12 — 33 %
    strict = make_env(tmp_path / "strict")
    strict.service.init("admin")
    record = strict.upload(changed(BASE, **prices))
    with pytest.raises(SafetyCheckFailed, match="цена меняется у 33.3%"):
        strict.service.approve(record.id, "manager")

    relaxed = make_env(tmp_path / "relaxed", thresholds=Thresholds(0.10, 0.50))
    relaxed.service.init("admin")
    record = relaxed.upload(changed(BASE, **prices))
    assert not relaxed.service.approve(record.id, "manager").forced


def test_force_does_not_bypass_snapshot_checks(env, monkeypatch):
    env.service.init("admin")
    record = env.upload(BASE[:10])
    monkeypatch.setattr(service_module, "validate_records", lambda _records: ["код P00 повторяется"])

    with pytest.raises(CatalogVersionError, match="--force это не отменяет"):
        env.service.approve(record.id, "manager", force=True)
    assert read_pointer(env.kb.parent).version == "baseline"


def test_returning_code_is_marked(env):
    env.service.init("admin")
    env.service.approve(env.upload(BASE[:11]).id, "manager")  # P11 исчезает: 8.3 %

    record = env.upload(BASE)

    rows = {row.sku_1c: row for row in env.imports.diff_rows(record.id)}
    assert rows["P11"].returning and record.summary.diff["returning"] == 1
    env.service.approve(record.id, "manager")
    assert [row["change_status"] for row in env.versions.product_history("P11")] == ["NEW", "REMOVED", "NEW"]


# --- Откат ---------------------------------------------------------------------


def test_rollback_creates_new_linear_version(env):
    env.service.init("admin")
    baseline_bytes = snapshot_path(env.kb.parent, "baseline").read_bytes()
    first = env.service.approve(env.upload(changed(BASE, P01=150)).id, "manager")

    rolled = env.service.rollback("baseline", "admin")

    assert (rolled.version, rolled.source, rolled.parent_version, rolled.inputs) == (
        "2026-09-13-002",
        VersionSource.ROLLBACK,
        first.version,
        {"rollback_to": "baseline"},
    )
    assert snapshot_path(env.kb.parent, rolled.version).read_bytes() == baseline_bytes
    assert read_pointer(env.kb.parent) == Pointer(rolled.version, file_sha256(env.kb))
    assert [(row["version"], row["price"]) for row in env.versions.product_history("P01")] == [
        ("baseline", 101),
        (first.version, 150),
        (rolled.version, 101),
    ]
    assert [v.version for v in env.service.list_versions()] == [rolled.version, first.version, "baseline"]


def test_rollback_refusals_and_threshold(env):
    env.service.init("admin")
    with pytest.raises(CatalogVersionError, match="и так текущая"):
        env.service.rollback("baseline", "admin")
    with pytest.raises(CatalogVersionError, match="нет среди применённых"):
        env.service.rollback("2099-01-01-001", "admin")

    grown = env.service.approve(env.upload(BASE + [("N1", "Лыжи", 900, 1), ("N2", "Сани", 800, 1)]).id, "manager")
    with pytest.raises(SafetyCheckFailed, match="исчезает"):
        env.service.rollback("baseline", "admin")  # убирает 2 из 14
    rolled = env.service.rollback("baseline", "admin", force=True)
    assert rolled.forced and rolled.parent_version == grown.version


# --- Фото и реестр -------------------------------------------------------------


def test_media_version_only_when_something_changed(env):
    env.service.init("admin")

    noop = env.service.publish_media({"P00": ["https://vdm.ru/a/1.jpg"]}, {}, "admin")
    assert noop.noop and env.versions.latest_applied().version == "baseline"

    result = env.service.publish_media({"P02": ["https://vdm.ru/c.jpg"]}, {"P02": {"Страна": "Россия"}}, "admin")
    assert result.version.source is VersionSource.MEDIA
    [change] = [row for row in env.versions.product_history("P02") if row["version"] == result.version.version]
    assert change["changed_fields"] == ["images", "attributes"]
    assert {r["sku_1c"]: r for r in env.records()}["P02"]["price"] == 102


def test_media_and_registry_must_not_touch_other_fields():
    old = [{"sku_1c": "A", "name": "Мяч", "price": 100, "images": []}]
    assert forbidden_changes(old, [{**old[0], "images": ["a.jpg"]}], MEDIA_FIELDS) == []
    assert "запрещено менять price" in forbidden_changes(old, [{**old[0], "price": 90}], MEDIA_FIELDS)[0]
    assert "набор товаров" in forbidden_changes(old, [], MEDIA_FIELDS)[0]


def test_registry_version_and_noop(env):
    env.service.init("admin")
    registry = env.kb.parent / "norms_1057.json"
    registry.write_text(
        json.dumps({"products": {"P03": [{"item_code": "2.4.1", "item_title": "Кегли"}]}}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = env.service.publish_registry("admin")

    assert result.version.source is VersionSource.REGISTRY
    card = {r["sku_1c"]: r for r in env.records()}["P03"]
    assert [(n["item_code"], n["source"]) for n in card["norms"] if n["doc_id"] == "order_1057"] == [("2.4.1", "registry")]
    assert result.version.inputs["registry_sha256"] == file_sha256(registry)
    assert env.service.publish_registry("admin").noop


# --- Восстановление ------------------------------------------------------------


def crash(*_args, **_kwargs):
    raise Crash


def test_recovery_a_ready_with_old_pointer_continues(env, monkeypatch):
    env.service.init("admin")
    record = env.upload(changed(BASE, P01=150))
    monkeypatch.setattr(service_module, "write_pointer", crash)
    with pytest.raises(Crash):
        env.service.approve(record.id, "manager")
    monkeypatch.undo()
    assert env.versions.get("2026-09-13-001").status is VersionStatus.READY
    assert read_pointer(env.kb.parent).version == "baseline"

    messages = env.service_again().recover()

    assert any("продолжено" in message for message in messages)
    assert read_pointer(env.kb.parent).version == "2026-09-13-001"
    assert env.versions.get("2026-09-13-001").status is VersionStatus.APPLIED
    assert env.imports.get(record.id).status is ImportStatus.APPLIED
    assert open_rows(env, "P01")[0]["price"] == 150


def test_recovery_b_pointer_switched_finishes_database(env, monkeypatch):
    env.service.init("admin")
    record = env.upload(changed(BASE, P01=150))
    monkeypatch.setattr(env.versions, "finalize", crash)
    with pytest.raises(Crash):
        env.service.approve(record.id, "manager")
    monkeypatch.undo()
    assert read_pointer(env.kb.parent).version == "2026-09-13-001"
    assert env.versions.get("2026-09-13-001").status is VersionStatus.READY

    env.service_again().recover()

    assert env.versions.get("2026-09-13-001").status is VersionStatus.APPLIED
    assert env.imports.get(record.id).status is ImportStatus.APPLIED
    assert [row["version"] for row in env.versions.product_history("P01")] == ["baseline", "2026-09-13-001"]
    # Повторное восстановление ничего не дублирует.
    assert env.service_again().recover() == []
    assert len(env.versions.product_history("P01")) == 2


def test_recovery_c_pointer_to_unknown_version_fails_safe(env):
    env.service.init("admin")
    baseline = snapshot_path(env.kb.parent, "baseline")
    stray = snapshot_path(env.kb.parent, "2026-01-01-001")
    stray.parent.mkdir(parents=True)
    stray.write_bytes(baseline.read_bytes())
    write_pointer(env.kb.parent, Pointer("2026-01-01-001", file_sha256(stray)))

    with pytest.raises(CatalogVersionError, match="нет в базе"):
        env.service_again().recover()

    write_pointer(env.kb.parent, Pointer("2026-01-01-002", "a" * 64))
    with pytest.raises(CatalogPointerError, match="снимка"):
        resolve_catalog(env.kb)


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
def test_recovery_d_e_broken_ready_snapshot_is_failed(env, monkeypatch, damage):
    env.service.init("admin")
    record = env.upload(changed(BASE, P01=150))
    monkeypatch.setattr(service_module, "write_pointer", crash)
    with pytest.raises(Crash):
        env.service.approve(record.id, "manager")
    monkeypatch.undo()
    snapshot = snapshot_path(env.kb.parent, "2026-09-13-001")
    if damage == "delete":
        snapshot.unlink()
    else:
        snapshot.write_bytes(snapshot.read_bytes() + b"{}\n")

    env.service_again().recover()

    assert env.versions.get("2026-09-13-001").status is VersionStatus.FAILED
    assert read_pointer(env.kb.parent).version == "baseline"
    assert env.imports.get(record.id).status is ImportStatus.FAILED
    # Утверждение можно повторить: снимок собирается заново под новым номером.
    again = env.service.approve(record.id, "manager")
    assert again.version == "2026-09-13-002" and env.imports.get(record.id).status is ImportStatus.APPLIED


def test_recovery_f_orphan_snapshot_is_not_used(env):
    env.service.init("admin")
    orphan = snapshot_path(env.kb.parent, "2026-09-13-001")
    orphan.parent.mkdir(parents=True)
    orphan.write_text("{}\n", encoding="utf-8")

    assert any("сирота 2026-09-13-001" in line for line in env.service.check())
    assert env.service.recover() == []
    version = env.service.approve(env.upload(changed(BASE, P01=150)).id, "manager")
    assert version.version == "2026-09-13-002" and orphan.read_text(encoding="utf-8") == "{}\n"


def test_recovery_g_pointer_reconciled_with_latest_applied(env):
    env.service.init("admin")
    applied = env.service.approve(env.upload(changed(BASE, P01=150)).id, "manager")
    baseline = env.versions.get("baseline")
    write_pointer(env.kb.parent, Pointer("baseline", baseline.sha256))
    count = len(env.service.list_versions())

    assert any("ОШИБКА" in line for line in env.service.check())
    messages = env.service_again().recover()

    assert any("сверен" in message for message in messages)
    assert read_pointer(env.kb.parent).version == applied.version
    assert len(env.service.list_versions()) == count


def test_corrupted_current_snapshot_is_never_served(env):
    env.service.init("admin")
    current = snapshot_path(env.kb.parent, "baseline")
    current.write_bytes(current.read_bytes().replace(b"101", b"999", 1))

    with pytest.raises(CatalogPointerError, match="повреждён"):
        resolve_catalog(env.kb)
    with pytest.raises(CatalogPointerError):
        env.service_again().recover()


def test_building_folder_is_cleaned(env):
    env.service.init("admin")
    leftover = env.kb.parent / "versions" / ".building-deadbeef"
    leftover.mkdir()
    (leftover / "products.jsonl").write_text("{}", encoding="utf-8")

    messages = env.service.recover()

    assert not leftover.exists() and any("недостроенный" in message for message in messages)


def test_apply_lock_serializes_commands(env):
    env.service.init("admin")
    record = env.upload(changed(BASE, P01=150))

    with CatalogApplyLock(env.kb.parent / LOCK_NAME):
        with pytest.raises(CatalogLockTimeout):
            env.service_again().approve(record.id, "manager")
    assert env.service.approve(record.id, "manager").status is VersionStatus.APPLIED


def test_change_status_values_are_stored(env):
    env.service.init("admin")
    env.service.approve(env.upload(BASE[:11] + [("N1", "Лыжи", 900, 1)]).id, "manager")
    statuses = {
        sku: env.versions.product_history(sku)[-1]["change_status"] for sku in ("N1", "P11", "P00")
    }
    assert statuses == {"N1": ChangeStatus.NEW, "P11": ChangeStatus.REMOVED, "P00": ChangeStatus.NEW}
