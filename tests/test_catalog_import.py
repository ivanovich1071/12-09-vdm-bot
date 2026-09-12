"""EPIC 2: импорт выгрузки 1С — загрузка, разбор, проверка, предпросмотр.

Книги Excel синтетические и собираются прямо в тесте: данных заказчика в git нет.
Настоящие `data/kb`, `data/uploads` и базы SQLite не читаются и не создаются —
всё во временной папке.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from catalog.models import Product
from catalog.repository import InMemoryCatalogRepository, load_products
from catalog.search import CatalogIndex
from catalog_import import service as import_service
from catalog_import.files import FileStore, UploadRejected
from catalog_import.models import CatalogComparison, ImportStatus, Severity
from catalog_import.parser import EXPECTED_HEADERS
from catalog_import.repository import SqliteImportRepository
from catalog_import.service import CatalogImportService, format_imports, format_preview
from core.migrations import apply_migrations
from ingest import build_kb, norm_registry
from ingest.xlsx_reader import XlsxFile, column_name

NOW = "2026-09-12T10:00:00+00:00"
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

HEADER = [EXPECTED_HEADERS[column] for column in "ABCDEFG"]
BITRIX_HEADER = ["ID элемента", "Наименование элемента"]
KINDERGARTEN = "ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"
SCHOOL = "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"
BALL = ["S1", "Мяч резиновый", "https://vdm.ru/catalog/myach.html", "333", "20", None, "<p>Мяч для <b>игр</b>.</p>"]

VALID_ROWS = [
    HEADER,  # 1
    [KINDERGARTEN],  # 2
    ["12. Спортивное оборудование и инвентарь"],  # 3
    ["12.04 Мячи"],  # 4
    BALL,  # 5
    ["S2", "Обруч 60 см", "https://vdm.ru/catalog/obruch.html", None, "0"],  # 6: нет цены
    None,  # 7: пустая строка в файл не пишется
    [SCHOOL],  # 8
    ["Раздел 1. Комплекс оснащения общешкольных помещений"],  # 9
    ["Подраздел 7. Спортивный комплекс"],  # 10
    ["1.7.11. Мяч баскетбольный"],  # 11
    BALL,  # 12: второе размещение того же товара
    ["S3", "Скакалка", "https://vdm.ru/catalog/skakalka.html", "150", None],  # 13: нет остатка
]
VALID_BITRIX = [BITRIX_HEADER, ["101", "Мяч резиновый"], ["102", "Обруч 60 см"], ["103", "Скакалка"]]

ERROR_ROWS = [
    HEADER,  # 1
    ["X0", "Товар до разделов", None, "100", "1"],  # 2: вне разделов
    [KINDERGARTEN],  # 3
    ["14. Мебель"],  # 4
    [None, "Стул без кода", None, "500", "3"],  # 5: нет кода
    ["E1", "Стол", None, "-10", "2"],  # 6: отрицательная цена
    ["E2", "Шкаф", None, "договорная", "2"],  # 7: цена не число
    ["E3", "Полка", None, "700", "2,5"],  # 8: дробный остаток — только оговорка
    ["E4", "Кровать", None, "900", "5"],  # 9
    ["E4", "Кровать", None, "950", "5"],  # 10: тот же код с другой ценой
    ["0Э-00002542"],  # 11: товар, потерявший наименование
    ["E5", "Ковёр", None, "1200", "-1"],  # 12: отрицательный остаток
]
ERROR_BITRIX = [BITRIX_HEADER, ["201", "Стол"], ["202", "стол"]]


def write_xlsx(
    path: Path, sheets: list[list[list[str | None] | None]], *, shared_strings: bool = False
) -> Path:
    """Минимальная книга Excel: листы, строки с номерами, пропуски ячеек и строк."""
    strings: list[str] = []

    def cell(ref: str, value: str) -> str:
        if shared_strings:
            strings.append(value)
            return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'

    with zipfile.ZipFile(path, "w") as book:
        entries, rels = [], []
        for index, rows in enumerate(sheets, start=1):
            body = []
            for number, row in enumerate(rows, start=1):
                if row is None:
                    continue
                cells = "".join(
                    cell(f"{column_name(col)}{number}", value)
                    for col, value in enumerate(row)
                    if value
                )
                body.append(f'<row r="{number}">{cells}</row>')
            book.writestr(
                f"xl/worksheets/sheet{index}.xml",
                f'<worksheet xmlns="{MAIN}"><sheetData>{"".join(body)}</sheetData></worksheet>',
            )
            entries.append(f'<sheet name="Лист {index}" sheetId="{index}" r:id="rId{index}"/>')
            rels.append(
                f'<Relationship Id="rId{index}" Type="{DOC_REL}/worksheet" '
                f'Target="worksheets/sheet{index}.xml"/>'
            )
        book.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}"><sheets>{"".join(entries)}</sheets></workbook>',
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PKG_REL}">{"".join(rels)}</Relationships>'
        )
        if shared_strings:
            items = "".join(f'<si><t xml:space="preserve">{escape(s)}</t></si>' for s in strings)
            book.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">{items}</sst>')
    return path


@pytest.fixture
def repository(tmp_path):
    repo = SqliteImportRepository(tmp_path / "catalog.sqlite3")
    yield repo
    repo.close()


def make_service(repository, tmp_path: Path, **kwargs) -> CatalogImportService:
    return CatalogImportService(repository, FileStore(tmp_path / "uploads"), clock=lambda: NOW, **kwargs)


@pytest.fixture
def service(repository, tmp_path) -> CatalogImportService:
    return make_service(repository, tmp_path)


@pytest.fixture
def valid_file(tmp_path) -> Path:
    return write_xlsx(tmp_path / "Pricelist20260912.xlsx", [VALID_ROWS, VALID_BITRIX])


# --- Названные в ТЗ ------------------------------------------------------------


def test_import_valid(service, valid_file):
    record = service.upload(valid_file, uploaded_by="manager")

    assert (record.id, record.status, record.duplicate) == ("2026-09-12-001", ImportStatus.PARSED, False)
    summary = record.summary
    assert (summary.rows_total, summary.headings, summary.product_rows) == (12, 7, 4)
    assert (summary.products, summary.cross_listed, summary.accepted, summary.rejected) == (3, 1, 3, 0)
    assert (summary.errors, summary.warnings) == (0, 2)
    assert summary.issues_by_code == {"missing_price": 1, "missing_stock": 1}
    assert summary.comparison is None

    items = {item.sku_1c: item for item in service.items(record.id)}
    assert items["S1"].rows == [5, 12]
    assert (items["S1"].price, items["S1"].stock) == (333, 20)
    assert (items["S2"].price, items["S3"].stock) == (None, None)

    ball = Product.from_dict(items["S1"].payload)
    assert ball.bitrix_id == 101
    assert "игр" in ball.description and "<" not in ball.description
    assert len(ball.category_paths) == 2
    assert ball.sources == {"catalog": "Pricelist20260912.xlsx"}

    issues = [(i.code, i.row_number, i.column, i.sku_1c) for i in service.issues(record.id)]
    assert issues == [("missing_price", 6, "D", "S2"), ("missing_stock", 13, "E", "S3")]


@pytest.mark.parametrize(
    ("name", "make", "code"),
    [
        (
            "wrong-columns.xlsx",
            lambda path: write_xlsx(path, [[["Код", "Название", "Цена"], ["S1", "Мяч", "100"]]]),
            "header_mismatch",
        ),
        (
            "no-products.xlsx",
            lambda path: write_xlsx(path, [[HEADER, [KINDERGARTEN], ["12.04 Мячи"]]]),
            "no_products",
        ),
        ("not-excel.xlsx", lambda path: path.write_bytes(b"this is not a workbook"), "unreadable_file"),
    ],
)
def test_import_invalid(service, tmp_path, name, make, code):
    source = tmp_path / name
    make(source)

    record = service.upload(source, uploaded_by="manager")

    assert record.status is ImportStatus.INVALID
    issues = service.issues(record.id)
    assert [issue.code for issue in issues] == [code]
    assert service.items(record.id) == []
    assert "Файл непригоден" in format_preview(record, issues)


def test_duplicate_import(service, valid_file, tmp_path):
    first = service.upload(valid_file, uploaded_by="manager")
    copy = tmp_path / "prices-copy.xlsx"
    copy.write_bytes(valid_file.read_bytes())

    second = service.upload(copy, uploaded_by="another")

    assert (second.id, second.duplicate, first.duplicate) == (first.id, True, False)
    assert second.file.filename == valid_file.name
    assert len(service.list_imports()) == 1
    assert len(list((tmp_path / "uploads").rglob("*.xlsx"))) == 1
    assert "уже загружали" in format_preview(second, [])

    other = write_xlsx(tmp_path / "next.xlsx", [VALID_ROWS[:6], VALID_BITRIX])
    assert service.upload(other, uploaded_by="manager").id == "2026-09-12-002"


# --- Проверки ------------------------------------------------------------------


def test_row_errors_exclude_products_but_keep_import(service, tmp_path):
    source = write_xlsx(tmp_path / "errors.xlsx", [ERROR_ROWS, ERROR_BITRIX])

    record = service.upload(source, uploaded_by="manager")

    assert record.status is ImportStatus.PARSED
    assert {item.sku_1c for item in service.items(record.id)} == {"X0", "E3"}
    summary = record.summary
    assert (summary.products, summary.accepted, summary.rejected) == (6, 2, 4)
    assert (summary.errors, summary.warnings) == (6, 3)

    found = {(i.severity, i.code, i.sheet, i.row_number) for i in service.issues(record.id)}
    error, warning = Severity.ERROR, Severity.WARNING
    assert found == {
        (error, "missing_code", 1, 5),
        (error, "negative_price", 1, 6),
        (error, "invalid_price", 1, 7),
        (error, "conflicting_rows", 1, 10),
        (error, "code_without_name", 1, 11),
        (error, "negative_stock", 1, 12),
        (warning, "no_section", 1, 2),
        (warning, "fractional_stock", 1, 8),
        (warning, "ambiguous_bitrix_id", 2, 2),
    }
    # Ошибки идут первыми, чтобы в сокращённом предпросмотре их не заслонили оговорки.
    assert service.issues(record.id, limit=1)[0].severity is error
    assert "строке 9" in service.issues(record.id, severity=error)[3].message


def test_upload_rejected_before_storing(repository, tmp_path, valid_file):
    csv = tmp_path / "prices.csv"
    csv.write_text("A;B", encoding="utf-8")

    with pytest.raises(UploadRejected, match="xlsx"):
        make_service(repository, tmp_path).upload(csv, uploaded_by="manager")
    with pytest.raises(UploadRejected, match="больше предела"):
        make_service(repository, tmp_path, max_bytes=100).upload(valid_file, uploaded_by="manager")

    assert repository.list_imports() == []
    assert not (tmp_path / "uploads").exists()


def test_upload_does_not_touch_bot_catalog(repository, tmp_path, valid_file):
    kb = tmp_path / "kb" / "products.jsonl"
    kb.parent.mkdir()
    kb.write_text(
        "".join(json.dumps({"sku_1c": sku, "name": sku}) + "\n" for sku in ("S1", "OLD")),
        encoding="utf-8",
    )
    before = (kb.read_bytes(), kb.stat().st_mtime_ns)
    service = make_service(
        repository,
        tmp_path,
        catalog=lambda: InMemoryCatalogRepository(CatalogIndex(load_products(kb))),
    )

    record = service.upload(valid_file, uploaded_by="manager")

    # Названия в этой базе знаний — коды, поэтому у S1 название не подтверждает код,
    # а новые S2 и S3 не похожи на исчезнувший OLD.
    assert record.summary.comparison == CatalogComparison(
        in_catalog=1,
        new=2,
        missing_from_file=1,
        matching=True,
        existing_by_status={"MATCHED_REVIEW": 1},
        recoding_checked=2,
        recoding_candidates=0,
        recoding_by_status={"NOT_FOUND": 2},
    )
    assert (kb.read_bytes(), kb.stat().st_mtime_ns) == before
    assert sorted(path.name for path in kb.parent.iterdir()) == ["products.jsonl"]
    assert "есть в каталоге 1, новых 2, нет в файле 1" in format_preview(record, [])


def test_failed_parse_is_retried_on_next_upload(service, valid_file, monkeypatch):
    real = import_service.parse_products

    def broken(*_args, **_kwargs):
        raise RuntimeError("сбой разбора")

    monkeypatch.setattr(import_service, "parse_products", broken)
    with pytest.raises(RuntimeError):
        service.upload(valid_file, uploaded_by="manager")
    [stuck] = service.list_imports()
    assert stuck.status is ImportStatus.UPLOADED and "сбой разбора" in stuck.error
    assert "Разбор не завершён" in format_preview(stuck, [])

    monkeypatch.setattr(import_service, "parse_products", real)
    record = service.upload(valid_file, uploaded_by="manager")

    assert (record.id, record.status, record.duplicate, record.error) == (
        stuck.id,
        ImportStatus.PARSED,
        False,
        None,
    )


# --- Один разбор на импорт и базу знаний -----------------------------------------


def test_parser_matches_knowledge_base_build(service, valid_file, tmp_path, monkeypatch):
    """Импорт и `run.py ingest` разбирают выгрузку одним кодом и получают одно и то же."""
    monkeypatch.setattr(norm_registry, "load", lambda *_a, **_kw: {})
    report = build_kb.build(valid_file, tmp_path / "kb")
    lines = (tmp_path / "kb" / "products.jsonl").read_text(encoding="utf-8").splitlines()
    built = {record["sku_1c"]: record for record in map(json.loads, lines)}

    record = service.upload(valid_file, uploaded_by="manager")
    imported = {item.sku_1c: item.payload for item in service.items(record.id)}

    def without_time(products: dict[str, dict]) -> dict[str, dict]:
        return {sku: {k: v for k, v in raw.items() if k != "updated_at"} for sku, raw in products.items()}

    assert without_time(imported) == without_time(built)
    assert (report.headings, report.rows_with_product, report.products) == (
        record.summary.headings,
        record.summary.product_rows,
        record.summary.products,
    )
    assert report.stock_unknown == 1


def test_xlsx_rows_keep_excel_numbers(tmp_path):
    rows = [["a", None, "c"], None, [None, "b"]]
    for path in (
        write_xlsx(tmp_path / "inline.xlsx", [rows]),
        write_xlsx(tmp_path / "shared.xlsx", [rows], shared_strings=True),
    ):
        with XlsxFile(path) as book:
            assert list(book.numbered_rows(0)) == [(1, {"A": "a", "C": "c"}), (3, {"B": "b"})]
            assert list(book.rows(0)) == [{"A": "a", "C": "c"}, {"B": "b"}]


def test_preview_and_list(service, valid_file):
    record = service.upload(valid_file, uploaded_by="manager")

    text = format_preview(record, service.issues(record.id))

    for expected in (
        "Импорт 2026-09-12-001 — PARSED",
        "Товаров (кодов 1С): 3, в нескольких разделах: 1",
        "нет цены: 1",
        "строка 6, колонка D, код S2",
        "база знаний бота не собрана",
        "Каталог бота не изменён",
    ):
        assert expected in text
    assert "2026-09-12-001" in format_imports(service.list_imports())


# --- Миграции ------------------------------------------------------------------


def test_migrations_apply_once(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    SqliteImportRepository(path).close()
    SqliteImportRepository(path).close()

    with closing(sqlite3.connect(path)) as db:
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migrations")]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert versions == ["0001_catalog_import"]
    assert {"files", "catalog_imports", "catalog_import_items", "catalog_import_issues"} <= tables


def test_failed_migration_leaves_no_trace(tmp_path):
    folder = tmp_path / "migrations"
    folder.mkdir()
    (folder / "0001_first.sql").write_text("CREATE TABLE first (x INTEGER);", encoding="utf-8")
    (folder / "0002_broken.sql").write_text(
        "CREATE TABLE second (x INTEGER);\nINSERT INTO missing VALUES (1);", encoding="utf-8"
    )

    with closing(sqlite3.connect(tmp_path / "t.sqlite3")) as db:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(db, folder)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migrations")]

    assert "first" in tables and "second" not in tables
    assert versions == ["0001_first"]
