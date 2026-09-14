"""Сопоставление строк заказа с каталогом — общий matcher EPIC 3.

Приоритет строгий: код 1С → точное название → нормализованное название →
артикул поставщика и производитель → похожее название → пункт перечня строки →
подсказка модели → ручная проверка. `MATCHED_REVIEW` и `AMBIGUOUS` автоматическим
соответствием не становятся, подсказка модели — не выше `MATCHED_REVIEW` и только из
кандидатов matcher.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from catalog.matcher import CatalogMatcher, MatchCandidate, MatchInput, MatchSettings, MatchStatus
from catalog.models import Availability, Product
from catalog.repository import ProductListRepository
from catalog.runtime import CatalogRuntimeState
from order_import.models import UploadedOrderItem

log = logging.getLogger(__name__)

# Формат кода 1С в выгрузке заказчика: «0Э-00005297» или число из 3–6 цифр.
_ONE_C_CODE = re.compile(r"^(?:0[ЭЮ]-\d{4,}|\d{3,6})$")
MANUAL = "manual"
AI_ASSISTED = "ai_assisted"
NORM_POINT = "norm_point"
_MATCHED = (MatchStatus.MATCHED_EXACT, MatchStatus.MATCHED_HIGH)


@dataclass(frozen=True)
class OrderMatch:
    status: MatchStatus
    method: str
    confidence: float
    product_id: str | None
    candidates: tuple[MatchCandidate, ...]
    reasons: tuple[str, ...]

    def candidates_dict(self) -> list[dict[str, Any]]:
        return [
            {"product_id": c.product_id, "article": c.article, "name": c.name, "score": c.score}
            for c in self.candidates
        ]


class MatchAssistant(Protocol):
    """Подсказка модели для ненайденной или неоднозначной строки."""

    def suggest(self, item: UploadedOrderItem, candidates: Sequence[MatchCandidate]) -> str | None: ...


class OrderMatcher:
    def __init__(
        self,
        state: CatalogRuntimeState,
        settings: MatchSettings | None = None,
        assistant: MatchAssistant | None = None,
    ) -> None:
        self.state = state
        self.matcher = CatalogMatcher(ProductListRepository(state.index.products), settings)
        self.assistant = assistant
        self._points: dict[str | None, dict[str, list[Product]]] = {}

    def match(self, item: UploadedOrderItem, document: str | None = None) -> OrderMatch:
        if item.manual_product_id:
            product = self.state.index.get(item.manual_product_id)
            if product is not None and product.is_active:
                return OrderMatch(MatchStatus.MATCHED_EXACT, MANUAL, 1.0, product.id, (), ("MANUAL_MATCH",))
            return OrderMatch(MatchStatus.NOT_FOUND, MANUAL, 0.0, None, (), ("MANUAL_PRODUCT_NOT_IN_CATALOG",))

        article = item.article or ""
        code = article if article and (self.state.index.get(article) or _ONE_C_CODE.match(article)) else None
        result = self.matcher.match(
            MatchInput(
                name=item.name or "",
                article_1c=code,
                supplier_article=article if article and code is None else None,
                manufacturer=item.manufacturer,
            )
        )
        found = OrderMatch(
            status=result.status,
            method=str(result.method),
            confidence=result.confidence,
            product_id=result.product_id,
            candidates=result.candidates,
            reasons=tuple(str(reason) for reason in result.reason_codes),
        )
        if found.status not in _MATCHED and item.norm_item:
            by_point = self._by_point(item, document)
            if by_point is not None:
                return by_point
        if (
            self.assistant is not None
            and found.status in (MatchStatus.NOT_FOUND, MatchStatus.AMBIGUOUS)
            and found.candidates
        ):
            try:
                suggestion = self.assistant.suggest(item, found.candidates)
            except Exception as exc:  # подсказка не должна ломать проверку заказа
                log.warning("Подсказка сопоставления не получена: %s", exc)
                suggestion = None
            if suggestion in {candidate.product_id for candidate in found.candidates}:
                return replace(
                    found,
                    status=MatchStatus.MATCHED_REVIEW,
                    method=AI_ASSISTED,
                    product_id=suggestion,
                    reasons=(*found.reasons, "AI_SUGGESTED"),
                )
        return found

    def _by_point(self, item: UploadedOrderItem, document: str | None) -> OrderMatch | None:
        """Строка со своим пунктом перечня — среди товаров каталога, привязанных к этому пункту.

        14.09 прислали комплектацию бота, вставленную в Word: «1.13.3.3.27 Логопедические зонды —
        1 шт.». Названия перечня с названиями каталога не совпадают («Набор зондов логопедических»),
        и из 65 строк не нашлась ни одна, хотя к 20 пунктам товары привязаны. Пункт — данные
        реестра, а не догадка: единственный товар пункта — соответствие; из нескольких название
        выбирает, а если не выбрало — предлагаем лучший на проверку менеджеру.
        """
        pool = self._point_products(document).get(item.norm_item or "", [])
        if not pool:
            return None
        named = self.matcher.match(MatchInput(name=item.name or ""), among=[product.id for product in pool])
        if named.status in _MATCHED and named.matched_product is not None:
            product, status = named.matched_product, named.status
        elif len(pool) == 1:
            product, status = pool[0], MatchStatus.MATCHED_HIGH
        else:
            product, status = named.matched_product or _best(pool), MatchStatus.MATCHED_REVIEW
        candidates = tuple(
            MatchCandidate(other.id, other.article, other.name, 1.0 if other.id == product.id else 0.0, ())
            for other in pool[:5]
        )
        return OrderMatch(status, NORM_POINT, named.confidence, product.id, candidates, ("NORM_POINT",))

    def _point_products(self, document: str | None) -> dict[str, list[Product]]:
        """Товары по пунктам перечня — один проход по каталогу на заказ."""
        if document not in self._points:
            found: dict[str, list[Product]] = {}
            for product in self.state.index.products:
                if not product.is_active:
                    continue
                for ref in product.norms:
                    if not ref.item_code or (document is not None and ref.doc_id != document):
                        continue
                    bucket = found.setdefault(ref.item_code, [])
                    if all(other.id != product.id for other in bucket):
                        bucket.append(product)
            self._points[document] = found
        return self._points[document]


def _best(products: list[Product]) -> Product:
    """Из равных по названию — то, что есть в наличии и с ценой, дальше дешевле."""
    return min(
        products,
        key=lambda product: (product.availability is not Availability.AVAILABLE, product.price is None, product.price or 0),
    )
