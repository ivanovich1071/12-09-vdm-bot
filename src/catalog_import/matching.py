"""Сравнение товаров импорта 1С с каталогом бота (EPIC 3).

Состояние позиции решает импорт — по точному коду 1С, как и предпросмотр EPIC 2:

- `EXISTING` — код есть в каталоге. Сопоставление проверяет только пару «код →
  товар»: название, цифры, «+», производитель. Другой товар по названию не ищется:
  код не переезжает на соседа из-за похожего названия;
- `NEW` — кода в каталоге нет. Если из файла исчезли старые коды, новый товар ищется
  только среди них: это возможная перекодировка. Исчезнувших кодов нет —
  сопоставление для новых товаров не вызывается;
- `MISSING` — код каталога в файле не встретился. Это только отметка: что делать
  с товаром, решает EPIC 4.

`NEW` и `MISSING` — состояния импорта, а не статусы сопоставления: в `MatchStatus`
их нет, и результат сопоставления их не подменяет. Сохранение результатов по
позициям (`catalog_matches`) — EPIC 4.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from catalog.matcher import MatchInput, MatchResult, MatchStatus
from catalog.models import Product
from catalog_import.models import CatalogComparison, ImportItem

if TYPE_CHECKING:
    from catalog.matcher import CatalogMatcher
    from catalog.repository import CatalogRepository


class CodeState(StrEnum):
    EXISTING = "EXISTING"
    NEW = "NEW"
    MISSING = "MISSING"


CODE_STATE_LABELS: dict[CodeState, str] = {
    CodeState.EXISTING: "Товар есть в каталоге",
    CodeState.NEW: "Новый товар",
    CodeState.MISSING: "Товар отсутствует в новом файле 1С",
}

# Подписи для счётчиков предпросмотра. Полные фразы — `MatchResult.status_label`.
MATCH_STATUS_SHORT_LABELS: dict[MatchStatus, str] = {
    MatchStatus.MATCHED_EXACT: "совпадают по коду и названию",
    MatchStatus.MATCHED_HIGH: "уверенное совпадение",
    MatchStatus.MATCHED_REVIEW: "требуют проверки менеджера",
    MatchStatus.AMBIGUOUS: "несколько подходящих товаров",
    MatchStatus.NOT_FOUND: "кандидата нет",
}


@dataclass(frozen=True)
class CodeMatch:
    """Позиция сравнения: `old_*` — каталог бота, `new_*` — файл 1С."""

    state: CodeState
    old_article: str | None
    new_article: str | None
    old_name: str | None
    new_name: str | None
    # EXISTING — проверка кода; NEW — поиск среди исчезнувших кодов или `None`,
    # если исчезнувших нет; MISSING — всегда `None`.
    match: MatchResult | None = None

    @property
    def is_recoding_candidate(self) -> bool:
        """Новому коду нашёлся кандидат среди исчезнувших — возможно, товар перекодирован."""
        return (
            self.state is CodeState.NEW
            and self.match is not None
            and self.match.status is not MatchStatus.NOT_FOUND
        )

    @property
    def state_label(self) -> str:
        return CODE_STATE_LABELS[self.state]

    @property
    def message(self) -> str:
        """Итог позиции для менеджера — на русском."""
        text = f"{self.state_label}."
        if self.match is None:
            return text
        if self.state is CodeState.NEW:
            if not self.is_recoding_candidate:
                return f"{text} Среди исчезнувших кодов подходящего товара нет."
            return f"{text} Возможна перекодировка. {self.match.message}"
        return f"{text} {self.match.message}"

    def to_dict(self) -> dict[str, Any]:
        """Машинный контракт: коды без подписей."""
        match = self.match.to_dict() if self.match else {}
        return {
            "state": str(self.state),
            "old_article": self.old_article,
            "new_article": self.new_article,
            "old_name": self.old_name,
            "new_name": self.new_name,
            "match_status": match.get("status"),
            "match_method": match.get("method"),
            "confidence": match.get("confidence"),
            "matched_product_id": match.get("product_id"),
            "candidates": match.get("candidates", []),
            "reason_codes": match.get("reason_codes", []),
        }


@dataclass(frozen=True)
class CatalogMatching:
    comparison: CatalogComparison
    existing: tuple[CodeMatch, ...] = ()
    new: tuple[CodeMatch, ...] = ()
    missing: tuple[CodeMatch, ...] = ()


def compare_with_catalog(
    items: Iterable[ImportItem],
    file_codes: Iterable[str],
    catalog: CatalogRepository,
    matcher: CatalogMatcher,
) -> CatalogMatching:
    """Сравнить принятые товары импорта с каталогом бота.

    `file_codes` — все коды файла, включая исключённые из-за ошибок: счётчики
    остаются такими же, как в EPIC 2, а исключённый код не считается исчезнувшим.
    """
    current = {product.id: product for product in catalog.list_active()}
    codes = frozenset(file_codes)
    disappeared = frozenset(current.keys() - codes)

    existing: list[CodeMatch] = []
    new: list[CodeMatch] = []
    for item in items:
        query = match_input(item)
        old = current.get(item.sku_1c)
        if old is not None:
            # Пустой `among`: даже если код окажется неактивным, поиска по названию нет.
            result = matcher.match(query, among=())
            existing.append(
                CodeMatch(CodeState.EXISTING, old.id, item.sku_1c, old.name, item.name, result)
            )
            continue
        result = matcher.match(query, among=disappeared) if disappeared else None
        proposed = result.matched_product if result else None
        new.append(
            CodeMatch(
                CodeState.NEW,
                proposed.id if proposed else None,
                item.sku_1c,
                proposed.name if proposed else None,
                item.name,
                result,
            )
        )

    missing = tuple(
        CodeMatch(CodeState.MISSING, code, None, current[code].name, None)
        for code in sorted(disappeared)
    )
    checked = [position for position in new if position.match is not None]
    comparison = CatalogComparison(
        in_catalog=len(current.keys() & codes),
        new=len(codes - current.keys()),
        missing_from_file=len(disappeared),
        matching=True,
        existing_by_status=_by_status(existing),
        recoding_checked=len(checked),
        recoding_candidates=sum(position.is_recoding_candidate for position in new),
        recoding_by_status=_by_status(checked),
    )
    return CatalogMatching(comparison, tuple(existing), tuple(new), missing)


def match_input(item: ImportItem) -> MatchInput:
    """Признаки товара импорта — тем же разбором, что и у товара каталога."""
    product = Product.from_dict({**item.payload, "sku_1c": item.sku_1c, "name": item.name})
    return MatchInput(
        name=item.name,
        article_1c=item.sku_1c,
        supplier_article=product.supplier_article,
        manufacturer=product.manufacturer,
    )


def _by_status(positions: Iterable[CodeMatch]) -> dict[str, int]:
    counts = Counter(position.match.status for position in positions if position.match)
    return {str(status): counts[status] for status in MatchStatus if counts[status]}
