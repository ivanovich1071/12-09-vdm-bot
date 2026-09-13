"""Синтетический каталог и нормативная база для тестов ядра (NEXT-1…3).

Названия разделов — из настоящей выгрузки 26.08.2026: на них держится разбор
учреждения, кабинета и возраста. Реальные данные (`data/kb`) не читаются.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from catalog.models import Product
from catalog.runtime import CatalogRuntime, CatalogRuntimeState
from catalog.search import CatalogIndex
from core.database import CoreDatabase
from norms import documents as docs
from norms.items import NormItem
from norms.repository import FileNormRepository
from procurement.repository import SqliteProcurementRepository
from procurement.service import ProcurementService

SCHOOL = "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"
PRESCHOOL = "ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"
NEW_BUILDINGS = "ОСНАЩЕНИЕ НОВОСТРОЕК"
CABINETS = "Раздел 2. Комплекс оснащения предметных кабинетов"

INFORMATICS = [SCHOOL, CABINETS, "Подраздел 18. Кабинет информатики"]
CHEMISTRY = [SCHOOL, CABINETS, "Подраздел 15. Кабинет химии", "2.15.85. Пробирка"]
SCHOOL_GYM = [
    SCHOOL,
    "Раздел 1. Комплекс оснащения общешкольных помещений",
    "Подраздел 7. Спортивный комплекс",
]
CANTEEN = [SCHOOL, "Раздел 1. Комплекс оснащения общешкольных помещений", "Подраздел 5. Столовая"]
SPORT = [NEW_BUILDINGS, "1.5 Спортивный зал", "1.5.1 Спортивное оборудование для зала"]
BALLS = [PRESCHOOL, "12. Спортивное оборудование и инвентарь", "12.04 Мячи"]
GROUP_34 = [NEW_BUILDINGS, "1.14 Групповые помещения", "1.14.5 Групповые помещения для детей 3 - 4 лет"]
GROUP_23 = [NEW_BUILDINGS, "1.14 Групповые помещения", "1.14.4 Групповые помещения для детей 2 - 3 лет"]
GAMES = [PRESCHOOL, "03. Развивающие игры"]

VERSION = "2026-09-13-001"


def ref(doc_id: str, code: str | None, source: str = "heading", confidence: float = 0.9) -> dict:
    return {
        "doc_id": doc_id,
        "doc_citation": docs.get(doc_id).citation,
        "item_code": code,
        "item_title": None,
        "source": source,
        "confidence": confidence,
    }


def raw(
    sku: str,
    name: str,
    paths: list[list[str]],
    *,
    price: int | None = 1000,
    stock: int | None = 1,
    norms: tuple = (),
    attributes: dict | None = None,
) -> dict:
    return {
        "sku_1c": sku,
        "name": name,
        "url": f"https://vdm.ru/catalog/{sku}/",
        "short_url": None,
        "price": price,
        "currency": "RUB",
        "in_stock": stock,
        "category_paths": paths,
        "description": f"Описание: {name}",
        "kit_contents": [],
        "norms": list(norms),
        "bitrix_id": None,
        "attributes": attributes or {},
        "sources": {"catalog": "test.xlsx"},
    }


def product_records() -> list[dict]:
    registry = "registry"
    return [
        raw("I1", "Ноутбук ученический", [INFORMATICS], price=50000, stock=3, norms=(ref("order_838", "2.18.5"),)),
        raw("I2", "Интерактивная панель", [INFORMATICS], price=250000, stock=0, norms=(ref("order_838", "2.18.7"),)),
        raw("I3", "Кресло компьютерное", [INFORMATICS], price=9000, stock=10, norms=(ref("order_838", "2.18.1"),)),
        raw("I4", "Принтер 3D учебный", [INFORMATICS], price=None, stock=1),
        raw("C1", "Пробирка лабораторная", [CHEMISTRY], price=50, stock=100, norms=(ref("order_838", "2.15.85"),)),
        raw("SB1", "Мяч баскетбольный школьный", [[*SCHOOL_GYM, "1.7.11. Мяч баскетбольный"]], price=1015, stock=0, norms=(ref("order_838", "1.7.11"),)),
        raw("SB2", "Мяч волейбольный", [[*SCHOOL_GYM, "1.7.13. Мяч волейбольный"]], price=1200, stock=5),
        raw("T1", "Стол для столовой", [[*CANTEEN, "1.5.1. Стол для столовой"]], price=15000, stock=2, norms=(ref("order_838", "1.5.1"),)),
        raw("B1", "Мяч баскетбольный № 3", [BALLS], price=908, stock=4, norms=(ref("order_1057", "1.5.1.33", registry, 0.98),)),
        raw("B2", "Мат детский", [SPORT], price=8164, stock=2, norms=(ref("order_1057", "1.5.1.7", registry, 0.98),)),
        raw("B3", "Доска ребристая", [SPORT], price=12748, stock=0, norms=(ref("order_1057", "1.5.1.13", registry, 0.98),)),
        raw("B4", "Скамейка гимнастическая", [SPORT], price=30000, stock=1, norms=(ref("order_1057", None, "mention", 0.6),)),
        raw("G34", "Кубики для группы", [GROUP_34], price=1500, stock=5, norms=(ref("order_1057", "1.14.5.1", registry, 0.98),)),
        raw("G23", "Каталка для прогулки", [GROUP_23], price=7714, stock=0, norms=(ref("order_1057", "1.14.4.2.2", registry, 0.98),)),
        raw("Z1", "Ковёр развивающий", [GAMES], price=4200, stock=1, norms=(ref("order_1057", "1.9.9", registry, 0.98),)),
    ]


def products(**changes: dict) -> list[Product]:
    """Каталог; `changes` — поля отдельных товаров: `B2={"price": 9000}`."""
    records = []
    for record in product_records():
        record.update(changes.get(record["sku_1c"], {}))
        records.append(record)
    return [Product.from_dict(record) for record in records]


def item(doc_id: str, code: str, title: str, section: str | None = None, unit=None, quantity=None) -> NormItem:
    return NormItem(doc_id=doc_id, code=code, title=title, section=section, unit=unit, quantity=quantity)


def norm_items() -> dict[str, dict[str, NormItem]]:
    found = [
        item("order_838", "1.5.1", "Стол для столовой", "Столовая"),
        item("order_838", "1.7.11", "Мяч баскетбольный", "Спортивный комплекс"),
        item("order_838", "2.15.1", "Весы электронные", "Кабинет химии"),
        item("order_838", "2.15.85", "Пробирка", "Кабинет химии"),
        item("order_838", "2.18.1", "Кресло компьютерное", "Кабинет информатики"),
        item("order_838", "2.18.5", "Ноутбук", "Кабинет информатики"),
        item("order_838", "2.18.7", "Интерактивная панель", "Кабинет информатики"),
        item("order_1057", "1.5.1", "Спортивное оборудование и инвентарь"),
        item("order_1057", "1.5.1.7", "Мат гимнастический", unit="Шт.", quantity="1"),
        item("order_1057", "1.5.1.13", "Доска с ребристой поверхностью", unit="Шт.", quantity="2"),
        item("order_1057", "1.5.1.33", "Мяч для игр", unit="Шт.", quantity="4"),
        item("order_1057", "1.14.5.1", "Кубики", unit="Шт.", quantity="По количест ву детей в группе"),
        item("order_1057", "1.14.4.2.2", "Каталки", unit="Шт.", quantity="1 Шт. на каждую группу"),
    ]
    result: dict[str, dict[str, NormItem]] = {}
    for entry in found:
        result.setdefault(entry.doc_id, {})[entry.code] = entry
    return result


def state(version: str = VERSION, **changes: dict) -> CatalogRuntimeState:
    return CatalogRuntimeState.from_index(
        CatalogIndex(products(**changes)), version=version, sha256=version.replace("-", "").ljust(64, "0")
    )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def procurement_service(tmp_path: Path, runtime: CatalogRuntime | None = None, **kwargs) -> ProcurementService:
    runtime = runtime or CatalogRuntime(state())
    return ProcurementService(
        SqliteProcurementRepository(CoreDatabase(tmp_path / "core.sqlite3")),
        runtime,
        FileNormRepository(norm_items()),
        clock=kwargs.pop("clock", Clock()),
        **kwargs,
    )


def with_price(record: Product, price: int) -> Product:
    return replace(record, price=price)
