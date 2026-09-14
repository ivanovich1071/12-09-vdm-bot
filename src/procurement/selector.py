"""Подбор товаров под требование закупки.

Порядок — как у поиска в ТЗ §7: кандидаты → жёсткие фильтры → ранжирование → три
позиции. Кандидаты и фильтры даёт `CatalogService` (EPIC 1): учреждение,
помещение, зона, возраст, норматив, раздел, наличие, цена. Всё это применяется до
ранжирования.

Модель, если её подключить (`GuardedRanker`), видит только отфильтрованных
кандидатов, не больше `LLM_CANDIDATE_LIMIT`, и может лишь переставить их: товар не
из списка отбрасывается. Весь каталог модели не отдаётся.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from catalog.models import Availability, Product
from catalog.placement import parse_age
from catalog.query import CatalogQuery
from catalog.runtime import CatalogRuntimeState
from core.errors import Notice
from norms import documents as docs
from norms.mapping import MappingStatus, NormCheck, NormCheckStatus, NormMapping, NormMappingService
from procurement.models import (
    Alternative,
    ProcurementRequirement,
    ProcurementTask,
    SelectionItem,
    SelectionResult,
    SelectionStatus,
)
from procurement.quantity import QuantityResolver

log = logging.getLogger(__name__)

# Позиций за один показ: три помещаются на экран и не превращают чат в ленту.
PAGE_LIMIT = 3
LLM_CANDIDATE_LIMIT = 30
ALTERNATIVES = 2
_RELEVANCE = frozenset({"article", "norm_code", "text", "trigram"})


@dataclass(frozen=True)
class Candidate:
    product: Product
    # Порядок после фильтров каталога: у найденного по словам — релевантность.
    position: int
    by_query: bool
    in_room: bool
    mappings: tuple[NormMapping, ...]
    norm: NormCheck
    # Возраст раздела совпадает с запрошенным точно. Фильтр возраста пропускает и
    # соседнюю группу («2–3 года» пересекается с «3–4»), но первой должна идти своя.
    age_exact: bool = False

    def brief(self) -> dict[str, Any]:
        """Что видит модель: без описаний и без остального каталога."""
        product = self.product
        return {
            "id": product.id,
            "name": product.name,
            "price": product.price,
            "availability": str(product.availability),
            "section": _section(product, None),
            "norm_points": [m.item_code for m in self.mappings if m.item_code],
        }


class CandidateRanker(Protocol):
    def rank(
        self, requirement: ProcurementRequirement, candidates: list[Candidate]
    ) -> list[Candidate]: ...


class DeterministicRanker:
    """Найденное по словам — в порядке релевантности, остальное из раздела — по данным.

    Внутри раздела выше то, что закрывает норматив, есть в наличии и имеет цену.
    """

    def rank(
        self, requirement: ProcurementRequirement, candidates: list[Candidate]
    ) -> list[Candidate]:
        return sorted(candidates, key=_key)


def _key(candidate: Candidate) -> tuple:
    if candidate.by_query:
        return (0, candidate.position)
    product = candidate.product
    return (
        1,
        not candidate.age_exact,
        candidate.norm.status is not NormCheckStatus.NORM_OK,
        not any(m.status is MappingStatus.APPROVED for m in candidate.mappings),
        product.availability is not Availability.AVAILABLE,
        product.price is None,
        candidate.position,
    )


class GuardedRanker:
    """Перестановка кандидатов внешним ранжировщиком (моделью) под контролем.

    `reorder(требование, кандидаты)` возвращает идентификаторы в желаемом порядке.
    Незнакомые идентификаторы отбрасываются, пропущенные остаются на своих местах,
    ошибка возвращает детерминированный порядок.
    """

    def __init__(
        self,
        reorder: Callable[[dict[str, Any], list[dict[str, Any]]], list[str]],
        fallback: CandidateRanker | None = None,
        limit: int = LLM_CANDIDATE_LIMIT,
    ) -> None:
        self.reorder = reorder
        self.fallback = fallback or DeterministicRanker()
        self.limit = min(limit, LLM_CANDIDATE_LIMIT)

    def rank(
        self, requirement: ProcurementRequirement, candidates: list[Candidate]
    ) -> list[Candidate]:
        ordered = self.fallback.rank(requirement, candidates)
        head, tail = ordered[: self.limit], ordered[self.limit :]
        try:
            wanted = self.reorder(requirement.to_dict(), [c.brief() for c in head])
        except Exception as exc:  # ранжировщик не должен ломать подбор
            log.warning("Внешнее ранжирование не удалось, порядок детерминированный: %s", exc)
            return ordered
        by_id = {candidate.product.id: candidate for candidate in head}
        result: list[Candidate] = []
        for product_id in wanted or []:
            candidate = by_id.pop(product_id, None)
            if candidate is not None:
                result.append(candidate)
        result += [candidate for candidate in head if candidate.product.id in by_id]
        return result + tail


class ProcurementSelector:
    def __init__(
        self,
        mapping: NormMappingService,
        quantities: QuantityResolver,
        ranker: CandidateRanker | None = None,
    ) -> None:
        self.mapping = mapping
        self.quantities = quantities
        self.ranker = ranker or DeterministicRanker()

    def select(
        self,
        task: ProcurementTask,
        requirement: ProcurementRequirement,
        state: CatalogRuntimeState,
        limit: int = PAGE_LIMIT,
    ) -> SelectionResult:
        limit = max(1, min(limit, PAGE_LIMIT))
        norm = requirement.norm
        warnings = list(requirement.warnings)

        questions = _questions(requirement)
        if questions:
            return self._result(
                task, state, requirement, SelectionStatus.NEEDS_DETAILS, (), 0, 0, (), warnings, questions
            )

        query = self.catalog_query(requirement, len(state.index.products))
        found = state.catalog.search(query)
        filters = tuple(_report(report) for report in found.filters)
        warnings += [
            Notice(f"FILTER_{report.name.upper()}_{report.status.value.upper()}", report.note)
            for report in found.filters
            if report.note and report.status.value != "applied"
        ]

        candidates = [
            self._candidate(requirement, hit.product, position, hit.reason in _RELEVANCE)
            for position, hit in enumerate(found.hits)
            if hit.product.id not in requirement.exclude
        ]
        if requirement.user.text:
            # Названы слова запроса — показываем то, что им отвечает. Раздел помещения
            # сужает найденное, но не подменяет запрос: на «мячи» в спортзале не должны
            # приходить маты и доски только потому, что они лежат в том же разделе (NEXT-4.1).
            matched = [candidate for candidate in candidates if candidate.by_query]
            if not matched and requirement.catalog_room is not None:
                found = state.catalog.search(replace(query, room=None))
                filters = tuple(_report(report) for report in found.filters)
                matched = [
                    self._candidate(requirement, hit.product, position, True)
                    for position, hit in enumerate(found.hits)
                    if hit.product.id not in requirement.exclude and hit.reason in _RELEVANCE
                ]
                if matched:
                    warnings.append(
                        Notice(
                            "ROOM_WIDENED",
                            f"В разделе «{requirement.user.room}» по запросу «{requirement.user.text}» "
                            "ничего нет — показаны позиции по запросу из всего каталога.",
                        )
                    )
            candidates = matched
        ranked = self.ranker.rank(requirement, candidates)
        picked, rest = ranked[:limit], ranked[limit:]
        items = tuple(self._item(task, requirement, candidate, rest) for candidate in picked)

        if requirement.user.budget is not None:
            page_total = sum(item.total_price or 0 for item in items)
            if page_total > requirement.user.budget:
                warnings.append(
                    Notice(
                        "BUDGET_EXCEEDED",
                        f"Показанные позиции вместе стоят {page_total} ₽ — больше бюджета "
                        f"{requirement.user.budget} ₽.",
                        {"total": page_total, "budget": requirement.user.budget},
                    )
                )

        if norm.requires_review:
            status = SelectionStatus.REVIEW_REQUIRED
        else:
            status = SelectionStatus.FOUND if items else SelectionStatus.EMPTY
        return self._result(
            task,
            state,
            requirement,
            status,
            items,
            len(rest),
            found.matched,
            filters,
            warnings,
            (),
            candidates=found.candidates,
        )

    def catalog_query(self, requirement: ProcurementRequirement, catalog_size: int) -> CatalogQuery:
        user, norm = requirement.user, requirement.norm
        text = user.text
        if user.room and requirement.catalog_room is None:
            text = f"{text} {user.room}".strip()
        return CatalogQuery(
            query=text,
            institution_type=requirement.audience,
            institution_name=user.institution_name,
            room=requirement.catalog_room,
            zone=user.zone,
            age_group=user.age_group,
            category=user.category,
            norm_document=norm.document if norm.filters else None,
            norm_point=norm.point if norm.filters else None,
            price_max=user.budget,
            available_only=user.available_only,
            limit=max(catalog_size, 1),
        )

    def _candidate(
        self, requirement: ProcurementRequirement, product: Product, position: int, by_query: bool
    ) -> Candidate:
        norm = requirement.norm
        mappings = tuple(self.mapping.mappings(product, requirement.audience))
        check = self.mapping.check(product, norm.document, norm.point, requirement.audience)
        age = parse_age(requirement.user.age_group)
        return Candidate(
            product=product,
            position=position,
            by_query=by_query,
            in_room=requirement.catalog_room is not None
            and requirement.catalog_room in product.rooms,
            mappings=mappings,
            norm=check,
            age_exact=age is not None
            and (age in [p.age for p in product.placements] or product.card_age == age),
        )

    def _item(
        self,
        task: ProcurementTask,
        requirement: ProcurementRequirement,
        candidate: Candidate,
        rest: list[Candidate],
    ) -> SelectionItem:
        product = candidate.product
        decision = self.quantities.resolve(task, product, candidate.mappings, requirement.norm)
        return SelectionItem(
            product_id=product.id,
            article=product.article,
            name=product.name,
            price=product.price,
            currency=product.currency,
            availability=product.availability,
            quantity_available=product.quantity_available,
            quantity=decision.quantity,
            quantity_source=decision.source,
            quantity_note=decision.note,
            total_price=product.price * decision.quantity if product.price is not None else None,
            reason=_reason(requirement, candidate),
            norm_mappings=candidate.mappings,
            norm_status=candidate.norm.status,
            confidence=_confidence(candidate),
            alternatives=_alternatives(requirement, candidate, rest),
            url=product.url,
            image=product.images[0] if product.images else None,
        )

    def _result(
        self,
        task: ProcurementTask,
        state: CatalogRuntimeState,
        requirement: ProcurementRequirement,
        status: SelectionStatus,
        items: tuple[SelectionItem, ...],
        remaining: int,
        matched: int,
        filters: tuple[dict[str, Any], ...],
        warnings: list[Notice],
        questions: tuple[str, ...],
        candidates: int = 0,
    ) -> SelectionResult:
        return SelectionResult(
            task_id=task.id,
            status=status,
            catalog_version=state.label,
            catalog_sha256=state.sha256,
            norm_version=requirement.norm.norm_version,
            items=items,
            has_more=remaining > 0,
            remaining=remaining,
            matched=matched,
            candidates=candidates,
            filters=filters,
            norm=requirement.norm,
            warnings=tuple(warnings),
            questions=questions,
        )


def _questions(requirement: ProcurementRequirement) -> tuple[str, ...]:
    """Спрашиваем, только если искать не по чему: длинной анкеты до первой выдачи нет.

    Одного типа учреждения мало — это тысячи товаров. Хватает помещения, слов о
    товаре, раздела, возраста или пункта перечня.
    """
    user, norm = requirement.user, requirement.norm
    if user.room or user.text or user.category or user.age_group or (norm.filters and norm.point):
        return ()
    if not user.institution_type:
        return ("institution_type", "room")
    return ("room",)


def _reason(requirement: ProcurementRequirement, candidate: Candidate) -> str:
    product, norm = candidate.product, requirement.norm
    parts: list[str] = []
    if candidate.in_room and (section := _section(product, requirement.catalog_room)):
        parts.append(f"раздел каталога «{section}»")
    if candidate.norm.status is NormCheckStatus.NORM_OK and candidate.norm.mapping:
        mapping = candidate.norm.mapping
        name = docs.get(mapping.doc_id).short_name if mapping.doc_id in docs.DOCUMENTS else mapping.doc_id
        parts.append(f"пункт {mapping.item_code}, {name}" if mapping.item_code else name)
    elif norm.document and not norm.filters:
        approved = [m for m in candidate.mappings if m.status is MappingStatus.APPROVED]
        if approved and approved[0].item_code:
            name = docs.get(approved[0].doc_id).short_name if approved[0].doc_id in docs.DOCUMENTS else approved[0].doc_id
            parts.append(f"пункт {approved[0].item_code}, {name}")
    if candidate.by_query and requirement.user.text:
        parts.append(f"совпадает с запросом «{requirement.user.text}»")
    if requirement.user.budget is not None and product.price is not None:
        parts.append("укладывается в бюджет")
    if not parts:
        parts.append("подходит под фильтры задачи")
    return "; ".join(parts)


def _confidence(candidate: Candidate) -> float:
    """Насколько надёжно основание подбора — по данным, а не по мнению модели."""
    status = candidate.norm.status
    if status is NormCheckStatus.NORM_OK and candidate.norm.item_code:
        return 0.95
    if status is NormCheckStatus.NORM_OK and candidate.in_room:
        return 0.9
    if candidate.in_room:
        return 0.8
    if candidate.by_query:
        return 0.6
    return 0.5


def _alternatives(
    requirement: ProcurementRequirement, candidate: Candidate, rest: list[Candidate]
) -> tuple[Alternative, ...]:
    """Замены из того же раздела — не показанные и не отклонённые."""
    section = _section(candidate.product, requirement.catalog_room)
    if section is None:
        return ()
    found: list[Alternative] = []
    for other in rest:
        if _section(other.product, requirement.catalog_room) != section:
            continue
        product = other.product
        found.append(Alternative(product.id, product.article, product.name, product.price, product.availability))
        if len(found) == ALTERNATIVES:
            break
    return tuple(found)


def _section(product: Product, room: str | None) -> str | None:
    placements = [p for p in product.placements if room and p.room == room] or product.placements
    for placement in placements:
        if placement.sections:
            return placement.sections[-1]
    return None


def _report(report) -> dict[str, Any]:  # noqa: ANN001 — catalog.query.FilterReport
    value = report.value
    return {
        "name": report.name,
        "value": list(value) if isinstance(value, tuple) else value,
        "status": str(report.status),
        "excluded": report.excluded,
        "unknown": report.unknown,
        "note": report.note,
    }
