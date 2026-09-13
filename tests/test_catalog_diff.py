"""EPIC 4, этап 1: diff импорта 1С против текущего каталога и его хранение.

Каталог и выгрузки синтетические, настоящие data/kb и базы не читаются.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from catalog.current import resolve_catalog
from catalog.matcher import MatchMethod, MatchStatus, Reason
from catalog_import.diff import (
    DiffCounters,
    DiffStatus,
    PriceStatus,
    compute_diff,
    filter_rows,
    fingerprint,
    format_diff,
)
from catalog_import.files import FileStore
from catalog_import.matching import CodeState
from catalog_import.models import ImportItem, ImportStatus
from catalog_import.repository import SqliteImportRepository
from catalog_import.service import CatalogImportService, ImportStateError, format_preview
from catalog_versions.cards import apply_registry
from ingest import build_kb
from test_catalog_import import ERROR_BITRIX, ERROR_ROWS, NOW, VALID_BITRIX, VALID_ROWS, write_xlsx
from test_catalog_import_matching import SpyMatcher

REGISTRY = {"S1": [{"item_code": "2.4.1", "item_title": "Мяч"}]}


def record(code: str, name: str, price: int | None = 100, stock: int | None = 5, **extra) -> dict:
    return {
        "sku_1c": code,
        "name": name,
        "url": f"https://vdm.ru/{code}.html",
        "short_url": None,
        "price": price,
        "currency": "RUB",
        "in_stock": stock,
        "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА", "12.04 Мячи"]],
        "description": "",
        "kit_contents": [],
        "norms": [],
        "bitrix_id": None,
        "images": [],
        "attributes": {},
        "sources": {"catalog": "Pricelist.xlsx"},
        "updated_at": "2026-09-01T00:00:00+00:00",
        **extra,
    }


def item(code: str, name: str, price: int | None = 100, stock: int | None = 5, **extra) -> ImportItem:
    payload = record(code, name, price, stock, **extra)
    payload["updated_at"] = NOW
    payload["sources"] = {"catalog": "Pricelist20260913.xlsx"}
    return ImportItem(sku_1c=code, name=name, price=price, stock=stock, rows=[2], payload=payload)


def snapshot(tmp_path: Path, *records: dict):
    kb = tmp_path / "kb" / "products.jsonl"
    kb.parent.mkdir(parents=True, exist_ok=True)
    kb.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return resolve_catalog(kb)


def diff_of(tmp_path, current: list[dict], items: list[ImportItem], **kwargs):
    snap = snapshot(tmp_path, *current)
    codes = kwargs.pop("file_codes", [i.sku_1c for i in items])
    return compute_diff(items, codes, snap, **kwargs)


def by_code(diff) -> dict:
    return {row.sku_1c: row for row in diff.rows}


# --- Статусы из ТЗ -------------------------------------------------------------


def test_unchanged_import(tmp_path):
    """Фото, характеристики и нормы реестра есть только в снимке — это не изменения."""
    enriched = record(
        "S1", "Мяч резиновый", images=["https://vdm.ru/1.jpg"], attributes={"Страна": "Россия"}
    )
    apply_registry([enriched], REGISTRY)
    current = [enriched, record("S2", "Обруч 60 см", price=None, stock=None)]

    diff = diff_of(
        tmp_path,
        current,
        [item("S1", "Мяч резиновый"), item("S2", "Обруч 60 см", price=None, stock=None)],
        registry=REGISTRY,
    )

    assert [row.diff_status for row in diff.rows] == [DiffStatus.UNCHANGED] * 2
    assert all(row.changed_fields == () for row in diff.rows)
    assert diff.counters == DiffCounters(current_products=2, existing=2, unchanged=2)
    assert {row.match_status for row in diff.rows} == {"MATCHED_EXACT"}
    # Снимок из этого импорта совпадает с текущим, кроме служебных полей.
    assert [r["images"] for r in diff.candidate] == [["https://vdm.ru/1.jpg"], []]
    assert diff.candidate[0]["norms"] == enriched["norms"]


def test_no_false_new_or_missing_from_enrichment(tmp_path):
    current = [record("S1", "Мяч", images=["a.jpg"], attributes={"Артикул": "M-1"})]

    diff = diff_of(tmp_path, current, [item("S1", "Мяч")])

    assert diff.counters.new == diff.counters.missing == diff.counters.updated == 0


def test_price_change(tmp_path):
    current = [record("S1", "Мяч", price=12500), record("S2", "Обруч", price=400)]

    diff = diff_of(tmp_path, current, [item("S1", "Мяч", price=13700), item("S2", "Обруч", price=300)])
    rows = by_code(diff)

    up, down = rows["S1"], rows["S2"]
    assert (up.diff_status, up.changed_fields, up.price_status) == (
        DiffStatus.UPDATED,
        ("price",),
        PriceStatus.INCREASED,
    )
    assert (up.old_price, up.new_price, up.price_delta, up.price_delta_pct) == (12500, 13700, 1200, 9.6)
    assert (down.price_status, down.price_delta, down.price_delta_pct) == (PriceStatus.DECREASED, -100, -25.0)
    assert (diff.counters.price_changed, diff.counters.price_increased, diff.counters.price_decreased) == (2, 1, 1)
    assert diff.counters.price_changed_share == 1.0


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (None, 500, (PriceStatus.NEW, None, None)),
        (500, None, (PriceStatus.REMOVED, None, None)),
        (0, 500, (PriceStatus.INCREASED, 500, None)),
        (None, None, (PriceStatus.UNCHANGED, None, None)),
    ],
)
def test_price_appears_disappears_and_zero(tmp_path, old, new, expected):
    diff = diff_of(tmp_path, [record("S1", "Мяч", price=old)], [item("S1", "Мяч", price=new)])
    row = diff.rows[0]
    assert (row.price_status, row.price_delta, row.price_delta_pct) == expected


def test_stock_change(tmp_path):
    current = [record("S1", "Мяч", stock=5), record("S2", "Обруч", stock=None)]

    diff = diff_of(tmp_path, current, [item("S1", "Мяч", stock=0), item("S2", "Обруч", stock=3)])
    rows = by_code(diff)

    assert (rows["S1"].diff_status, rows["S1"].changed_fields) == (DiffStatus.UPDATED, ("in_stock",))
    assert (rows["S1"].old_stock, rows["S1"].new_stock, rows["S1"].stock_changed) == (5, 0, True)
    assert rows["S1"].price_status is PriceStatus.UNCHANGED
    # Остаток был неизвестен и стал известен — тоже изменение.
    assert (rows["S2"].old_stock, rows["S2"].new_stock, rows["S2"].stock_changed) == (None, 3, True)
    assert (diff.counters.stock_changed, diff.counters.price_changed) == (2, 0)


def test_name_change(tmp_path):
    diff = diff_of(tmp_path, [record("S1", "Обруч 60 см")], [item("S1", "Обруч 80 см")])
    row = diff.rows[0]

    assert (row.diff_status, row.changed_fields) == (DiffStatus.UPDATED, ("name",))
    assert (row.old_name, row.new_name) == ("Обруч 60 см", "Обруч 80 см")
    # Код тот же, но числа в названии разошлись: применяется целиком, но на проверку.
    assert row.match_status == MatchStatus.MATCHED_REVIEW and row.needs_review
    assert Reason.DIGITS_MISMATCH in row.reason_codes
    assert diff.counters.needs_review == 1


def test_other_catalog_fields_are_compared(tmp_path):
    diff = diff_of(
        tmp_path,
        [record("S1", "Мяч", kit_contents=["мяч"])],
        [item("S1", "Мяч", kit_contents=["мяч", "насос"], url="https://vdm.ru/new.html")],
    )
    assert diff.rows[0].changed_fields == ("url", "kit_contents")


def test_new_product(tmp_path):
    diff = diff_of(tmp_path, [record("S1", "Мяч")], [item("S1", "Мяч"), item("N1", "Скакалка", price=150)])
    row = by_code(diff)["N1"]

    assert (row.state, row.diff_status, row.price_status) == (CodeState.NEW, DiffStatus.NEW, PriceStatus.NEW)
    assert (row.old_name, row.new_name, row.new_price) == (None, "Скакалка", 150)
    # Исчезнувших кодов нет — искать перекодировку не среди чего.
    assert (row.match_status, row.recoding) == (None, False)
    assert diff.counters.new == 1


def test_removed_product(tmp_path):
    current = [record("S1", "Мяч"), record("R1", "Кегли", price=900, stock=2)]

    diff = diff_of(tmp_path, current, [item("S1", "Мяч")])
    row = by_code(diff)["R1"]

    assert (row.state, row.diff_status, row.price_status) == (CodeState.MISSING, DiffStatus.REMOVED, PriceStatus.REMOVED)
    assert (row.old_name, row.old_price, row.old_stock, row.new_name) == ("Кегли", 900, 2, None)
    assert (diff.counters.missing, diff.counters.removed_share) == (1, 0.5)
    assert [r["sku_1c"] for r in diff.candidate] == ["S1"]


# --- Коды и перекодировка ------------------------------------------------------


def test_existing_numeric_code_unchanged(tmp_path):
    diff = diff_of(tmp_path, [record("1217", "Скакалка гимнастическая")], [item("1217", "Скакалка гимнастическая")])
    row = diff.rows[0]

    assert (row.state, row.diff_status) == (CodeState.EXISTING, DiffStatus.UNCHANGED)
    assert (row.match_status, row.match_method, row.matched_product_id) == ("MATCHED_EXACT", MatchMethod.CODE_1C, "1217")


def test_numeric_code_changed_is_recoding_candidate(tmp_path):
    diff = diff_of(tmp_path, [record("1217", "Скакалка гимнастическая")], [item("1218", "Скакалка гимнастическая")])
    rows = by_code(diff)

    new, removed = rows["1218"], rows["1217"]
    assert (new.diff_status, new.recoding, new.matched_product_id) == (DiffStatus.NEW, True, "1217")
    assert removed.diff_status is DiffStatus.REMOVED
    # Автоматически не связывается: новый код идёт в снимок сам по себе, старый уходит.
    assert [r["sku_1c"] for r in diff.candidate] == ["1218"]
    assert (diff.counters.new, diff.counters.recoding, diff.counters.missing) == (1, 1, 1)
    assert filter_rows(diff.rows, "recoding") == [new]


def test_ambiguous_recoding(tmp_path):
    current = [record("A1", "Мяч резиновый"), record("A2", "Мяч резиновый")]

    diff = diff_of(tmp_path, current, [item("N1", "Мяч резиновый")])
    row = by_code(diff)["N1"]

    assert (row.diff_status, row.match_status, row.recoding) == (DiffStatus.AMBIGUOUS, "AMBIGUOUS", False)
    assert {c["product_id"] for c in row.candidates} == {"A1", "A2"}
    assert (diff.counters.ambiguous, diff.counters.new, diff.counters.missing) == (1, 0, 2)


def test_recoding_only_removed_and_no_full_catalog_fuzzy_scan(tmp_path):
    current = [
        record("S1", "Стол ученический регулируемый"),
        record("S2", "Стол ученический регулируемый"),
        record("R1", "Стол ученический регулируемый"),
    ]
    matchers: list[SpyMatcher] = []

    def factory(repository, settings):
        matchers.append(SpyMatcher(repository, settings))
        return matchers[-1]

    diff = diff_of(
        tmp_path,
        current,
        [
            item("S1", "Стол ученический регулируемый"),
            item("S2", "Стол ученический регулируемый"),
            item("N1", "Стол ученический регулируемый"),
            # Опечатка: точного названия нет, дело доходит до поиска похожих.
            item("N2", "Стол ученичесикй регулируемый"),
        ],
        matcher_factory=factory,
    )
    row = by_code(diff)["N1"]

    # Совпадающие по названию S1 и S2 кандидатами не стали: они в файле есть.
    assert (row.recoding, row.matched_product_id) == (True, "R1")
    [spy] = matchers
    assert spy.calls == [
        ("S1", frozenset()),
        ("S2", frozenset()),
        ("N1", frozenset({"R1"})),
        ("N2", frozenset({"R1"})),
    ]
    # Поиск похожих шёл только по исчезнувшему коду; индекс всего каталога не тронут
    # (SpyMatcher падает при обращении к нему).
    assert spy.pools == [frozenset({"R1"})]
    assert by_code(diff)["N2"].matched_product_id in (None, "R1")


def test_supplier_article_is_checked_among_removed(tmp_path):
    current = [
        record("R1", "Стол ученический", attributes={"Артикул": "СТ-1"}),
        record("R2", "Шкаф для пособий", attributes={"Артикул": "ШК-7"}),
    ]

    diff = diff_of(
        tmp_path,
        current,
        [
            item("N1", "Стол ученический", attributes={"Артикул": "СТ-1"}),
            item("N2", "Шкаф для пособий", attributes={"Артикул": "ШК-9"}),
        ],
    )
    rows = by_code(diff)

    assert rows["N1"].recoding and rows["N1"].matched_product_id == "R1"
    # Артикул поставщика с обеих сторон и разный — уверенного совпадения нет.
    conflict = rows["N2"]
    assert conflict.match_status not in ("MATCHED_EXACT", "MATCHED_HIGH")
    assert Reason.SUPPLIER_ARTICLE_CONFLICT in {
        code for candidate in conflict.candidates for code in candidate["reason_codes"]
    } | set(conflict.reason_codes)


def test_matcher_confidence_is_kept(tmp_path):
    diff = diff_of(tmp_path, [record("R1", "Скакалка гимнастическая")], [item("N1", "Скакалка гимнастическа")])
    row = by_code(diff)["N1"]

    assert row.match_confidence is not None and 0.0 < row.match_confidence < 1.0
    assert row.match_method == MatchMethod.FUZZY_NAME


def test_returning_code(tmp_path):
    diff = diff_of(
        tmp_path,
        [record("S1", "Мяч")],
        [item("S1", "Мяч"), item("N1", "Кегли")],
        returning=lambda codes: {"N1"} & set(codes),
    )
    assert by_code(diff)["N1"].returning and diff.counters.returning == 1


def test_row_error_keeps_existing_card_and_is_not_missing(tmp_path):
    current = [record("S1", "Мяч", price=100), record("E1", "Стол", price=800)]

    diff = diff_of(tmp_path, current, [item("S1", "Мяч")], file_codes=["S1", "E1"])
    row = by_code(diff)["E1"]

    assert (row.state, row.diff_status, row.row_error, row.new_price) == (
        CodeState.EXISTING,
        DiffStatus.UNCHANGED,
        True,
        800,
    )
    assert diff.counters.missing == 0 and diff.counters.row_errors == 1
    assert [r["sku_1c"] for r in diff.candidate] == ["S1", "E1"]
    assert diff.candidate[1]["price"] == 800


# --- Отпечаток -----------------------------------------------------------------


def test_diff_fingerprint(tmp_path):
    current = [record("S1", "Мяч", price=100)]
    first = diff_of(tmp_path / "a", current, [item("S1", "Мяч", price=120)])
    again = diff_of(tmp_path / "b", current, [item("S1", "Мяч", price=120)])
    other_price = diff_of(tmp_path / "c", current, [item("S1", "Мяч", price=130)])
    other_registry = diff_of(tmp_path / "d", current, [item("S1", "Мяч", price=120)], registry_sha256="f" * 64)

    assert len(first.fingerprint) == 64 and first.fingerprint == again.fingerprint
    assert other_price.fingerprint != first.fingerprint
    assert other_registry.fingerprint != first.fingerprint
    # Порядок строк на отпечаток не влияет.
    assert fingerprint(first.base_sha256, None, reversed(first.rows)) == first.fingerprint


def test_base_version(tmp_path):
    diff = diff_of(tmp_path, [record("S1", "Мяч")], [item("S1", "Мяч")])
    snap = resolve_catalog(tmp_path / "kb" / "products.jsonl")

    assert diff.base_version == f"legacy:{snap.sha256}" and diff.base_sha256 == snap.sha256


# --- Хранение и команды --------------------------------------------------------


@pytest.fixture
def repository(tmp_path):
    repo = SqliteImportRepository(tmp_path / "catalog.sqlite3")
    yield repo
    repo.close()


def kb_from(tmp_path: Path, source: Path) -> Path:
    """База знаний, собранная `ingest` из той же выгрузки, с реестром и фото."""
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir(exist_ok=True)
    (kb_dir / "norms_1057.json").write_text(
        json.dumps({"doc_id": "order_1057", "products": REGISTRY}, ensure_ascii=False), encoding="utf-8"
    )
    build_kb.build(source, kb_dir)
    kb = kb_dir / "products.jsonl"
    records = [json.loads(line) for line in kb.read_text(encoding="utf-8").splitlines()]
    records[0]["images"] = ["https://vdm.ru/a/1.jpg"]
    records[0]["attributes"] = {"Страна": "Россия"}
    kb.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return kb


def service_for(repository, tmp_path: Path, kb: Path) -> CatalogImportService:
    return CatalogImportService(
        repository,
        FileStore(tmp_path / "uploads"),
        current=lambda: resolve_catalog(kb),
        registry_path=kb.parent / "norms_1057.json",
        clock=lambda: NOW,
    )


def test_same_import_twice_is_all_unchanged(repository, tmp_path):
    source = write_xlsx(tmp_path / "Pricelist20260912.xlsx", [VALID_ROWS, VALID_BITRIX])
    kb = kb_from(tmp_path, source)
    service = service_for(repository, tmp_path, kb)

    record_ = service.upload(source, uploaded_by="manager")

    counters = DiffCounters.from_dict(record_.summary.diff)
    assert (counters.unchanged, counters.new, counters.updated, counters.missing, counters.recoding) == (3, 0, 0, 0, 0)
    rows = service.diff_rows(record_.id)
    assert [row.diff_status for row in rows] == [DiffStatus.UNCHANGED] * 3
    snap = resolve_catalog(kb)
    assert record_.base_version == f"legacy:{snap.sha256}"
    # Отпечаток, пересчитанный из сохранённых строк, совпадает с сохранённым.
    assert fingerprint(snap.sha256, service.current_snapshot() and _registry_sha(kb), rows) == record_.diff_fingerprint
    preview = format_preview(record_, [])
    assert "UNCHANGED 3" in preview and f"import-1c --diff {record_.id}" in preview


def _registry_sha(kb: Path) -> str:
    from catalog.current import file_sha256

    return file_sha256(kb.parent / "norms_1057.json")


def test_rediff_replaces_base_version_and_fingerprint(repository, tmp_path):
    source = write_xlsx(tmp_path / "Pricelist20260912.xlsx", [VALID_ROWS, VALID_BITRIX])
    kb = kb_from(tmp_path, source)
    service = service_for(repository, tmp_path, kb)
    first = service.upload(source, uploaded_by="manager")

    records = [json.loads(line) for line in kb.read_text(encoding="utf-8").splitlines()]
    records[0]["price"] = 300
    kb.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    second = service.rediff(first.id)

    assert second.status is ImportStatus.PARSED
    assert second.base_version != first.base_version
    assert second.diff_fingerprint != first.diff_fingerprint
    rows = {row.sku_1c: row for row in service.diff_rows(first.id)}
    assert (rows["S1"].old_price, rows["S1"].new_price, rows["S1"].price_status) == (300, 333, PriceStatus.INCREASED)
    assert len(rows) == 3
    text = format_diff(second, list(rows.values()), "price-up")
    assert "Позиции — цена выросла: 1" in text and second.diff_fingerprint in text


def test_rediff_refused_outside_parsed_or_failed(repository, tmp_path):
    source = write_xlsx(tmp_path / "Pricelist20260912.xlsx", [VALID_ROWS, VALID_BITRIX])
    service = service_for(repository, tmp_path, kb_from(tmp_path, source))
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not a workbook")
    invalid = service.upload(broken, uploaded_by="manager")

    with pytest.raises(ImportStateError, match="PARSED или FAILED"):
        service.rediff(invalid.id)
    with pytest.raises(ImportStateError, match="нет"):
        service.rediff("2099-01-01-001")


def test_row_error_of_existing_code_is_stored_as_unchanged(repository, tmp_path):
    source = write_xlsx(tmp_path / "errors.xlsx", [ERROR_ROWS, ERROR_BITRIX])
    kb = tmp_path / "kb" / "products.jsonl"
    snapshot(tmp_path, record("E1", "Стол", price=800), record("E3", "Полка", price=700, stock=2))
    service = service_for(repository, tmp_path, kb)

    record_ = service.upload(source, uploaded_by="manager")
    rows = {row.sku_1c: row for row in service.diff_rows(record_.id)}

    assert rows["E1"].row_error and rows["E1"].diff_status is DiffStatus.UNCHANGED
    assert "E2" not in rows and "X0" in rows  # новый код с ошибкой в diff не попадает
    assert DiffCounters.from_dict(record_.summary.diff).missing == 0


def test_catalog_matches_schema_and_values(repository, tmp_path):
    source = write_xlsx(tmp_path / "Pricelist20260912.xlsx", [VALID_ROWS, VALID_BITRIX])
    service = service_for(repository, tmp_path, kb_from(tmp_path, source))
    record_ = service.upload(source, uploaded_by="manager")

    with closing(sqlite3.connect(repository.path)) as db:
        columns = {row[1]: row[2] for row in db.execute("PRAGMA table_info(catalog_matches)")}
        import_columns = {row[1] for row in db.execute("PRAGMA table_info(catalog_imports)")}
        stored = db.execute(
            "SELECT typeof(import_id), import_id, match_confidence FROM catalog_matches WHERE sku_1c = 'S1'"
        ).fetchone()

    assert columns["import_id"] == "TEXT"
    assert {"matched_product_id", "candidates", "reason_codes", "match_confidence", "is_returning"} <= set(columns)
    # Базовая версия и отпечаток — у импорта, а не в каждой строке.
    assert "base_version" not in columns and "diff_fingerprint" not in columns
    assert {"base_version", "diff_fingerprint", "approved_by", "version"} <= import_columns
    assert stored[:2] == ("text", record_.id)
    assert stored[2] == service.diff_rows(record_.id)[0].match_confidence
