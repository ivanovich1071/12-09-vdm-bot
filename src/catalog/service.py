"""Сервис каталога: единая точка для прикладного кода.

Подбор разбит на стадии, у каждой свой метод:

- `retrieve` — кандидаты: существующий поиск по словам и номеру пункта плюс все
  товары заданного кабинета или раздела;
- `filter` — жёсткие фильтры, по каждому отчёт: сколько исключено, у скольких
  не хватило данных;
- `rank` — сначала совпавшее по словам запроса, затем остальное из раздела;
- `present` — данные для модели и карточки, где коммерческие поля, карточка,
  размещение и нормативка разделены и у каждой части свой источник.

Алгоритм поиска не меняется: retrieval — это `CatalogIndex` через репозиторий.
Бот и агент переходят на сервис в следующих EPIC.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any

from catalog.models import Availability, CardData, CommercialData, NormRef, Product, SourceRef
from catalog.placement import (
    AgeRange,
    Placement,
    institution_code,
    matches_category,
    parse_age,
    room_in,
)
from catalog.query import CatalogQuery, CatalogResult, FilterReport, FilterStatus
from catalog.repository import CatalogRepository
from catalog.search import SearchHit
from norms import documents as norm_docs
from norms.extract import codes_in_query, document_ids_in_text

# Сколько кандидатов берём у поиска по словам. При выдаче из 50 редкий раздел до
# фильтров не доживал: на «кабинет информатики» из 12 товаров раздела в топ-10
# попадал один.
CANDIDATE_LIMIT = 300

# Причина попадания в кандидаты. Первые — по смыслу запроса и уже упорядочены
# поиском; остальные просто лежат в заданном разделе или в каталоге.
_BY_RELEVANCE = frozenset({"article", "norm_code", "text", "trigram"})
PLACEMENT = "placement"
LISTING = "catalog"


class _Verdict(Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


@dataclass
class _Candidate:
    hit: SearchHit
    # Размещения, которые ещё подходят. Учреждение, кабинет и раздел проверяются
    # на одном и том же размещении: садовская сенсорная комната не делает товар
    # школьным кабинетом психолога только потому, что он есть и в школьной ветке.
    placements: list[Placement]


@dataclass(frozen=True)
class _Step:
    name: str
    value: object
    check: Callable[[_Candidate], _Verdict]
    # Когда у товара нет данных для фильтра: оставить (мягкий) или исключить.
    keep_unknown: bool = False
    note: str = ""


@dataclass(frozen=True)
class PlacementView:
    institution_types: list[str]
    rooms: list[str]
    paths: list[list[str]]
    source: SourceRef


@dataclass(frozen=True)
class ProductView:
    """Товар для модели и карточки. Коммерческое, карточка и нормативка — порознь."""

    id: str
    article: str
    commercial: CommercialData
    card: CardData
    placement: PlacementView
    # Только основания, уместные собеседнику: 838 — школе, 1057 — саду.
    norms: list[NormRef]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CatalogService:
    def __init__(self, repository: CatalogRepository) -> None:
        self.repository = repository

    # --- Точечный доступ ------------------------------------------------------

    def get_product(self, product_id: str) -> Product | None:
        return self.repository.get_product(product_id)

    def get_by_article(self, article: str) -> Product | None:
        return self.repository.get_by_article(article)

    def list_active(self) -> list[Product]:
        return self.repository.list_active()

    # --- Подбор ---------------------------------------------------------------

    def search(self, query: CatalogQuery) -> CatalogResult:
        query = query.with_text_hints()
        candidates = self.retrieve(query)
        kept, filters = self.filter(candidates, query)
        ranked = self.rank(kept)
        return CatalogResult(
            query=query,
            hits=ranked[: query.limit],
            filters=filters,
            candidates=len(candidates),
            matched=len(ranked),
        )

    def get_available(self, query: CatalogQuery | None = None) -> CatalogResult:
        return self.search(replace(query or CatalogQuery(), available_only=True))

    def retrieve(self, query: CatalogQuery) -> list[SearchHit]:
        """Кандидаты. Жёстких фильтров здесь нет — только отбор.

        Кабинет или раздел добавляют к найденному по словам **все** товары этого
        размещения: слова запроса («оборудование для кабинета информатики»)
        описывают сам кабинет, и по ним одним товары раздела не находятся.
        """
        if query.article:
            product = self.repository.get_by_article(query.article)
            return [SearchHit(product, 1.0, "article")] if product else []

        audience = institution_code(query.institution_type)
        point = _point(query.norm_point)
        searched = bool(query.query or point)
        hits = (
            self.repository.search_text(
                query.query, limit=CANDIDATE_LIMIT, audience=audience, norm_point=point
            )
            if searched
            else []
        )
        structural = bool(query.room or query.category)
        if searched and not structural:
            return hits

        room = room_in(query.room)
        if query.room and room is None:
            return hits
        seen = {hit.product.sku_1c for hit in hits}
        reason = PLACEMENT if structural else LISTING
        for product in self.repository.list_active():
            if product.sku_1c in seen:
                continue
            if structural and not _in_placement(product, room, query.category):
                continue
            hits.append(SearchHit(product, 0.0, reason, audience=audience, query=query.query))
        return hits

    def filter(
        self, hits: list[SearchHit], query: CatalogQuery
    ) -> tuple[list[SearchHit], list[FilterReport]]:
        """Жёсткие фильтры по порядку. Каждый заданный фильтр попадает в отчёт."""
        candidates = [
            _Candidate(hit, list(hit.product.placements))
            for hit in hits
            if hit.product.is_active
        ]
        reports: list[FilterReport] = []
        for step in _steps(query):
            kept: list[_Candidate] = []
            excluded = unknown = 0
            for candidate in candidates:
                verdict = step.check(candidate)
                unknown += verdict is _Verdict.UNKNOWN
                if verdict is _Verdict.MATCH or (
                    verdict is _Verdict.UNKNOWN and step.keep_unknown
                ):
                    kept.append(candidate)
                else:
                    excluded += 1
            reports.append(_report(step, excluded, unknown))
            candidates = kept
        reports.extend(_not_applied(query))
        return [candidate.hit for candidate in candidates], reports

    def rank(self, hits: list[SearchHit]) -> list[SearchHit]:
        """Найденное по словам — в порядке поиска, остальное из раздела — по названию."""
        relevant = [hit for hit in hits if hit.reason in _BY_RELEVANCE]
        rest = sorted(
            (hit for hit in hits if hit.reason not in _BY_RELEVANCE),
            key=lambda hit: hit.product.name,
        )
        return relevant + rest

    def present(self, product: Product, audience: str | None = None) -> ProductView:
        placements = product.placements
        return ProductView(
            id=product.id,
            article=product.article,
            commercial=product.commercial,
            card=product.card,
            placement=PlacementView(
                institution_types=sorted(product.institution_types),
                rooms=sorted(product.rooms),
                paths=[list(placement.path) for placement in placements],
                source=product.placement_source,
            ),
            norms=product.norms_for(institution_code(audience)),
        )


# --- Фильтры ------------------------------------------------------------------


def _steps(query: CatalogQuery) -> list[_Step]:
    steps: list[_Step] = []
    if query.institution_type:
        code = institution_code(query.institution_type)
        steps.append(
            _Step(
                "institution_type",
                query.institution_type,
                _by_institution(code),
                note="" if code else "тип учреждения не распознан — подходящих товаров нет",
            )
        )
    if query.room:
        room = room_in(query.room)
        steps.append(
            _Step(
                "room",
                query.room,
                _narrow(lambda placement: placement.room, room),
                note="" if room else "такое помещение разделом каталога не выделено — подходящих товаров нет",
            )
        )
    if query.category:
        steps.append(_Step("category", query.category, _by_category(query.category)))
    if query.age_group and (age := parse_age(query.age_group)):
        steps.append(_Step("age_group", query.age_group, _by_age(age), keep_unknown=True))
    if query.norm_document or query.norm_point:
        doc = _document(query.norm_document)
        point = _point(query.norm_point)
        recognized = (doc or not query.norm_document) and (point or not query.norm_point)
        steps.append(
            _Step(
                "norm",
                " ".join(filter(None, (query.norm_document, query.norm_point))),
                _by_norm(doc, point) if recognized else _nothing,
                note="" if recognized else "документ или пункт не распознан — подходящих товаров нет",
            )
        )
    if query.manufacturer:
        steps.append(_Step("manufacturer", query.manufacturer, _by_manufacturer(query.manufacturer)))
    if query.price_min is not None or query.price_max is not None:
        steps.append(
            _Step("price", (query.price_min, query.price_max), _by_price(query.price_min, query.price_max))
        )
    if query.available_only:
        steps.append(_Step("available_only", True, _by_availability))
    return steps


def _not_applied(query: CatalogQuery) -> list[FilterReport]:
    reports: list[FilterReport] = []
    if query.zone:
        reports.append(
            FilterReport(
                "zone",
                query.zone,
                FilterStatus.NOT_APPLIED,
                note="разметки зон в каталоге нет — выдача по зоне не сужена",
            )
        )
    if query.age_group and parse_age(query.age_group) is None:
        reports.append(
            FilterReport(
                "age_group",
                query.age_group,
                FilterStatus.NOT_APPLIED,
                note="возраст не разобран: нужен вид «3–4 года», «5 лет» или «3+»",
            )
        )
    if query.institution_name:
        reports.append(
            FilterReport(
                "institution_name",
                query.institution_name,
                FilterStatus.NOT_APPLIED,
                note="название учреждения каталог не сужает",
            )
        )
    return reports


def _report(step: _Step, excluded: int, unknown: int) -> FilterReport:
    notes = [step.note] if step.note else []
    if unknown:
        fate = "оставлены" if step.keep_unknown else "исключены"
        notes.append(f"нет данных у товаров: {unknown} — {fate}")
    return FilterReport(
        name=step.name,
        value=step.value,
        status=FilterStatus.PARTIAL if unknown else FilterStatus.APPLIED,
        excluded=excluded,
        unknown=unknown,
        note="; ".join(notes),
    )


def _by_institution(wanted: str | None) -> Callable[[_Candidate], _Verdict]:
    """Учреждение не стирает «нейтральные» разделы (К9.1).

    Размещение без учреждения («Коррекционная среда», «Инновационные решения»)
    подходит и саду, и школе. Прежний общий фильтр оставлял товару только
    размещения с учреждением, поэтому 61 позиция нейтральных корней выпадала
    при любом запросе, а подбор по кабинету логопеда находил одни заготовки
    «Оснащения новостроек». Исключается только товар, у которого все размещения
    с учреждением чужие и ни одного нейтрального нет.
    """

    def check(candidate: _Candidate) -> _Verdict:
        if wanted is None:
            return _Verdict.MISMATCH
        known = [p for p in candidate.placements if p.institution]
        neutral = [p for p in candidate.placements if not p.institution]
        if not known and not neutral:
            return _Verdict.UNKNOWN
        matching = [p for p in known if p.institution == wanted]
        if matching:
            candidate.placements = matching + neutral
            return _Verdict.MATCH
        if neutral:
            candidate.placements = neutral
            return _Verdict.MATCH
        return _Verdict.MISMATCH

    return check


def _narrow(
    attribute: Callable[[Placement], str | None], wanted: str | None
) -> Callable[[_Candidate], _Verdict]:
    """Фильтр по свойству размещения, который оставляет только подошедшие размещения."""

    def check(candidate: _Candidate) -> _Verdict:
        if wanted is None:
            return _Verdict.MISMATCH
        known = [placement for placement in candidate.placements if attribute(placement)]
        if not known:
            return _Verdict.UNKNOWN
        matching = [placement for placement in known if attribute(placement) == wanted]
        if not matching:
            return _Verdict.MISMATCH
        candidate.placements = matching
        return _Verdict.MATCH

    return check


def _by_category(category: str) -> Callable[[_Candidate], _Verdict]:
    def check(candidate: _Candidate) -> _Verdict:
        if not candidate.placements:
            return _Verdict.UNKNOWN
        matching = [p for p in candidate.placements if matches_category(p, category)]
        if not matching:
            return _Verdict.MISMATCH
        candidate.placements = matching
        return _Verdict.MATCH

    return check


def _by_age(age: AgeRange) -> Callable[[_Candidate], _Verdict]:
    """Возраст подраздела приказа — точный признак, а не пересечение (шаг 3.7).

    Прогон 04.10, BUG-11: группе 3–7 лет предлагали подраздел 1.14.5 (3–4 года)
    со словами «для 4–7 то же самое»; в переписке 23.09 на «дети 3–7» прилетал
    ящик для детей до года (1.14.2.2.3). Подраздел подходит, если его диапазон
    вложен в запрошенный ИЛИ пересекается с соседним («3–4» при запросе «2–4»);
    дальний чужой возраст («до года» при 3–7) — вне выдачи. Точный возраст
    («5 лет») внутри подраздела 5–6 тоже подходит. Товар без возраста остаётся.
    """

    def check(candidate: _Candidate) -> _Verdict:
        ranges = [p.age for p in candidate.placements if p.age]
        if candidate.hit.product.card_age:
            ranges.append(candidate.hit.product.card_age)
        if not ranges:
            return _Verdict.UNKNOWN
        for known in ranges:
            inside = known.min_years >= age.min_years and (
                age.max_years is None
                or (known.max_years is not None and known.max_years <= age.max_years)
            )
            exact = (
                age.min_years == age.max_years
                and known.min_years <= age.min_years <= (known.max_years or age.min_years)
            )
            if inside or exact or age.overlaps(known):
                return _Verdict.MATCH
        return _Verdict.MISMATCH

    return check


def _by_norm(doc: str | None, point: str | None) -> Callable[[_Candidate], _Verdict]:
    def check(candidate: _Candidate) -> _Verdict:
        refs = candidate.hit.product.norms
        if not refs:
            return _Verdict.UNKNOWN
        for ref in refs:
            if doc and ref.doc_id != doc:
                continue
            if point and not _within(ref.item_code, point):
                continue
            return _Verdict.MATCH
        return _Verdict.MISMATCH

    return check


def _by_manufacturer(name: str) -> Callable[[_Candidate], _Verdict]:
    wanted = _fold(name)

    def check(candidate: _Candidate) -> _Verdict:
        brand = candidate.hit.product.manufacturer
        if not brand:
            return _Verdict.UNKNOWN
        return _Verdict.MATCH if _fold(brand) == wanted else _Verdict.MISMATCH

    return check


def _by_price(low: int | None, high: int | None) -> Callable[[_Candidate], _Verdict]:
    def check(candidate: _Candidate) -> _Verdict:
        price = candidate.hit.product.price
        if price is None:
            return _Verdict.UNKNOWN
        if low is not None and price < low:
            return _Verdict.MISMATCH
        if high is not None and price > high:
            return _Verdict.MISMATCH
        return _Verdict.MATCH

    return check


def _by_availability(candidate: _Candidate) -> _Verdict:
    availability = candidate.hit.product.availability
    if availability is Availability.UNKNOWN:
        return _Verdict.UNKNOWN
    return _Verdict.MATCH if availability is Availability.AVAILABLE else _Verdict.MISMATCH


def _nothing(_candidate: _Candidate) -> _Verdict:
    return _Verdict.MISMATCH


# --- Мелочи -------------------------------------------------------------------


def _in_placement(product: Product, room: str | None, category: str | None) -> bool:
    return any(
        (room is None or placement.room == room)
        and (category is None or matches_category(placement, category))
        for placement in product.placements
    )


def _document(value: str | None) -> str | None:
    """`order_838` из «order_838», «838» или «приказ № 838»."""
    text = (value or "").strip()
    if not text:
        return None
    if text in norm_docs.DOCUMENTS:
        return text
    if f"order_{text}" in norm_docs.DOCUMENTS:
        return f"order_{text}"
    found = document_ids_in_text(text)
    return found[0] if found else None


def _point(value: str | None) -> str | None:
    found = codes_in_query(value or "")
    return found[0] if found else None


def _within(code: str | None, point: str) -> bool:
    """Пункт совпадает или лежит внутри подраздела: «2.4» включает «2.4.35»."""
    return bool(code) and (code == point or code.startswith(f"{point}."))


def _fold(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())
