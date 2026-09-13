"""NEXT-2 Order Core: файл → разбор → нормализация → сопоставление → цена, наличие, норматив → оценка."""

from __future__ import annotations

import csv
import io
import json

import pytest

from catalog.matcher import MatchStatus
from catalog.models import Availability
from catalog.runtime import CatalogRuntime
from core.database import CoreDatabase
from core.errors import Conflict, InvalidRequest
from core_fixtures import (
    INFORMATICS,
    SCHOOL_GYM,
    Clock,
    norm_items,
    procurement_service,
    raw,
    state,
)
from documents.docx import write_document
from documents.xlsx import write_workbook
from norms.mapping import NormCheckStatus
from norms.repository import FileNormRepository
from order_import.evaluation import EvaluationStatus, PriceStatus
from order_import.models import OrderContext, UploadStatus
from order_import.normalizer import dimensions, header_field, parse_money, parse_quantity
from order_import.repository import SqliteOrderRepository
from order_import.service import OrderCoreService
from pdf_fixture import make_pdf

OWNER = "user-1"
HEADER = ["№", "Код 1С", "Наименование", "Кол-во", "Цена, ₽"]
PRESCHOOL_1057 = OrderContext(institution_type="preschool", norm_document="order_1057")


@pytest.fixture
def runtime():
    return CatalogRuntime(state())


def make_orders(tmp_path, runtime, **kwargs) -> OrderCoreService:
    return OrderCoreService(
        SqliteOrderRepository(CoreDatabase(tmp_path / "core.sqlite3")),
        runtime,
        FileNormRepository(norm_items()),
        tmp_path / "uploads",
        clock=Clock(),
        **kwargs,
    )


@pytest.fixture
def orders(tmp_path, runtime):
    return make_orders(tmp_path, runtime)


def xlsx(rows: list[list]) -> bytes:
    return write_workbook("Заказ", [[(value, 0) for value in row] for row in rows], [12] * 12)


def docx(rows: list[list[str]]) -> bytes:
    return write_document("Заявка", ["Организация: МБОУ СОШ № 1"], rows, "", [])


def upload_rows(orders, rows, context=None, name="order.xlsx"):
    return orders.upload(OWNER, "test", name, xlsx(rows), context)


def evaluated(orders, rows, context=None):
    order = upload_rows(orders, rows, context)
    return orders.evaluate(order.id, OWNER)


def line(evaluation, number):
    return next(item for item in evaluation.items if item.line_no == number)


# --- Разбор файлов ---------------------------------------------------------------


def test_excel_with_preamble_and_totals(orders):
    order = upload_rows(
        orders,
        [
            ["ЗАЯВКА на поставку оборудования"],
            ["Организация", "МБДОУ № 5"],
            [],
            [*HEADER, "Документ", "Пункт"],
            ["1", "B2", "Мат детский", "2", "8 164,00", "приказ 1057", "п. 1.5.1.7"],
            ["2", "", "Мяч баскетбольный № 3", "4 шт.", "908"],
            ["", "", "Итого", "6", "17236"],
        ],
    )
    assert order.status is UploadStatus.PARSED and order.parser == "excel" and len(order.items) == 2
    first, second = order.items
    assert (first.article, first.quantity, first.price) == ("B2", 2, 8164)
    assert (first.norm_document, first.norm_item) == ("order_1057", "1.5.1.7")
    assert first.raw["price"] == "8 164,00" and first.source_line == 5 and "МБДОУ" not in first.cells
    assert (second.article, second.name, second.quantity) == (None, "Мяч баскетбольный № 3", 4)
    assert order.catalog_version == "2026-09-13-001" and order.norm_version.startswith("norms-")


def test_word_table(orders):
    order = orders.upload(
        OWNER, "test", "заявка.docx", docx([["№", "Артикул", "Наименование", "Количество", "Цена"], ["1", "", "Кресло компьютерное", "3", "9000"]])
    )
    assert order.parser == "word" and [(i.name, i.quantity, i.price) for i in order.items] == [("Кресло компьютерное", 3, 9000)]


def test_pdf_text_layer(orders):
    pdf = make_pdf(
        [
            "№    Код 1С      Наименование                  Кол-во    Цена",
            "1    I3          Кресло компьютерное           2         9 000",
            "2    B1          Мяч баскетбольный № 3         4         908,00",
        ]
    )
    order = orders.upload(OWNER, "test", "order.pdf", pdf)
    assert order.parser == "pdf"
    assert [(i.article, i.name, i.quantity, i.price) for i in order.items] == [
        ("I3", "Кресло компьютерное", 2, 9000),
        ("B1", "Мяч баскетбольный № 3", 4, 908),
    ]


def test_csv_in_cp1251(orders):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerows([["Артикул", "Наименование", "Количество"], ["C1", "Пробирка лабораторная", "30"]])
    order = orders.upload(OWNER, "test", "order.csv", buffer.getvalue().encode("cp1251"))
    assert [(i.article, i.quantity) for i in order.items] == [("C1", 30)]


def test_scan_without_text_goes_to_review(orders):
    order = orders.upload(OWNER, "test", "scan.pdf", make_pdf([]))
    assert order.items == () and [w.code for w in order.warnings] == ["PDF_NO_TEXT"]
    assert orders.evaluate(order.id, OWNER).status is EvaluationStatus.REJECTED


def test_upload_rejections(tmp_path, runtime, orders):
    cases = [("virus.exe", b"MZ", "UNSUPPORTED_FILE_TYPE"), ("empty.xlsx", b"", "UPLOAD_REJECTED"), ("fake.xlsx", b"hello", "UPLOAD_REJECTED")]
    for name, content, code in cases:
        with pytest.raises(InvalidRequest) as error:
            orders.upload(OWNER, "test", name, content)
        assert error.value.code == code
    small = make_orders(tmp_path / "small", runtime, max_bytes=10)
    with pytest.raises(InvalidRequest) as error:
        small.upload(OWNER, "test", "big.csv", b"a;b\n" * 10)
    assert error.value.details["reason"] == "too_large"


def test_broken_file_is_kept_with_error(orders):
    order = orders.upload(OWNER, "test", "broken.xlsx", b"PK\x03\x04not a zip")
    assert order.status is UploadStatus.FAILED and "не читается" in order.error
    with pytest.raises(Conflict):
        orders.evaluate(order.id, OWNER)


def test_same_file_is_not_uploaded_twice(orders):
    content = xlsx([HEADER, ["1", "B2", "Мат детский", "1", "8164"]])
    first = orders.upload(OWNER, "test", "a.xlsx", content)
    assert orders.upload(OWNER, "test", "b.xlsx", content).id == first.id
    assert orders.upload("user-2", "test", "a.xlsx", content).id != first.id


def test_normalizer_values():
    assert parse_quantity("2,0") == 2 and parse_quantity("5 шт.") == 5
    assert parse_quantity("два") is None and parse_quantity("1,5") is None
    assert parse_money("12 500,00 ₽") == 12500 and parse_money("12.500,40") == 12500
    assert parse_money("1.234.567") == 1234567 and parse_money("-5") is None
    assert dimensions("Мат детский 140*140*6 см") == "140x140x6"
    assert header_field("Цена, ₽") == "price" and header_field("Кол-во") == "quantity"
    assert header_field("Источник количества") is None and header_field("Основание подбора") is None


def test_missing_and_invalid_quantity(orders):
    evaluation = evaluated(orders, [HEADER, ["1", "B2", "Мат детский", "", "8164"], ["2", "B1", "Мяч баскетбольный № 3", "много", "908"]])
    assert [n.code for n in line(evaluation, 1).errors] == ["QUANTITY_UNKNOWN"]
    assert [n.code for n in line(evaluation, 2).errors] == ["QUANTITY_INVALID"]
    assert evaluation.status is EvaluationStatus.REVIEW_REQUIRED and evaluation.summary["quantity_unknown"] == 2


# --- Сопоставление -------------------------------------------------------------------


def test_match_priority_and_statuses(orders):
    evaluation = evaluated(
        orders,
        [
            HEADER,
            ["1", "B2", "Мат детский", "1", "8164"],
            ["2", "", "Доска ребристая", "1", "12748"],
            ["3", "", 'Кресло "компьютерное"', "1", "9000"],
            ["4", "", "Ноутбук ученичесий", "1", "50000"],
            ["5", "", "Телескоп космический", "1", "1"],
        ],
    )
    got = {item.line_no: (item.match_status, item.match_method) for item in evaluation.items}
    assert got[1] == (MatchStatus.MATCHED_EXACT, "code_1c")
    assert got[2] == (MatchStatus.MATCHED_HIGH, "exact_name")
    assert got[3] == (MatchStatus.MATCHED_HIGH, "normalized_name")
    assert got[4][0] is MatchStatus.MATCHED_REVIEW
    assert got[5][0] is MatchStatus.NOT_FOUND
    assert line(evaluation, 4).status is EvaluationStatus.REVIEW_REQUIRED
    assert evaluation.status is EvaluationStatus.REVIEW_REQUIRED
    summary = evaluation.summary
    assert (summary["matched"], summary["match_review"], summary["not_found"]) == (3, 1, 1)


def test_ambiguous_and_model_suggestion(tmp_path):
    twin = raw("SB3", "Мяч волейбольный", [[*SCHOOL_GYM, "1.7.13. Мяч волейбольный"]], price=1300, stock=2)
    runtime = CatalogRuntime(state(extra=[twin]))
    rows = [HEADER, ["1", "", "Мяч волейбольный", "2", "1200"]]
    plain = evaluated(make_orders(tmp_path / "plain", runtime), rows)
    assert line(plain, 1).match_status is MatchStatus.AMBIGUOUS and line(plain, 1).product_id is None

    class Assistant:
        def __init__(self, answer):
            self.answer = answer

        def suggest(self, item, candidates):
            return self.answer

    helped = evaluated(make_orders(tmp_path / "ai", runtime, assistant=Assistant("SB3")), rows)
    assert (line(helped, 1).match_status, line(helped, 1).match_method, line(helped, 1).product_id) == (MatchStatus.MATCHED_REVIEW, "ai_assisted", "SB3")
    invented = evaluated(make_orders(tmp_path / "bad", runtime, assistant=Assistant("NOT-A-CANDIDATE")), rows)
    assert line(invented, 1).match_status is MatchStatus.AMBIGUOUS


def test_all_rows_not_found_is_rejected(orders):
    evaluation = evaluated(orders, [HEADER, ["1", "", "Телескоп космический", "1", "1"]])
    assert evaluation.status is EvaluationStatus.REJECTED


def test_manual_match_applies_on_next_evaluation(orders):
    order = upload_rows(orders, [HEADER, ["1", "", "Мат гимнастический складной", "1", "8000"]])
    assert line(orders.evaluate(order.id, OWNER), 1).match_status is not MatchStatus.MATCHED_EXACT
    with pytest.raises(InvalidRequest):
        orders.manual_match(order.id, 1, "NOPE", "manager")
    orders.manual_match(order.id, 1, "B2", "manager")
    item = line(orders.evaluate(order.id, OWNER), 1)
    assert (item.match_status, item.match_method, item.product_id) == (MatchStatus.MATCHED_EXACT, "manual", "B2")


# --- Цена, наличие, норматив -----------------------------------------------------------


def test_price_changed_and_missing(orders):
    evaluation = evaluated(orders, [HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "800"], ["2", "I4", "Принтер 3D учебный", "1", "90000"]])
    changed = line(evaluation, 1)
    assert (changed.price_status, changed.price_delta, changed.price_delta_pct) == (PriceStatus.PRICE_CHANGED, 108, 13.5)
    assert changed.current_price == 908 and changed.status is EvaluationStatus.READY_WITH_WARNINGS
    missing = line(evaluation, 2)
    assert missing.price_status is PriceStatus.PRICE_NOT_FOUND and "PRICE_NOT_FOUND" in [n.code for n in missing.errors]
    assert evaluation.summary["price_changed"] == 1 and evaluation.summary["document_amount"] == 1600 + 90000


def test_document_without_price_uses_current(orders):
    order = orders.upload(OWNER, "test", "no-price.csv", "Код 1С;Наименование;Кол-во\nB2;Мат детский;1\n".encode())
    item = line(orders.evaluate(order.id, OWNER), 1)
    assert item.price_status is PriceStatus.PRICE_OK and [n.code for n in item.warnings] == ["DOCUMENT_PRICE_MISSING"]


def test_availability_statuses(tmp_path):
    unknown = raw("U1", "Лупа учебная", [INFORMATICS], price=300, stock=None)
    runtime = CatalogRuntime(state(extra=[unknown]))
    evaluation = evaluated(
        make_orders(tmp_path, runtime),
        [
            HEADER,
            ["1", "B1", "Мяч баскетбольный № 3", "2", "908"],
            ["2", "B3", "Доска ребристая", "1", "12748"],
            ["3", "U1", "Лупа учебная", "1", "300"],
            ["4", "B2", "Мат детский", "10", "8164"],
        ],
    )
    assert line(evaluation, 1).availability is Availability.AVAILABLE and line(evaluation, 1).status is EvaluationStatus.READY
    assert "NOT_AVAILABLE" in [n.code for n in line(evaluation, 2).warnings]
    unknown_line = line(evaluation, 3)
    assert unknown_line.availability is Availability.UNKNOWN and unknown_line.availability is not Availability.NOT_AVAILABLE
    assert [n.code for n in unknown_line.warnings] == ["UNKNOWN_STOCK"]
    assert "INSUFFICIENT_STOCK" in [n.code for n in line(evaluation, 4).warnings]
    assert evaluation.summary["unknown_stock"] == 1 and evaluation.summary["not_available"] == 1


def test_norm_checks(orders):
    rows = [
        [*HEADER, "Документ", "Пункт"],
        ["1", "B2", "Мат детский", "1", "8164", "1057", "1.5.1"],
        ["2", "B2", "Мат детский", "2", "8164", "1057", "1.14"],
    ]
    evaluation = evaluated(orders, rows, PRESCHOOL_1057)
    assert line(evaluation, 1).norm_status is NormCheckStatus.NORM_OK
    assert line(evaluation, 2).norm_status is NormCheckStatus.NORM_MISMATCH
    assert "NORM_MISMATCH" in [n.code for n in line(evaluation, 2).errors]

    school = evaluated(orders, [HEADER, ["1", "I4", "Принтер 3D учебный", "1", ""]], OrderContext("school", "order_838"))
    assert line(school, 1).norm_status is NormCheckStatus.NORM_UNKNOWN and "NORM_UNKNOWN" in [n.code for n in line(school, 1).warnings]

    free = evaluated(orders, [HEADER, ["1", "I3", "Кресло компьютерное", "1", "9000"]])
    assert line(free, 1).norm_reason == "NORM_NOT_REQUESTED" and line(free, 1).status is EvaluationStatus.READY
    assert free.status is EvaluationStatus.READY


def test_evaluation_is_bound_to_catalog_version(orders, runtime):
    order = upload_rows(orders, [HEADER, ["1", "B1", "Мяч баскетбольный № 3", "1", "908"]])
    first = orders.evaluate(order.id, OWNER)
    runtime.replace(state("2026-09-13-002", B1={"price": 1000}))
    second = orders.evaluate(order.id, OWNER)
    assert (first.catalog_version, line(first, 1).price_status) == ("2026-09-13-001", PriceStatus.PRICE_OK)
    assert (second.catalog_version, line(second, 1).current_price) == ("2026-09-13-002", 1000)
    assert orders.latest_evaluation(order.id, OWNER).id == second.id
    assert json.loads(json.dumps(second.to_dict()))["items"][0]["price_status"] == "PRICE_CHANGED"


# --- Сквозной сценарий: спецификация → файл → проверка ------------------------------------


@pytest.mark.parametrize("fmt", ["xlsx", "docx"])
def test_exported_specification_passes_order_check(tmp_path, runtime, fmt):
    procurement = procurement_service(tmp_path, runtime)
    task = procurement.create_task(OWNER, "test", text="Детский сад, по приказу 1057 пункт 1.5.1")
    procurement.choose(task.id, OWNER, [item.product_id for item in procurement.select(task.id, OWNER).items])
    spec = procurement.build_specification(task.id, OWNER)
    document = procurement.export_specification(spec.id, OWNER, fmt)

    orders = make_orders(tmp_path, runtime)
    order = orders.upload(OWNER, "test", document.filename, document.content, PRESCHOOL_1057)
    evaluation = orders.evaluate(order.id, OWNER)
    assert len(evaluation.items) == len(spec.items)
    by_product = {item.product_id: item for item in evaluation.items}
    for spec_item in spec.items:
        checked = by_product[spec_item.product_id]
        assert checked.match_status is MatchStatus.MATCHED_EXACT and checked.quantity == spec_item.quantity
        assert checked.price_status is PriceStatus.PRICE_OK and checked.norm_status is NormCheckStatus.NORM_OK
    assert evaluation.summary["current_amount"] == spec.totals.amount
    assert evaluation.status is EvaluationStatus.READY_WITH_WARNINGS  # доска ребристая не в наличии
