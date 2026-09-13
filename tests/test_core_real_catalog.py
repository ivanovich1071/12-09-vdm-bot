"""NEXT-1…2 на реальном каталоге заказчика (5 936 товаров) — только чтение `data/kb`.

Каталог и справочник пунктов в git не хранятся, поэтому без `data/kb` тесты
пропускаются. Товары для заказа выбираются из каталога по свойствам (есть цена,
в наличии, уникальное название), а не по зашитым кодам: следующая выгрузка тест не
ломает.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from catalog.matcher import MatchStatus, canonical_name
from catalog.models import Availability
from catalog.runtime import CatalogRuntime
from core.database import CoreDatabase
from norms.mapping import NormCheckStatus
from norms.repository import FileNormRepository
from order_import.evaluation import EvaluationStatus, PriceStatus
from order_import.models import OrderContext
from order_import.repository import SqliteOrderRepository
from order_import.service import OrderCoreService
from pdf_fixture import make_pdf
from preorder.models import PreorderStatus
from preorder.repository import SqlitePreorderRepository
from preorder.service import PreorderService
from procurement.models import QuantitySource, SelectionStatus
from procurement.repository import SqliteProcurementRepository
from procurement.service import ProcurementService
from test_order_core import HEADER, docx, xlsx

ROOT = Path(__file__).parents[1]
KB = ROOT / "data" / "kb" / "products.jsonl"
ITEMS = ROOT / "data" / "kb" / "norm_items.json"
OWNER = "real-check"

pytestmark = pytest.mark.skipif(not KB.exists(), reason="реального каталога data/kb нет")


@pytest.fixture(scope="module")
def runtime():
    return CatalogRuntime.open(KB)


@pytest.fixture(scope="module")
def norms():
    return FileNormRepository.from_file(ITEMS)


@pytest.fixture
def core(tmp_path, runtime, norms):
    db = CoreDatabase(tmp_path / "core.sqlite3")
    procurement = ProcurementService(SqliteProcurementRepository(db), runtime, norms)
    orders = OrderCoreService(SqliteOrderRepository(db), runtime, norms, tmp_path / "uploads")
    preorders = PreorderService(
        SqlitePreorderRepository(db), runtime, procurement, orders, lambda owner: None, notifier=None  # type: ignore[arg-type]
    )
    return procurement, orders, preorders


def select(procurement, text, **fields):
    task = procurement.create_task(OWNER, "real", text=text, fields=fields or None)
    return task, procurement.select(task.id, OWNER)


# --- Procurement Core -----------------------------------------------------------------


def test_school_informatics(core, runtime):
    procurement, _, _ = core
    task, first = select(procurement, "Школа, кабинет информатики")
    assert first.status is SelectionStatus.FOUND and len(first.items) == 3
    products = [runtime.state.index.get(item.product_id) for item in first.items]
    assert all("кабинет информатики" in product.rooms and "school" in product.audiences for product in products)
    second = procurement.select(task.id, OWNER)
    assert not {i.product_id for i in first.items} & {i.product_id for i in second.items}


def test_preschool_age_group(core, runtime):
    procurement, _, _ = core
    _, result = select(procurement, "Детский сад, групповая комната для детей 3-4 лет")
    assert result.status is SelectionStatus.FOUND
    first = runtime.state.index.get(result.items[0].product_id)
    assert any(p.age and (p.age.min_years, p.age.max_years) == (3, 4) for p in first.placements)


def test_norm_1057_point(core):
    procurement, _, _ = core
    _, result = select(procurement, "Детский сад, по приказу 1057 пункт 1.5.1")
    assert result.status is SelectionStatus.FOUND and result.norm.document == "order_1057"
    assert all(item.norm_status is NormCheckStatus.NORM_OK for item in result.items)
    assert any(item.quantity_source is QuantitySource.NORM for item in result.items)


def test_without_norm(core):
    procurement, _, _ = core
    _, result = select(procurement, "Нужны мячи для спортзала в школе, без норматива")
    assert result.status is SelectionStatus.FOUND and str(result.norm.status) == "NOT_REQUESTED"


def test_budget(core):
    procurement, _, _ = core
    _, result = select(procurement, "Детский сад, спортивный зал, бюджет до 15 тысяч")
    assert result.items and all(item.price is not None and item.price <= 15000 for item in result.items)


# --- Order Core ------------------------------------------------------------------------------


def _unique_named(runtime, predicate, skip=()):
    counts = Counter(canonical_name(p.name) for p in runtime.state.index.products)
    for product in runtime.state.index.products:
        if product.id in skip or not product.is_active or counts[canonical_name(product.name)] != 1:
            continue
        if predicate(product):
            return product
    raise AssertionError("в каталоге нет подходящего товара")


def _typo(name: str) -> str:
    words = name.split()
    longest = max(range(len(words)), key=lambda i: len(words[i]))
    word = words[longest]
    words[longest] = word[: len(word) // 2] + word[len(word) // 2 + 1 :]
    return " ".join(words)


def _order_rows(runtime):
    available = _unique_named(runtime, lambda p: p.price and p.availability is Availability.AVAILABLE)
    by_name = _unique_named(runtime, lambda p: p.price and len(p.name) > 20, skip={available.id})
    changed = _unique_named(runtime, lambda p: p.price and p.price > 1000, skip={available.id, by_name.id})
    typo = _unique_named(
        runtime, lambda p: max(len(w) for w in p.name.split()) >= 10, skip={available.id, by_name.id, changed.id}
    )
    rows = [
        HEADER,
        ["1", available.id, available.name, "1", str(available.price)],
        ["2", "", by_name.name, "2", str(by_name.price)],
        ["3", changed.id, changed.name, "1", str(round(changed.price * 0.9))],
        ["4", "", _typo(typo.name), "1", ""],
        ["5", "", "Телескоп межпланетный с варп-двигателем", "1", "1"],
        ["6", available.id, available.name, "", str(available.price)],
    ]
    return rows, (available, by_name, changed, typo)


def _check(evaluation, products):
    available, by_name, changed, typo = products
    items = {item.line_no: item for item in evaluation.items}
    assert (items[1].match_status, items[1].product_id, items[1].price_status) == (MatchStatus.MATCHED_EXACT, available.id, PriceStatus.PRICE_OK)
    assert items[1].availability is Availability.AVAILABLE
    assert (items[2].match_status, items[2].product_id) == (MatchStatus.MATCHED_HIGH, by_name.id)
    assert items[3].price_status is PriceStatus.PRICE_CHANGED and items[3].current_price == changed.price
    assert items[4].match_status in (MatchStatus.MATCHED_REVIEW, MatchStatus.AMBIGUOUS, MatchStatus.NOT_FOUND)
    assert items[4].status is EvaluationStatus.REVIEW_REQUIRED
    assert items[5].match_status is MatchStatus.NOT_FOUND
    assert "QUANTITY_UNKNOWN" in [n.code for n in items[6].errors]
    assert evaluation.status is EvaluationStatus.REVIEW_REQUIRED


def test_order_pipeline_excel_word_pdf(core, runtime):
    _, orders, _ = core
    rows, products = _order_rows(runtime)
    files = {
        "order.xlsx": xlsx(rows),
        "order.docx": docx(rows),
        "order.pdf": make_pdf(["    ".join(cell or "-" for cell in row) for row in rows]),
    }
    for name, content in files.items():
        order = orders.upload(OWNER, "real", name, content)
        # В PDF пустых ячеек нет: там прочерк, и он читается как пустое значение.
        _check(orders.evaluate(order.id, OWNER), products)


def test_specification_to_preorder(core):
    procurement, orders, preorders = core
    task, result = select(procurement, "Детский сад, по приказу 1057 пункт 1.5.1")
    procurement.choose(task.id, OWNER, [item.product_id for item in result.items])
    spec = procurement.build_specification(task.id, OWNER)
    document = procurement.export_specification(spec.id, OWNER, "xlsx")
    order = orders.upload(OWNER, "real", document.filename, document.content, OrderContext("preschool", "order_1057"))
    evaluation = orders.evaluate(order.id, OWNER)
    assert all(i.match_status is MatchStatus.MATCHED_EXACT and i.norm_status is NormCheckStatus.NORM_OK for i in evaluation.items)
    assert evaluation.summary["current_amount"] == spec.totals.amount
    preorder = preorders.create_from_order(order.id, OWNER, "real")
    assert preorder.status is PreorderStatus.READY_FOR_MANAGER and preorder.totals.amount == spec.totals.amount
