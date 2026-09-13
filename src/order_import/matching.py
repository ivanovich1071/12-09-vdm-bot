"""Сопоставление строк заказа с каталогом — общий matcher EPIC 3.

Приоритет строгий: код 1С → точное название → нормализованное название →
артикул поставщика и производитель → похожее название → подсказка модели →
ручная проверка. `MATCHED_REVIEW` и `AMBIGUOUS` автоматическим соответствием не
становятся, подсказка модели — не выше `MATCHED_REVIEW` и только из кандидатов
matcher.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from catalog.matcher import CatalogMatcher, MatchCandidate, MatchInput, MatchSettings, MatchStatus
from catalog.repository import ProductListRepository
from catalog.runtime import CatalogRuntimeState
from order_import.models import UploadedOrderItem

log = logging.getLogger(__name__)

# Формат кода 1С в выгрузке заказчика: «0Э-00005297» или число из 3–6 цифр.
_ONE_C_CODE = re.compile(r"^(?:0[ЭЮ]-\d{4,}|\d{3,6})$")
MANUAL = "manual"
AI_ASSISTED = "ai_assisted"


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

    def match(self, item: UploadedOrderItem) -> OrderMatch:
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
