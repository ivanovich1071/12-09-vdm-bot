"""NEXT-1: нормативный движок — документ → пункт → привязка → товар."""

from __future__ import annotations

import pytest

from core_fixtures import norm_items, products
from norms.items import NormItem
from norms.mapping import MappingStatus, NormCheckStatus, NormMappingService
from norms.repository import FileNormRepository, QuantityRule, norm_quantity, quantity_rule
from norms.selector import NormQuery, NormReason, NormResolutionStatus, NormSelector


@pytest.fixture
def engine():
    repository = FileNormRepository(norm_items())
    mapping = NormMappingService(repository)
    catalog = {product.id: product for product in products()}
    return repository, mapping, NormSelector(repository, mapping), catalog


def resolve(engine, **kwargs):
    _, _, selector, catalog = engine
    return selector.resolve(NormQuery(**kwargs), catalog.values())


# --- Нормативная база -------------------------------------------------------------


def test_norm_version_is_stable_and_follows_items():
    first, second = FileNormRepository(norm_items()), FileNormRepository(norm_items())
    assert first.version == second.version and first.version.startswith("norms-")
    changed = norm_items()
    changed["order_838"]["2.18.5"] = NormItem("order_838", "2.18.5", "Ноутбук учителя")
    assert FileNormRepository(changed).version != first.version


def test_norm_quantity_is_only_a_whole_number():
    assert norm_quantity(NormItem("order_1057", "1", "x", quantity="4")) == 4
    assert norm_quantity(NormItem("order_1057", "1", "x", quantity=") 1")) is None
    assert norm_quantity(NormItem("order_1057", "1", "x", quantity="По количест ву окон")) is None
    assert norm_quantity(NormItem("order_1057", "1", "x", quantity="0")) is None


def test_quantity_rule_survives_broken_pdf_spacing():
    assert quantity_rule(NormItem("order_1057", "1", "x", quantity="По количест ву детей в группе")) is QuantityRule.PER_CHILD
    assert quantity_rule(NormItem("order_1057", "1", "x", quantity="1 Шт. на каждую группу")) is QuantityRule.PER_GROUP


# --- Выбор документа и пункта --------------------------------------------------------


def test_document_by_institution_when_norm_not_requested(engine):
    school = resolve(engine, institution_type="школа")
    assert school.status is NormResolutionStatus.NOT_REQUESTED and school.document == "order_838"
    preschool = resolve(engine, institution_type="детский сад")
    assert preschool.document == "order_1057" and not preschool.filters


def test_1057_point_resolved_with_norm_quantity(engine):
    result = resolve(engine, institution_type="preschool", norm_document="1057", norm_item="1.5.1.33")
    assert result.status is NormResolutionStatus.RESOLVED
    assert (result.document, result.point, result.quantity, result.unit) == ("order_1057", "1.5.1.33", 4, "Шт.")
    assert result.filters and NormReason.POINT_FROM_USER in result.reasons


def test_838_point_resolved_by_document_name(engine):
    result = resolve(engine, institution_type="school", norm_document="приказ № 838", norm_item="п. 2.18.5")
    assert result.status is NormResolutionStatus.RESOLVED
    assert (result.document, result.point, result.point_title) == ("order_838", "2.18.5", "Ноутбук")
    assert result.quantity is None


def test_school_document_for_kindergarten_is_not_substituted(engine):
    result = resolve(engine, institution_type="детский сад", norm_document="838")
    assert result.status is NormResolutionStatus.REVIEW_REQUIRED
    assert result.document == "order_838" and not result.filters
    assert NormReason.DOCUMENT_INSTITUTION_CONFLICT in result.reasons


def test_point_in_two_documents_without_institution_is_ambiguous(engine):
    result = resolve(engine, norm_item="1.5.1")
    assert result.status is NormResolutionStatus.REVIEW_REQUIRED
    assert NormReason.POINT_IN_SEVERAL_DOCUMENTS in result.reasons
    assert {doc for doc, _ in result.candidates} == {"order_838", "order_1057"}


def test_institution_picks_document_for_shared_point(engine):
    result = resolve(engine, institution_type="preschool", norm_item="1.5.1")
    assert result.status is NormResolutionStatus.RESOLVED and result.document == "order_1057"
    assert NormReason.DOCUMENT_FROM_POINT in result.reasons


def test_unknown_document_requires_review(engine):
    result = resolve(engine, institution_type="school", norm_document="приказ 9999")
    assert result.status is NormResolutionStatus.REVIEW_REQUIRED
    assert result.reasons == (NormReason.DOCUMENT_UNKNOWN,)


def test_point_absent_from_document_requires_review(engine):
    result = resolve(engine, institution_type="school", norm_document="838", norm_item="9.9.9")
    assert NormReason.POINT_NOT_IN_DOCUMENT in result.reasons and result.requires_review


def test_norm_requested_without_institution_requires_review(engine):
    result = resolve(engine, required=True)
    assert result.reasons == (NormReason.INSTITUTION_UNKNOWN,) and result.requires_review


def test_point_without_products_requires_review(engine):
    result = resolve(engine, institution_type="school", norm_document="838", norm_item="2.15.1")
    assert result.requires_review and NormReason.NO_PRODUCT_MAPPING in result.reasons


def test_point_known_only_from_catalog_mapping(engine):
    result = resolve(engine, institution_type="preschool", norm_document="1057", norm_item="1.9.9")
    assert result.status is NormResolutionStatus.RESOLVED
    assert NormReason.POINT_ONLY_IN_CATALOG in result.reasons


def test_point_without_catalog_is_checked_by_text_only(engine):
    repository, mapping, selector, _ = engine
    result = selector.resolve(NormQuery(norm_document="838", norm_item="2.15.1", institution_type="school"))
    assert result.status is NormResolutionStatus.RESOLVED


# --- Проверка товара -------------------------------------------------------------------


def test_norm_check_statuses(engine):
    _, mapping, _, catalog = engine
    ok = mapping.check(catalog["B2"], "order_1057", "1.5.1")
    assert ok.status is NormCheckStatus.NORM_OK and ok.mapping.item_code == "1.5.1.7"
    assert mapping.check(catalog["B2"], "order_1057", "1.14").status is NormCheckStatus.NORM_MISMATCH
    assert mapping.check(catalog["B4"], "order_1057", "1.5.1").status is NormCheckStatus.REVIEW_REQUIRED
    assert mapping.check(catalog["B4"], "order_1057").reason == "WEAK_MAPPING"
    assert mapping.check(catalog["I4"], "order_838").status is NormCheckStatus.NORM_UNKNOWN
    assert mapping.check(None, "order_838").status is NormCheckStatus.NORM_UNKNOWN
    conflict = mapping.check(catalog["B2"], "order_838", audience="preschool")
    assert conflict.status is NormCheckStatus.NORM_MISMATCH and conflict.reason == "DOCUMENT_INSTITUTION_CONFLICT"


def test_weak_mapping_is_not_approved(engine):
    _, mapping, _, catalog = engine
    assert mapping.mappings(catalog["B4"])[0].status is MappingStatus.REVIEW_REQUIRED
    strong = mapping.mappings(catalog["B2"])[0]
    assert strong.status is MappingStatus.APPROVED and strong.item_title == "Мат гимнастический"


def test_products_for_point_include_subsection(engine):
    _, mapping, _, catalog = engine
    found = [product.id for product, _ in mapping.products_for(catalog.values(), "order_1057", "1.5.1")]
    assert found == ["B2", "B3", "B1"]


def test_mappings_respect_audience(engine):
    _, mapping, _, catalog = engine
    assert mapping.mappings(catalog["T1"], audience="preschool") == []
