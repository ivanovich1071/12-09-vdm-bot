"""NEXT-1 Procurement Core: задача → норматив → подбор → количество → спецификация → документы."""

from __future__ import annotations

import ast
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from catalog.runtime import CatalogRuntime
from core.errors import Conflict, Forbidden, InvalidRequest, NotFound
from core_fixtures import procurement_service, state
from documents.exporters import export_specification
from ingest.xlsx_reader import XlsxFile
from norms.mapping import NormCheckStatus
from procurement import discovery
from procurement.models import (
    FreshnessStatus,
    ProcurementTask,
    QuantitySource,
    SelectionStatus,
    SpecificationStatus,
    Stage,
)
from procurement.selector import LLM_CANDIDATE_LIMIT, GuardedRanker
from procurement.specification import SpecificationLine, validate_specification

OWNER = "user-1"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@pytest.fixture
def runtime():
    return CatalogRuntime(state())


@pytest.fixture
def service(tmp_path, runtime):
    return procurement_service(tmp_path, runtime)


def task_from(service, text, **fields):
    return service.create_task(OWNER, "test", text=text, fields=fields or None)


def ids(result):
    return [item.product_id for item in result.items]


# --- Понимание задачи ------------------------------------------------------------


def test_task_from_short_school_request(service):
    task = task_from(service, "Школа, кабинет информатики")
    assert (task.institution_type, task.room) == ("school", "кабинет информатики")
    assert task.preferences == {} and task.stage is Stage.DISCOVERY


def test_discovery_reads_procurement_facts():
    task = ProcurementTask(id="t", owner="o", channel="c", created_at="", updated_at="")
    changed = discovery.apply_text(
        task,
        "Детский сад, дети 3-4 лет, 2 группы, на 25 детей, бюджет 200 тысяч, "
        "по приказу 1057 пункт 1.5.1, нужно 10 штук в наличии",
    )
    assert task.institution_type == "preschool" and task.age_group == "3–4 лет"
    assert (task.budget, task.quantity, task.norm_document, task.norm_item) == (200_000, 10, "order_1057", "1.5.1")
    assert task.norm_required is True
    assert task.preferences["participants"] == 25 and task.preferences["groups"] == 2
    assert task.preferences["available_only"] is True
    assert {"institution_type", "budget", "norm_item"} <= set(changed)


def test_discovery_grade_and_without_norm():
    task = ProcurementTask(id="t", owner="o", channel="c", created_at="", updated_at="")
    discovery.apply_text(task, "школа, 5-9 классы, без норматива")
    assert task.grade == "5–9 классы" and task.norm_required is False and task.norm_item is None


def test_numbers_are_not_taken_for_points():
    task = ProcurementTask(id="t", owner="o", channel="c", created_at="", updated_at="")
    discovery.apply_text(task, "бюджет 1.5 млн, дети 2.5 года")
    assert task.norm_item is None and task.budget == 1_500_000


def test_query_keeps_only_product_words():
    assert discovery.query_from_text("Нужны мячи для спортзала в саду, дети 3-4 лет") == "мячи"
    assert discovery.query_from_text("Школа, кабинет информатики, бюджет до 500 тысяч") == ""


def test_known_institution_is_not_overwritten(service):
    task = task_from(service, "детский сад, спортзал")
    task = service.update_task(task.id, OWNER, text="а в школе у нас кабинет химии")
    assert task.institution_type == "preschool" and task.room == "кабинет химии"


def test_unknown_field_and_bad_type_are_rejected(service):
    with pytest.raises(InvalidRequest) as error:
        service.create_task(OWNER, "test", fields={"phone": "+7"})
    assert error.value.code == "UNKNOWN_FIELD"
    with pytest.raises(InvalidRequest):
        service.create_task(OWNER, "test", fields={"budget": "много"})


# --- Подбор ----------------------------------------------------------------------------


def test_first_pass_school_informatics(service):
    task = task_from(service, "Школа, кабинет информатики")
    result = service.select(task.id, OWNER)
    assert result.status is SelectionStatus.FOUND and len(result.items) == 3
    assert set(ids(result)) <= {"I1", "I2", "I3", "I4"} and result.has_more and result.remaining == 1
    assert result.catalog_version == "2026-09-13-001" and result.norm_version.startswith("norms-")
    first = result.items[0]
    assert first.norm_status is NormCheckStatus.NORM_OK and "Кабинет информатики" in first.reason
    assert service.get_task(task.id, OWNER).stage is Stage.PRESENTATION


def test_show_more_never_repeats(service):
    task = task_from(service, "Школа, кабинет информатики")
    first = service.select(task.id, OWNER)
    second = service.select(task.id, OWNER)
    assert not set(ids(first)) & set(ids(second)) and len(second.items) == 1 and not second.has_more
    assert service.select(task.id, OWNER).status is SelectionStatus.EMPTY
    assert len(service.select(task.id, OWNER, restart=True).items) == 3


def test_changed_task_starts_new_selection(service):
    task = task_from(service, "Школа, кабинет информатики")
    service.select(task.id, OWNER)
    service.update_task(task.id, OWNER, text="кабинет химии")
    assert ids(service.select(task.id, OWNER)) == ["C1"]


def test_asks_only_when_nothing_to_search(service):
    empty = service.select(task_from(service, "здравствуйте").id, OWNER)
    assert empty.status is SelectionStatus.NEEDS_DETAILS and empty.questions == ("institution_type", "room")
    institution_only = service.select(task_from(service, "мы школа").id, OWNER)
    assert institution_only.questions == ("room",) and not institution_only.items


def test_preschool_age_group_prefers_own_group(service):
    task = task_from(service, "Детский сад, групповая комната для детей 3-4 лет")
    result = service.select(task.id, OWNER)
    assert ids(result) == ["G34", "G23"]


def test_norm_selection_1057_quantities_from_order(service):
    task = task_from(service, "Детский сад, по приказу 1057 пункт 1.5.1")
    result = service.select(task.id, OWNER)
    assert result.norm.filters and set(ids(result)) == {"B1", "B2", "B3"}
    quantities = {item.product_id: (item.quantity, item.quantity_source) for item in result.items}
    assert quantities == {
        "B1": (4, QuantitySource.NORM),
        "B2": (1, QuantitySource.NORM),
        "B3": (2, QuantitySource.NORM),
    }
    assert all(item.confidence == 0.95 for item in result.items)


def test_selection_without_norm_keeps_unmapped_products(service):
    task = task_from(service, "Нужны мячи для спортзала в школе, без норматива")
    result = service.select(task.id, OWNER)
    assert set(ids(result)) == {"SB1", "SB2"} and result.norm.status.value == "NOT_REQUESTED"


def test_hard_filters_keep_foreign_sections_out(service):
    task = task_from(service, "Школа, кабинет информатики")
    shown = ids(service.select(task.id, OWNER)) + ids(service.select(task.id, OWNER))
    assert not {"C1", "B1", "SB1", "G34"} & set(shown)


def test_budget_filters_expensive_items(service):
    task = task_from(service, "Детский сад, спортивный зал, бюджет до 10 тысяч")
    result = service.select(task.id, OWNER)
    assert ids(result) == ["B2"]
    budget = next(f for f in result.filters if f["name"] == "price")
    assert budget["excluded"] == 2


def test_budget_warning_when_page_exceeds_budget(service):
    task = task_from(service, "Детский сад, спортивный зал", budget=13000)
    result = service.select(task.id, OWNER)
    assert set(ids(result)) == {"B2", "B3"}
    assert any(notice.code == "BUDGET_EXCEEDED" for notice in result.warnings)


def test_available_only(service):
    task = task_from(service, "Школа, кабинет информатики, в наличии")
    assert "I2" not in ids(service.select(task.id, OWNER)) + ids(service.select(task.id, OWNER))


def test_rejected_are_not_offered_again(service):
    task = task_from(service, "Школа, кабинет информатики")
    first = service.select(task.id, OWNER)
    task = service.update_task(task.id, OWNER, text="дорого")
    assert set(task.rejected_products) == set(ids(first)) and task.objections == ["price"]
    assert task.stage is Stage.OBJECTION
    again = service.select(task.id, OWNER, restart=True)
    assert not set(ids(again)) & set(ids(first))


def test_alternatives_come_from_same_section(service):
    task = task_from(service, "Школа, кабинет информатики")
    item = service.select(task.id, OWNER).items[0]
    assert item.alternatives and all(alt.product_id.startswith("I") for alt in item.alternatives)


def test_kindergarten_with_school_document_is_review(service):
    task = task_from(service, "Детский сад, спортзал, по приказу 838")
    result = service.select(task.id, OWNER)
    assert result.status is SelectionStatus.REVIEW_REQUIRED
    assert all(item.norm_status is not NormCheckStatus.NORM_OK for item in result.items)
    assert any(notice.code == "NORM_REVIEW_REQUIRED" for notice in result.warnings)


def test_model_ranker_sees_only_filtered_candidates(tmp_path, runtime):
    seen: list[list[dict]] = []

    def reorder(requirement, candidates):
        seen.append(candidates)
        return ["NOT-IN-CATALOG", candidates[-1]["id"]]

    service = procurement_service(tmp_path, runtime, ranker=GuardedRanker(reorder, limit=2))
    result = service.select(task_from(service, "Школа, кабинет информатики").id, OWNER)
    assert len(seen[0]) == 2 <= LLM_CANDIDATE_LIMIT
    assert all("description" not in candidate for candidate in seen[0])
    assert ids(result)[0] == seen[0][-1]["id"] and "NOT-IN-CATALOG" not in ids(result)


def test_ranker_failure_falls_back(tmp_path, runtime):
    def broken(requirement, candidates):
        raise TimeoutError("модель не ответила")

    service = procurement_service(tmp_path, runtime, ranker=GuardedRanker(broken))
    assert len(service.select(task_from(service, "Школа, кабинет информатики").id, OWNER).items) == 3


# --- Количество --------------------------------------------------------------------------


def test_quantity_sources(service):
    task = task_from(service, "Детский сад, групповая комната 3-4 лет")
    service.update_task(task.id, OWNER, fields={"participants": 20, "groups": 3})
    items = {item.product_id: item for item in service.select(task.id, OWNER).items}
    assert (items["G34"].quantity, items["G34"].quantity_source) == (20, QuantitySource.CALCULATED)
    assert (items["G23"].quantity, items["G23"].quantity_source) == (3, QuantitySource.CALCULATED)

    service.set_quantity(task.id, OWNER, "G34", 7)
    spec = service.build_specification(task.id, OWNER, [SpecificationLine("G34"), SpecificationLine("I3")])
    lines = {line.product_id: line for line in spec.items}
    assert (lines["G34"].quantity, lines["G34"].quantity_source) == (7, QuantitySource.USER)
    assert (lines["I3"].quantity, lines["I3"].quantity_source) == (1, QuantitySource.DEFAULT)


def test_backend_only_quantity_sources(service):
    task = task_from(service, "Школа, кабинет информатики")
    with pytest.raises(InvalidRequest) as error:
        service.set_quantity(task.id, OWNER, "I1", 5, source=QuantitySource.NORM)
    assert error.value.code == "QUANTITY_SOURCE_NOT_ALLOWED"
    with pytest.raises(Forbidden):
        service.set_quantity(task.id, OWNER, "I1", 5, source=QuantitySource.MANAGER)
    service.set_quantity(task.id, OWNER, "I1", 5, source=QuantitySource.MANAGER, manager=True)
    with pytest.raises(InvalidRequest):
        service.build_specification(task.id, OWNER, [SpecificationLine("I1", 2, QuantitySource.NORM)])


def test_user_quantity_keeps_norm_quantity_visible(service):
    task = task_from(service, "Детский сад, по приказу 1057 пункт 1.5.1, 10 штук")
    item = next(i for i in service.select(task.id, OWNER).items if i.product_id == "B1")
    assert (item.quantity, item.quantity_source) == (10, QuantitySource.USER)


# --- Спецификация ------------------------------------------------------------------------


def chosen_spec(service, text="Детский сад, по приказу 1057 пункт 1.5.1"):
    task = task_from(service, text)
    result = service.select(task.id, OWNER)
    service.choose(task.id, OWNER, ids(result))
    return task, service.build_specification(task.id, OWNER)


def test_specification_totals_versions_and_norms(service):
    task, spec = chosen_spec(service)
    assert spec.catalog_version == "2026-09-13-001" and spec.norm_version.startswith("norms-")
    assert spec.totals.positions == 3 and spec.totals.quantity == 7
    assert spec.totals.amount == 908 * 4 + 8164 * 1 + 12748 * 2 and spec.totals.complete
    assert all(item.norm_document == "order_1057" and item.norm_status is NormCheckStatus.NORM_OK for item in spec.items)
    assert {item.norm_item for item in spec.items} == {"1.5.1.33", "1.5.1.7", "1.5.1.13"}
    assert spec.header["norm_citation"].startswith("позиция 1.5.1") and not validate_specification(spec)
    assert service.get_specification(spec.id, OWNER) == spec
    assert service.get_task(task.id, OWNER).stage is Stage.CART


def test_specification_is_bound_to_catalog_version(service, runtime):
    task, spec = chosen_spec(service)
    runtime.replace(state("2026-09-13-002", B2={"price": 9000}))

    stored = service.get_specification(spec.id, OWNER)
    assert stored.catalog_version == "2026-09-13-001"
    assert next(i for i in stored.items if i.product_id == "B2").unit_price == 8164

    freshness = service.check_specification(spec.id, OWNER)
    assert freshness.status is FreshnessStatus.CATALOG_CHANGED
    assert [(c.product_id, c.field, c.old, c.new) for c in freshness.changes] == [("B2", "price", 8164, 9000)]

    revised = service.revise_specification(spec.id, OWNER)
    assert revised.parent_id == spec.id and revised.catalog_version == "2026-09-13-002"
    assert next(i for i in revised.items if i.product_id == "B2").unit_price == 9000
    assert service.get_specification(spec.id, OWNER).status is SpecificationStatus.SUPERSEDED
    with pytest.raises(Conflict):
        service.revise_specification(spec.id, OWNER)


def test_specification_rejects_unknown_product_and_bad_quantity(service):
    task = task_from(service, "Школа, кабинет информатики")
    with pytest.raises(InvalidRequest) as error:
        service.build_specification(task.id, OWNER, [SpecificationLine("NOPE")])
    assert error.value.code == "UNKNOWN_PRODUCT"
    with pytest.raises(InvalidRequest) as error:
        service.build_specification(task.id, OWNER, [SpecificationLine("I1", 0)])
    assert error.value.code == "INVALID_QUANTITY"
    with pytest.raises(InvalidRequest) as error:
        service.build_specification(task.id, OWNER)
    assert error.value.code == "EMPTY_SPECIFICATION"


def test_duplicates_are_merged(service):
    task = task_from(service, "Школа, кабинет информатики")
    spec = service.build_specification(task.id, OWNER, [SpecificationLine("I1", 2), SpecificationLine("I1", 3)])
    assert len(spec.items) == 1 and spec.items[0].quantity == 5
    assert any(notice.code == "DUPLICATE_MERGED" for notice in spec.warnings)


def test_missing_price_and_unavailable_are_visible(service):
    task = task_from(service, "Школа, кабинет информатики")
    spec = service.build_specification(task.id, OWNER, [SpecificationLine("I4"), SpecificationLine("I2")])
    assert not spec.totals.complete and spec.totals.missing_prices == 1 and spec.totals.amount == 250000
    codes = {notice.code for notice in spec.warnings}
    assert {"PRICE_MISSING", "NOT_AVAILABLE", "DEFAULT_QUANTITY"} <= codes


def test_specification_marks_mapping_to_other_point(service):
    task = task_from(service, "Детский сад, по приказу 1057 пункт 1.14")
    spec = service.build_specification(task.id, OWNER, [SpecificationLine("B2")])
    assert spec.items[0].norm_status is NormCheckStatus.NORM_MISMATCH
    assert any(notice.code == "NORM_MISMATCH" for notice in spec.warnings)


def test_validation_catches_tampered_totals(service):
    _, spec = chosen_spec(service)
    tampered = replace(spec, items=(replace(spec.items[0], total_price=1), *spec.items[1:]))
    codes = {issue.code for issue in validate_specification(tampered)}
    assert {"TOTAL_MISMATCH", "TOTALS_MISMATCH"} <= codes
    with pytest.raises(InvalidRequest) as error:
        export_specification(tampered, "xlsx")
    assert error.value.code == "INVALID_SPECIFICATION"


def test_owner_isolation(service):
    task, spec = chosen_spec(service)
    with pytest.raises(NotFound):
        service.get_task(task.id, "someone-else")
    with pytest.raises(NotFound):
        service.get_specification(spec.id, "someone-else")


def test_closed_task_does_not_change(service):
    task = task_from(service, "Школа, кабинет информатики")
    service.abandon(task.id, OWNER)
    with pytest.raises(Conflict) as error:
        service.select(task.id, OWNER)
    assert error.value.code == "TASK_CLOSED"


# --- Документы ---------------------------------------------------------------------------


def test_excel_export_columns_articles_and_totals(service, tmp_path):
    _, spec = chosen_spec(service)
    document = service.export_specification(spec.id, OWNER, "xlsx")
    path = tmp_path / document.filename
    path.write_bytes(document.content)
    with XlsxFile(path) as book:
        rows = list(book.rows(0))
    header = next(i for i, row in enumerate(rows) if row.get("B") == "Код 1С")
    assert [rows[header][c] for c in "ABCDEFG"] == ["№", "Код 1С", "Наименование", "Кол-во", "Ед.", "Цена, ₽", "Сумма, ₽"]
    lines = rows[header + 1 : header + 1 + len(spec.items)]
    for row, item in zip(lines, spec.items, strict=True):
        assert row["B"] == item.article and int(row["D"]) == item.quantity
        assert int(row["F"]) * int(row["D"]) == int(row["G"]) == item.total_price
        assert row["J"] == "приказ № 1057" and row["K"] == item.norm_item
    assert int(rows[header + 1 + len(spec.items)]["G"]) == spec.totals.amount
    assert any(row.get("B") == spec.catalog_version for row in rows[:header])


def test_word_export_table(service):
    _, spec = chosen_spec(service)
    document = service.export_specification(spec.id, OWNER, "docx")
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(document.content)).read("word/document.xml"))
    rows = root.findall(f".//{W}tbl/{W}tr")
    assert len(rows) == len(spec.items) + 1
    cells = [["".join(t.text or "" for t in cell.iter(f"{W}t")) for cell in row.findall(f"{W}tc")] for row in rows]
    assert cells[0][1] == "Код 1С" and [row[1] for row in cells[1:]] == [item.article for item in spec.items]
    text = "".join(t.text or "" for t in root.iter(f"{W}t"))
    assert f"сумма {spec.totals.amount} ₽" in text and spec.catalog_version in text


def test_unsupported_format(service):
    _, spec = chosen_spec(service)
    with pytest.raises(InvalidRequest) as error:
        service.export_specification(spec.id, OWNER, "pdf")
    assert error.value.code == "UNSUPPORTED_FORMAT"


# --- Независимость от каналов ---------------------------------------------------------


FORBIDDEN = ("aiogram", "adapters", "web", "fastapi", "core.dialog", "core.app", "agent")


def test_procurement_core_does_not_know_channels():
    root = Path(__file__).parents[1] / "src"
    for package in ("procurement", "norms", "documents"):
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert not name.startswith(FORBIDDEN), f"{path.name} импортирует {name}"
