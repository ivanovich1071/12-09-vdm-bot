"""EPIC 1: доменный слой каталога — контракт товара, репозиторий, сервис, запрос.

Каталог синтетический, реальные данные (`data/kb`) не читаются. Названия разделов
взяты из настоящей выгрузки от 26.08.2026 — именно на них держится разбор кабинетов.
Регрессия бота (Telegram, Web, консультант, продавец, корзина, заказ, 838, 1057)
проверяется в `test_regression_contract.py` и здесь не дублируется.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict

import pytest

from catalog.models import SOURCE_CATALOG_EXPORT, SOURCE_SITE_PAGE, Availability, Product
from catalog.placement import ROOM_NAMES, AgeRange, parse_age, placement_of, room_in
from catalog.query import CatalogQuery, FilterStatus
from catalog.repository import InMemoryCatalogRepository
from catalog.search import CatalogIndex, SearchHit, SearchQuery
from catalog.service import CatalogService
from core.config import Settings
from core.dialog import DialogEngine
from core.profile import _ROOMS as PROFILE_ROOMS
from core.storage import Storage
from ingest import build_kb
from norms import items as norm_items
from orders.service import OrderService
from orders.sinks import JsonlSink

SCHOOL = "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"
PRESCHOOL = "ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"
NEW_BUILDINGS = "ОСНАЩЕНИЕ НОВОСТРОЕК"
CORRECTION = "КОРРЕКЦИОННАЯ СРЕДА"
CABINETS = "Раздел 2. Комплекс оснащения предметных кабинетов"

INFORMATICS = [SCHOOL, CABINETS, "Подраздел 18. Кабинет информатики"]
CHEMISTRY = [SCHOOL, CABINETS, "Подраздел 15. Кабинет химии", "2.15.85. Пробирка"]
PRIMARY = [SCHOOL, CABINETS, "Подраздел 1. Кабинет начальных классов"]
SCHOOL_GYM = [
    SCHOOL,
    "Раздел 1. Комплекс оснащения общешкольных помещений",
    "Подраздел 7. Спортивный комплекс",
    "1.7.11. Мяч баскетбольный",
]
SENSORY_ROOM = [PRESCHOOL, "16. Сенсорная комната"]
BALLS = [PRESCHOOL, "12. Спортивное оборудование и инвентарь", "12.04 Мячи", "12.04.1 Мячи игровые"]
PYRAMIDS = [PRESCHOOL, "03. Развивающие игры", "03.04 Пирамидки, объемные вкладыши, сортировщики"]
GROUP_3_4 = [NEW_BUILDINGS, "1.6 Бассейн", "1.14.5 Групповые помещения для детей 3 - 4 лет"]
SPEECH_THERAPY = [CORRECTION, "04. Игровые пособия и материалы для кабинета логопеда"]

CITATIONS = {
    "order_838": "приказ Минпросвещения России от 28.11.2024 № 838",
    "order_1057": "приказ Минпросвещения России от 25.12.2024 № 1057",
}


def ref(doc_id: str, code: str) -> dict:
    return {
        "doc_id": doc_id,
        "doc_citation": CITATIONS[doc_id],
        "item_code": code,
        "item_title": None,
        "source": "heading",
        "confidence": 0.9,
    }


def raw(sku: str, name: str, paths: list[list[str]], **kw) -> dict:
    return {
        "sku_1c": sku,
        "name": name,
        "url": kw.get("url"),
        "short_url": None,
        "price": kw.get("price", 1000),
        "currency": "RUB",
        "in_stock": kw.get("in_stock", 1),
        "category_paths": paths,
        "description": kw.get("description", ""),
        "kit_contents": kw.get("kit_contents", []),
        "norms": kw.get("norms", []),
        "bitrix_id": kw.get("bitrix_id"),
        "images": kw.get("images", []),
        "attributes": kw.get("attributes", {}),
        "sources": {"catalog": "Pricelist20260826.xlsx"},
        "updated_at": "2026-08-26T10:00:00+00:00",
        **({"is_active": kw["is_active"]} if "is_active" in kw else {}),
    }


def product(sku: str, name: str, paths: list[list[str]], **kw) -> Product:
    return Product.from_dict(raw(sku, name, paths, **kw))


NOTEBOOK = raw(
    "INF1",
    "Ноутбук ученический",
    [INFORMATICS],
    price=45000,
    in_stock=5,
    url="https://vdm.ru/catalog/notebook/",
    description="Ноутбук для учеников.",
    kit_contents=["ноутбук", "зарядное устройство"],
    images=["https://vdm.ru/upload/notebook.jpg"],
    attributes={"Код": "INF1", "Артикул": "NB-15", "Бренд": "Аквариус"},
    norms=[ref("order_838", "2.18.1")],
    bitrix_id=71001,
)


@pytest.fixture
def products() -> list[Product]:
    return [
        Product.from_dict(NOTEBOOK),
        product("INF2", "Интерактивная панель 75 дюймов", [INFORMATICS], price=300000, in_stock=0),
        # Цена по запросу, остаток неизвестен.
        product("INF3", "Набор робототехники", [INFORMATICS], price=None, in_stock=None),
        # Ловушки для поиска по словам: описание говорит «кабинет информатики»,
        # а товар лежит в химии и в детском саду.
        product(
            "CHEM1",
            "Пробирка ПХ-14",
            [CHEMISTRY],
            price=30,
            in_stock=100,
            description="Оборудование для кабинета химии и кабинета информатики.",
        ),
        product(
            "KG1",
            "Световой стол для рисования песком",
            [SENSORY_ROOM],
            price=20000,
            in_stock=2,
            description="Оборудование для кабинета информатики у малышей.",
            attributes={"Возраст": "3+"},
        ),
        product(
            "BALL1",
            "Мяч игровой 20 см",
            [BALLS, SCHOOL_GYM],
            price=500,
            in_stock=10,
            attributes={"Возраст": "3+"},
        ),
        product("KG2", "Пирамидка деревянная", [PYRAMIDS], price=700, in_stock=4),
        product(
            "GRP1",
            "Кукла Маша",
            [GROUP_3_4],
            price=1500,
            norms=[ref("order_1057", "1.14.5.1")],
        ),
        # Логопедия есть только в смешанной ветке, а школьное размещение — начальные классы.
        product("LOG1", "Логопедическое лото", [SPEECH_THERAPY, PRIMARY], price=900, in_stock=3),
        product("OLD", "Мяч снятый с продажи", [BALLS], price=400, in_stock=9, is_active=False),
    ]


@pytest.fixture
def service(products) -> CatalogService:
    return CatalogService(InMemoryCatalogRepository(CatalogIndex(products)))


def skus(result) -> set[str]:
    return {hit.product.sku_1c for hit in result.hits}


# --- Названные в ТЗ ------------------------------------------------------------


def test_product_model():
    item = Product.from_dict(NOTEBOOK)

    assert item.id == item.article == "INF1"
    # Артикул сайта — поставщика, а «Код» карточки совпадает с кодом 1С.
    assert item.supplier_article == "NB-15"
    assert item.characteristics["Код"] == item.article
    assert item.manufacturer == "Аквариус"
    assert item.availability is Availability.AVAILABLE
    assert item.quantity_available == 5
    assert item.image_urls == ["https://vdm.ru/upload/notebook.jpg"]
    assert item.institution_types == {"school"}
    assert item.rooms == {"кабинет информатики"}
    assert item.norm_documents == ["order_838"]
    assert item.norm_points == [("order_838", "2.18.1")]
    assert item.is_active

    commercial = asdict(item.commercial)
    assert commercial["price"] == 45000
    assert commercial["source"]["kind"] == SOURCE_CATALOG_EXPORT
    assert commercial["source"]["origin"] == "Pricelist20260826.xlsx"
    # Коммерческие данные не смешаны с нормативными.
    assert not {"norms", "norm_documents", "norm_points"} & commercial.keys()

    card = item.card
    assert card.sources["description"].kind == SOURCE_CATALOG_EXPORT
    assert card.sources["image_urls"].kind == SOURCE_SITE_PAGE
    assert card.sources["image_urls"].origin == "https://vdm.ru/catalog/notebook/"
    assert "age" not in card.sources


def test_product_model_reads_legacy_records():
    """Записи базы знаний без новых полей читаются как раньше."""
    legacy = {key: value for key, value in NOTEBOOK.items() if key not in {"sources", "images"}}

    item = Product.from_dict(legacy)

    assert item.sources == {}
    assert item.stock_known and item.is_active
    assert item.commercial.source.origin == ""


def test_get_product_by_id(service):
    assert service.get_product("INF1").name == "Ноутбук ученический"
    assert service.get_product("NOPE") is None


def test_get_product_by_article(service):
    assert service.get_by_article(" INF1 ").sku_1c == "INF1"
    # Артикул поставщика не уникален и ключом не является.
    assert service.get_by_article("NB-15") is None
    assert service.get_by_article("") is None

    result = service.search(CatalogQuery(article="INF1"))
    assert [(hit.product.sku_1c, hit.reason) for hit in result.hits] == [("INF1", "article")]


def test_catalog_search(service):
    result = service.search(CatalogQuery(query="мяч"))

    assert "BALL1" in skus(result)
    assert "OLD" not in skus(result)
    assert result.filters == []
    assert result.matched == len(result.hits)


def test_catalog_query_filters(service):
    result = service.search(
        CatalogQuery(
            query="оборудование",
            institution_type="школа",
            institution_name="МБОУ СОШ № 1",
            room="кабинет информатики",
            zone="рабочее место учителя",
            available_only=True,
            price_max=100000,
        )
    )

    assert skus(result) == {"INF1"}
    statuses = {report.name: report.status for report in result.filters}
    assert statuses == {
        "institution_type": FilterStatus.APPLIED,
        "room": FilterStatus.APPLIED,
        "price": FilterStatus.PARTIAL,
        "available_only": FilterStatus.APPLIED,
        "zone": FilterStatus.NOT_APPLIED,
        "institution_name": FilterStatus.NOT_APPLIED,
    }
    assert {report.name for report in result.not_applied} == {"zone", "institution_name"}
    assert "зон" in result.filter("zone").note


def test_available_only(service):
    result = service.get_available(CatalogQuery(room="кабинет информатики"))

    assert skus(result) == {"INF1"}
    report = result.filter("available_only")
    assert (report.excluded, report.unknown, report.status) == (2, 1, FilterStatus.PARTIAL)
    assert "исключены" in report.note


def test_price_filter(service):
    result = service.search(CatalogQuery(room="кабинет информатики", price_max=100000))

    assert skus(result) == {"INF1"}
    report = result.filter("price")
    # Панель дороже, у набора цена по запросу — оба исключены, второй как «нет данных».
    assert (report.excluded, report.unknown) == (2, 1)

    assert skus(service.search(CatalogQuery(query="мяч", price_min=600))) == set()


def test_institution_filter(service):
    assert "BALL1" in skus(service.search(CatalogQuery(query="мяч", institution_type="детский сад")))
    assert skus(service.search(CatalogQuery(query="кукла", institution_type="school"))) == set()

    preschool = service.search(CatalogQuery(institution_type="preschool", limit=100))
    assert skus(preschool) == {"KG1", "BALL1", "KG2", "GRP1"}
    # Лото лежит в смешанной «Коррекционной среде» и в школьной ветке: для сада оно чужое.
    report = preschool.filter("institution_type")
    assert (report.excluded, report.unknown) == (5, 0)

    unknown = service.search(CatalogQuery(query="мяч", institution_type="колледж"))
    assert unknown.hits == []
    assert "не распознан" in unknown.filter("institution_type").note


def test_room_filter(service):
    result = service.search(
        CatalogQuery(
            query="оборудование для кабинета информатики",
            institution_type="школа",
            room="кабинет информатики",
        )
    )

    # Все три товара раздела и ни одного из химии или детского сада, хотя
    # описания ловушек совпадают с запросом лучше.
    assert skus(result) == {"INF1", "INF2", "INF3"}
    assert skus(service.search(CatalogQuery(room="информатика"))) == {"INF1", "INF2", "INF3"}

    unknown = service.search(CatalogQuery(room="столовая"))
    assert unknown.hits == []
    assert "не выделено" in unknown.filter("room").note


def test_catalog_repository(products):
    repository = InMemoryCatalogRepository(CatalogIndex(products))

    assert repository.get_product("INF1") is repository.get_by_article("INF1")
    assert "OLD" not in {item.sku_1c for item in repository.list_active()}
    assert "OLD" in {item.sku_1c for item in repository.index.products}
    hits = repository.search_text("2.18.1", limit=5)
    assert [hit.product.sku_1c for hit in hits] == ["INF1"]


def test_service_depends_only_on_repository_contract(products):
    """Сервису не нужны ни `CatalogIndex`, ни SQLite — только методы репозитория."""

    class ListRepository:
        def __init__(self, items: list[Product]) -> None:
            self.items = {item.sku_1c: item for item in items}

        def get_product(self, product_id: str) -> Product | None:
            return self.items.get(product_id)

        def get_by_article(self, article: str) -> Product | None:
            return self.items.get(article.strip())

        def list_active(self) -> list[Product]:
            return [item for item in self.items.values() if item.is_active]

        def search_text(
            self, text: str, *, limit: int, audience: str | None = None, norm_point: str | None = None
        ) -> list[SearchHit]:
            return [
                SearchHit(item, 1.0, "text")
                for item in self.list_active()
                if text.lower() in item.name.lower()
            ][:limit]

    service = CatalogService(ListRepository(products))

    assert skus(service.search(CatalogQuery(room="кабинет информатики"))) == {"INF1", "INF2", "INF3"}
    assert skus(service.search(CatalogQuery(query="мяч", institution_type="школа"))) == {"BALL1"}


# --- Размещение: учреждение и кабинет проверяются вместе ------------------------


def test_room_and_institution_checked_on_same_placement(service):
    # Логопедия у лото только в смешанной ветке, школьное размещение — начальные классы.
    assert "LOG1" in skus(service.search(CatalogQuery(room="кабинет логопеда")))
    assert "LOG1" not in skus(
        service.search(CatalogQuery(room="кабинет логопеда", institution_type="школа"))
    )

    # Мяч лежит в садовском разделе без кабинета и в школьном спортзале.
    assert "BALL1" in skus(service.search(CatalogQuery(room="спортзал", institution_type="школа")))
    preschool_gym = service.search(CatalogQuery(room="спортзал", institution_type="детский сад"))
    assert "BALL1" not in skus(preschool_gym)
    assert preschool_gym.filter("room").unknown == 1


@pytest.mark.parametrize(
    ("path", "institution", "room", "age"),
    [
        (INFORMATICS, "school", "кабинет информатики", None),
        (SCHOOL_GYM, "school", "спортивный зал", None),
        (CHEMISTRY, "school", "кабинет химии", None),
        (
            [SCHOOL, CABINETS, "Подраздел 20. Кабинет труда (технологии)", "2.20.63. Фрезерно-гравировальный станок"],
            "school",
            "кабинет технологии",
            None,
        ),
        (
            [SCHOOL, CABINETS, "Подраздел 3. Кабинет проектно-исследовательской деятельности для начальных классов"],
            "school",
            "кабинет проектной деятельности",
            None,
        ),
        ([SCHOOL, CABINETS, "Подраздел 7. Игровая для группы продленного дня"], "school", "игровая продлённого дня", None),
        ([SCHOOL, CABINETS, "Подраздел 22. Профильные классы"], "school", None, None),
        # Сюжетная игра и занятие, а не помещения.
        ([PRESCHOOL, "02. Игрушки и сюжетные игры", "02.16 Мастерская"], "preschool", None, None),
        ([PRESCHOOL, "09.Художественно-эстетическое развитие", "09.03 ИЗО"], "preschool", None, None),
        ([PRESCHOOL, "01. Образовательные комплекты", "01.01 ПРЕДШКОЛА 2025", "01.01.10 Физическое развитие"], "preschool", None, None),
        ([PRESCHOOL, "12. Спортивное оборудование и инвентарь", "12.02 Спортивное оборудование для зала"], "preschool", "спортивный зал", None),
        (SENSORY_ROOM, "preschool", "кабинет психолога", None),
        # В выгрузке кабинеты вложены в «1.6 Бассейн»: берётся самый глубокий раздел.
        ([NEW_BUILDINGS, "1.6 Бассейн", "1.13.1 Кабинет дефектолога"], "preschool", "кабинет дефектолога", None),
        ([NEW_BUILDINGS, "1.6 Бассейн"], "preschool", "бассейн", None),
        (GROUP_3_4, "preschool", "групповая комната", AgeRange(3, 4)),
        ([NEW_BUILDINGS, "1.6 Бассейн", "1.14.2 Групповые помещения для детей до 1 года"], "preschool", "групповая комната", AgeRange(0, 1)),
        ([CORRECTION, "05. Игровые пособия и материалы для кабинета психолога"], None, "кабинет психолога", None),
        (["ИННОВАЦИОННЫЕ РЕШЕНИЯ"], None, None, None),
    ],
)
def test_placement_on_real_titles(path, institution, room, age):
    placement = placement_of(path)

    assert (placement.institution, placement.room, placement.age) == (institution, room, age)


def test_profile_rooms_are_never_mistaken_for_other_rooms():
    """Кабинет из профиля разговора узнаётся как он сам или не узнаётся вовсе."""
    for name in ROOM_NAMES:
        assert room_in(name) == name
    for name, _pattern in PROFILE_ROOMS:
        assert room_in(name) in (name, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3–4 года", AgeRange(3, 4)),
        ("от 3 до 5 лет", AgeRange(3, 5)),
        ("до 1 года", AgeRange(0, 1)),
        ("3+", AgeRange(3, None)),
        ("5 лет", AgeRange(5, 5)),
        ("младшая группа", None),
        ("", None),
    ],
)
def test_age_parsing(text, expected):
    assert parse_age(text) == expected


# --- Нереализованные и неполные фильтры сообщают о себе -------------------------


def test_age_filter_is_soft_and_reported(service):
    result = service.search(CatalogQuery(institution_type="preschool", age_group="5–6 лет", limit=100))

    # Кукла из группы 3–4 лет исключена, пирамидка без возраста осталась.
    assert skus(result) == {"KG1", "BALL1", "KG2"}
    report = result.filter("age_group")
    assert (report.status, report.excluded, report.unknown) == (FilterStatus.PARTIAL, 1, 1)
    assert "оставлены" in report.note

    unparsed = service.search(CatalogQuery(institution_type="preschool", age_group="младшая группа", limit=100))
    assert skus(unparsed) == {"KG1", "BALL1", "KG2", "GRP1"}
    assert unparsed.filter("age_group").status is FilterStatus.NOT_APPLIED


def test_manufacturer_filter_is_strict(service):
    result = service.search(CatalogQuery(room="кабинет информатики", manufacturer="аквариус"))

    assert skus(result) == {"INF1"}
    report = result.filter("manufacturer")
    assert (report.status, report.unknown) == (FilterStatus.PARTIAL, 2)
    assert "исключены" in report.note


def test_norm_filter(service):
    assert skus(service.search(CatalogQuery(norm_document="1057"))) == {"GRP1"}
    assert skus(service.search(CatalogQuery(norm_document="приказ 1057", norm_point="1.14"))) == {"GRP1"}
    assert skus(service.search(CatalogQuery(norm_point="2.18.1"))) == {"INF1"}
    # Номер пункта не того документа.
    assert skus(service.search(CatalogQuery(norm_document="order_1057", norm_point="2.18.1"))) == set()

    unknown = service.search(CatalogQuery(norm_document="приказ 999"))
    assert unknown.hits == []
    assert "не распознан" in unknown.filter("norm").note


def test_text_hints_become_reported_filters(service):
    result = service.search(CatalogQuery(query="мяч в наличии до 1000 руб"))

    assert (result.query.query, result.query.available_only, result.query.price_max) == ("мяч", True, 1000)
    assert {report.name for report in result.filters} == {"price", "available_only"}
    assert "BALL1" in skus(result)


# --- Наличие: UNKNOWN не превращается в AVAILABLE ------------------------------


def test_unknown_availability_is_never_available(service):
    item = service.get_product("INF3")

    assert item.availability is Availability.UNKNOWN
    assert item.quantity_available is None
    # Прежние потребители видят ноль и пишут «под заказ», а не «в наличии».
    assert (item.in_stock, item.available) == (0, False)
    assert "INF3" not in skus(service.get_available(CatalogQuery(room="кабинет информатики")))


class _Book:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def rows(self, _sheet: int) -> Iterator[dict[str, str]]:
        return iter(self._rows)


def test_ingest_keeps_unknown_stock():
    rows = [
        dict(build_kb.EXPECTED_HEADERS),
        {"A": "S1", "B": "Мяч", "D": "100", "E": ""},
        {"A": "S2", "B": "Обруч", "D": "200", "E": "0"},
        {"A": "S3", "B": "Кегли", "D": "300", "E": "7"},
    ]
    report = build_kb.Report(source_file="t.xlsx", generated_at="now")

    built = build_kb._read_products(_Book(rows), {}, "now", report)
    build_kb._fill_report(report, built)

    assert {item.sku_1c: item.in_stock for item in built} == {"S1": None, "S2": 0, "S3": 7}
    assert (report.with_stock, report.stock_unknown) == (1, 1)
    availability = {
        item.sku_1c: Product.from_dict(json.loads(json.dumps(asdict(item)))).availability
        for item in built
    }
    assert availability == {
        "S1": Availability.UNKNOWN,
        "S2": Availability.NOT_AVAILABLE,
        "S3": Availability.AVAILABLE,
    }


# --- Представление и подключение ----------------------------------------------


def test_presentation_separates_sources(service, products):
    view = service.present(service.get_product("INF1"), audience="школа").to_dict()

    assert view.keys() == {"id", "article", "commercial", "card", "placement", "norms"}
    assert view["commercial"]["source"]["kind"] == SOURCE_CATALOG_EXPORT
    assert view["card"]["sources"]["characteristics"]["kind"] == SOURCE_SITE_PAGE
    assert view["placement"]["rooms"] == ["кабинет информатики"]
    assert view["placement"]["source"]["kind"] == "catalog_tree"
    assert [norm["item_code"] for norm in view["norms"]] == ["2.18.1"]
    json.dumps(view, ensure_ascii=False)

    # Школьный пункт не называется детскому саду.
    assert service.present(service.get_product("INF1"), audience="детский сад").norms == []


def test_engine_exposes_catalog_service(products, tmp_path, monkeypatch):
    monkeypatch.setattr(norm_items, "load", lambda *_a, **_kw: {})
    storage = Storage(tmp_path / "t.sqlite3")
    index = CatalogIndex(products)
    engine = DialogEngine(
        index,
        storage,
        OrderService(storage, JsonlSink(tmp_path / "orders.jsonl")),
        Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl")),
    )

    assert engine.catalog is engine.catalog
    assert engine.catalog.get_by_article("INF1") is engine.index.get("INF1")
    # Прежний поиск работает как раньше и о новом слое не знает.
    legacy = engine.index.search(SearchQuery(text="мяч"))
    assert "BALL1" in {hit.product.sku_1c for hit in legacy}
