"""Доменные типы каталога.

Общие для поиска, агента, адаптеров и заказа: ядро и адаптеры обмениваются именно
этими объектами, а не сырыми строками выгрузки.

Контракт товара v2 (docs/DECISIONS.md, D8) расширяет `Product`, а не заводит
вторую модель: новые поля идут в конец со значениями по умолчанию, остальное —
свойства поверх уже собранных данных. Формат `products.jsonl` не меняется.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import Any

from catalog.placement import AgeRange, Placement, parse_age, placement_of

# Кто поставляет данные сейчас. Владелец коммерческих полей по ТЗ — 1С, карточки —
# сайт. Прямой интеграции с 1С пока нет: код 1С, цена и остаток приходят той же
# выгрузкой каталога, что и описание. С интеграцией сменится поставщик, а не поля.
SOURCE_CATALOG_EXPORT = "catalog_export"
SOURCE_SITE_PAGE = "site_page"
SOURCE_CATALOG_TREE = "catalog_tree"


class Availability(StrEnum):
    """Наличие по данным каталога. `UNKNOWN` никогда не читается как `AVAILABLE`."""

    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SourceRef:
    kind: str
    # Файл выгрузки или страница товара.
    origin: str
    as_of: str = ""


_ORDER_INSTITUTION = {"order_1057": "preschool", "order_838": "school"}


def _institution_from_norms(norms: tuple[NormRef, ...]) -> str | None:
    """Документ привязки, по которому берём учреждение, — когда на сайте оно не названо."""
    docs = {ref.doc_id for ref in norms if ref.item_code} & _ORDER_INSTITUTION.keys()
    return docs.pop() if len(docs) == 1 else None


# Помещение по разделу приказа 1057, когда на сайте товар лежит вне именного раздела
# (план 05-10, шаг 2.10). Проверено на справочнике: заголовки дают эти помещения.
_NORM_ROOMS = (
    ("1.13.3", "кабинет логопеда"),
    ("1.13.2", "кабинет психолога"),
    ("1.13.1", "кабинет дефектолога"),
    ("1.13.4", "кабинет дополнительного образования"),
    ("1.5", "спортивный зал"),
    ("1.6", "бассейн"),
    ("1.2", "музыкальный зал"),
)
# Возраст групповых помещений: 1.14.2 — до года, дальше по году (1.14.4 — 2–3, 1.14.5 — 3–4).
_GROUP_AGES = {
    "1.14.2": (0, 1),
    "1.14.3": (1, 2),
    "1.14.4": (2, 3),
    "1.14.5": (3, 4),
    "1.14.6": (4, 5),
    "1.14.7": (5, 6),
    "1.14.8": (6, 7),
}


def _norm_room_age(doc_id: str, code: str) -> tuple[str | None, AgeRange | None]:
    """Помещение и возраст по коду пункта приказа."""
    if doc_id != "order_1057":
        return None, None
    for prefix, room in _NORM_ROOMS:
        if code == prefix or code.startswith(f"{prefix}."):
            return room, None
    if code == "1.14" or code.startswith("1.14."):
        for prefix, years in _GROUP_AGES.items():
            if code == prefix or code.startswith(f"{prefix}."):
                return "групповая комната", AgeRange(*years)
        return "групповая комната", None
    return None, None


def _norm_placements(norms: tuple[NormRef, ...]) -> list[Placement]:
    """Размещения «по приказу» из привязок реестра: учреждение, помещение, возраст."""
    doc = _institution_from_norms(norms)
    if doc is None:
        return []
    institution = _ORDER_INSTITUTION[doc]
    result: list[Placement] = []
    seen: set[tuple[str | None, AgeRange | None]] = set()
    for ref in norms:
        if not ref.item_code or ref.doc_id != doc:
            continue
        room, age = _norm_room_age(ref.doc_id, ref.item_code)
        if (room, age) in seen:
            continue
        seen.add((room, age))
        result.append(
            Placement(
                path=(f"ПРИКАЗ {_ORDER_INSTITUTION[doc]}", f"пункт {ref.item_code}"),
                institution=institution,
                room=room,
                age=age,
            )
        )
    return result


@dataclass(frozen=True)
class NormRef:
    """Нормативное основание: документ и, если известен, пункт перечня."""

    doc_id: str
    doc_citation: str
    item_code: str | None
    item_title: str | None
    source: str
    confidence: float

    @property
    def citation(self) -> str:
        if self.item_code:
            return f"позиция {self.item_code} — {self.doc_citation}"
        return self.doc_citation

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> NormRef:
        return cls(
            doc_id=raw["doc_id"],
            doc_citation=raw["doc_citation"],
            item_code=raw.get("item_code"),
            item_title=raw.get("item_title"),
            source=raw.get("source", ""),
            confidence=float(raw.get("confidence", 0.0)),
        )


@dataclass(frozen=True)
class CommercialData:
    """Коммерческие поля. Нормативных полей здесь нет по построению."""

    article: str
    name: str
    price: int | None
    currency: str
    availability: Availability
    quantity_available: int | None
    source: SourceRef


@dataclass(frozen=True)
class CardData:
    """Карточка товара. Расширяется независимо от коммерческих данных."""

    description: str
    kit_contents: list[str]
    url: str | None
    short_url: str | None
    image_urls: list[str]
    characteristics: dict[str, str]
    supplier_article: str | None
    manufacturer: str | None
    age: AgeRange | None
    bitrix_id: int | None
    # Источник каждого заполненного поля: описание пришло выгрузкой, фото и
    # характеристики — со страницы товара.
    sources: dict[str, SourceRef]


def _relevance(ref: NormRef, query: str) -> tuple[int, int, float]:
    """Насколько пункт отвечает на заданный вопрос.

    Порядок ключей: сначала есть ли вообще номер пункта, потом совпадение слов
    названия пункта со словами запроса, и только затем надёжность привязки.
    """
    overlap = 0
    if query and ref.item_title:
        from catalog.text import stems

        words = set(stems(query))
        overlap = len(words & set(stems(ref.item_title)))
    return (ref.item_code is not None, overlap, ref.confidence)


@dataclass(frozen=True)
class Product:
    sku_1c: str
    name: str
    url: str | None
    short_url: str | None
    price: int | None
    currency: str
    in_stock: int
    category_paths: list[list[str]]
    description: str
    kit_contents: list[str]
    norms: list[NormRef]
    bitrix_id: int | None
    images: list[str] = field(default_factory=list)
    # Характеристики со страницы товара: страна, сертификат. В выгрузке 1С их нет,
    # они добираются тем же проходом, что и фотографии.
    attributes: dict[str, str] = field(default_factory=dict)
    updated_at: str = ""
    # Откуда пришла запись: {"catalog": "Pricelist20260826.xlsx"}. Сборка базы
    # знаний пишет это поле давно, модель чтения его теряла.
    sources: dict[str, str] = field(default_factory=dict)
    # Остаток известен. Пустая ячейка выгрузки — не ноль: «нет данных» не должно
    # читаться ни как «нет в наличии», ни тем более как «есть». `in_stock` при
    # этом остаётся нулём, чтобы прежние потребители показывали «под заказ».
    stock_known: bool = True
    is_active: bool = True

    @property
    def roots(self) -> list[str]:
        return [path[0] for path in self.category_paths if path]

    @property
    def available(self) -> bool:
        return self.in_stock > 0

    @property
    def norm_codes(self) -> list[str]:
        return [ref.item_code for ref in self.norms if ref.item_code]

    @property
    def audiences(self) -> set[str]:
        """Кому товар предназначен — по веткам каталога, в которых он лежит.

        Товар часто лежит сразу в нескольких: садовский комплект попадает и в
        школьный раздел. Поэтому это множество, а не одно значение.
        """
        return {placement.institution for placement in self.placements if placement.institution}

    def norms_for(self, audience: str | None = None) -> list[NormRef]:
        """Основания, уместные этому собеседнику.

        Сайт заказчика кладёт один товар в несколько веток сразу: садовские
        карточки по лексическим темам стоят ещё и в школьном разделе «по приказу
        № 838». Раньше это приводило к тому, что на вопрос про детский сад бот
        цитировал школьный приказ.

        Чужой перечень не подменяется своим и не добирается «хоть какой-нибудь»:
        школьный пункт не является основанием для детского сада, и честнее не
        назвать основание вовсе, чем назвать чужое.
        """
        from norms import documents as docs

        allowed = docs.for_audience(audience)
        return [ref for ref in self.norms if ref.doc_id in allowed]

    def norm_for(self, audience: str | None = None, query: str = "") -> NormRef | None:
        """Одно основание для строки выдачи.

        У товара их бывает несколько — «КМО 2024 Классификация» закрывает и
        пункт 2.4.35, и 4.4.17. Показываем тот, что ближе к запросу: спросили про
        логопеда — назовём логопедический пункт, а не пункт про аутизм.
        """
        candidates = self.norms_for(audience)
        if not candidates:
            return None
        return max(candidates, key=lambda ref: _relevance(ref, query))

    def best_norm(self) -> NormRef | None:
        """Основание без учёта собеседника. Остаётся для мест, где его негде взять."""
        return self.norm_for(None)

    # --- Контракт товара v2 ---------------------------------------------------

    @property
    def id(self) -> str:
        return self.sku_1c

    @property
    def article(self) -> str:
        """Артикул — код 1С (D8, решение A).

        «Артикул» на сайте ни разу не совпадает с кодом 1С: это артикул
        поставщика, см. `supplier_article`. А атрибут карточки «Код» с кодом 1С
        совпадает — стабильный ключ один и тот же у выгрузки и у сайта.
        """
        return self.sku_1c

    @property
    def supplier_article(self) -> str | None:
        return self.attributes.get("Артикул") or None

    @property
    def manufacturer(self) -> str | None:
        """Бренд из карточки. Заполнен у немногих товаров — фильтр это сообщает."""
        return self.attributes.get("Бренд") or None

    @property
    def quantity_available(self) -> int | None:
        return self.in_stock if self.stock_known else None

    @property
    def availability(self) -> Availability:
        if not self.stock_known:
            return Availability.UNKNOWN
        return Availability.AVAILABLE if self.in_stock > 0 else Availability.NOT_AVAILABLE

    @property
    def image_urls(self) -> list[str]:
        return self.images

    @property
    def characteristics(self) -> dict[str, str]:
        return self.attributes

    @cached_property
    def placements(self) -> list[Placement]:
        found = [placement_of(path) for path in self.category_paths if path]
        # К9.2/К9.3: дерево сайта не даёт учреждение или помещение, а реестр
        # привязал товар к пункту приказа — размещение строится по пункту (1057 —
        # сад, 838 — школа). 61 позиция «Коррекционной среды» без этого выпадала
        # при любом запросе, 57 товаров с пунктом 1.13.3.x не доходили до кабинета
        # логопеда, а у групповых помещений настоящие товары вытеснялись заготовками.
        if not any(p.institution for p in found) or not any(p.room for p in found):
            extra = _norm_placements(self.norms)
            if not any(p.institution for p in found):
                found += [p for p in extra if p.institution]
            if not any(p.room for p in found):
                found += [p for p in extra if p.room or p.age]
        return found

    @property
    def institution_types(self) -> set[str]:
        return self.audiences

    @property
    def rooms(self) -> set[str]:
        return {placement.room for placement in self.placements if placement.room}

    @property
    def card_age(self) -> AgeRange | None:
        return parse_age(self.attributes.get("Возраст"))

    @property
    def age_ranges(self) -> list[AgeRange]:
        ranges = [placement.age for placement in self.placements if placement.age]
        if self.card_age:
            ranges.append(self.card_age)
        return ranges

    @property
    def norm_documents(self) -> list[str]:
        return sorted({ref.doc_id for ref in self.norms})

    @property
    def norm_points(self) -> list[tuple[str, str]]:
        return [(ref.doc_id, ref.item_code) for ref in self.norms if ref.item_code]

    @property
    def commercial(self) -> CommercialData:
        return CommercialData(
            article=self.article,
            name=self.name,
            price=self.price,
            currency=self.currency,
            availability=self.availability,
            quantity_available=self.quantity_available,
            source=self._export_source(),
        )

    @property
    def card(self) -> CardData:
        export = self._export_source()
        page = SourceRef(SOURCE_SITE_PAGE, self.url or "")
        values: dict[str, tuple[object, SourceRef]] = {
            "description": (self.description, export),
            "kit_contents": (self.kit_contents, export),
            "url": (self.url, export),
            "short_url": (self.short_url, export),
            "image_urls": (self.images, page),
            "characteristics": (self.attributes, page),
            "supplier_article": (self.supplier_article, page),
            "manufacturer": (self.manufacturer, page),
            "age": (self.card_age, page),
        }
        return CardData(
            description=self.description,
            kit_contents=self.kit_contents,
            url=self.url,
            short_url=self.short_url,
            image_urls=self.images,
            characteristics=self.attributes,
            supplier_article=self.supplier_article,
            manufacturer=self.manufacturer,
            age=self.card_age,
            bitrix_id=self.bitrix_id,
            sources={name: source for name, (value, source) in values.items() if value},
        )

    @property
    def placement_source(self) -> SourceRef:
        return SourceRef(SOURCE_CATALOG_TREE, self.sources.get("catalog", ""), self.updated_at)

    def _export_source(self) -> SourceRef:
        return SourceRef(SOURCE_CATALOG_EXPORT, self.sources.get("catalog", ""), self.updated_at)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Product:
        stock = raw.get("in_stock")
        return cls(
            sku_1c=raw["sku_1c"],
            name=raw["name"],
            url=raw.get("url"),
            short_url=raw.get("short_url"),
            price=raw.get("price"),
            currency=raw.get("currency", "RUB"),
            in_stock=stock if stock is not None else 0,
            category_paths=raw.get("category_paths", []),
            description=raw.get("description", ""),
            kit_contents=raw.get("kit_contents", []),
            norms=[NormRef.from_dict(n) for n in raw.get("norms", [])],
            bitrix_id=raw.get("bitrix_id"),
            images=raw.get("images", []),
            attributes=raw.get("attributes", {}),
            updated_at=raw.get("updated_at", ""),
            sources=raw.get("sources", {}),
            stock_known=stock is not None,
            is_active=raw.get("is_active", True),
        )
