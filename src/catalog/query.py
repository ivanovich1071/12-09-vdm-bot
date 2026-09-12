"""Запрос к каталогу и отчёт о том, как он исполнен.

Запрос структурированный: учреждение, кабинет, раздел и цена — отдельные поля,
а не слова в тексте. Текст остаётся для поиска по названию и описанию.

Каждый заданный фильтр возвращается в отчёте со статусом. Фильтр, который не
реализован или которому не хватило данных, молча не пропускается: иначе выдача
снова наполняется товарами из чужих разделов, а вызывающий код об этом не знает.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from catalog.models import Product
from catalog.search import SearchHit, SearchQuery, apply_text_filters


class FilterStatus(StrEnum):
    APPLIED = "applied"
    # Применён, но у части кандидатов нужных данных нет. Что с ними сделано —
    # оставлены или исключены — написано в `note`.
    PARTIAL = "partial"
    # Не применён: данных для него в каталоге нет. Выдачу он не сузил.
    NOT_APPLIED = "not_applied"


@dataclass(frozen=True)
class CatalogQuery:
    query: str = ""
    # Код 1С (D8, решение A). Если задан, остальной поиск не выполняется.
    article: str | None = None
    # `preschool` / `school` или слово: «детский сад», «школа».
    institution_type: str | None = None
    # Название учреждения каталог не сужает; поле нужно профилю задачи.
    institution_name: str | None = None
    room: str | None = None
    # Разметки зон в каталоге нет — фильтр возвращается как не применённый.
    zone: str | None = None
    age_group: str | None = None
    category: str | None = None
    manufacturer: str | None = None
    # `order_838`, «838» или «приказ 1057».
    norm_document: str | None = None
    norm_point: str | None = None
    price_min: int | None = None
    price_max: int | None = None
    available_only: bool = False
    limit: int = 20

    def with_text_hints(self) -> CatalogQuery:
        """Цена и наличие, названные в тексте, становятся полями запроса.

        Старый поиск разбирает «мячи в наличии до 2000 руб» внутри себя. Здесь это
        вынесено наружу, чтобы условие попало в отчёт о фильтрах, а не сработало
        незаметно.
        """
        lifted = apply_text_filters(
            SearchQuery(
                text=self.query,
                in_stock_only=self.available_only,
                price_min=self.price_min,
                price_max=self.price_max,
            )
        )
        return replace(
            self,
            query=lifted.text,
            available_only=lifted.in_stock_only,
            price_min=lifted.price_min,
            price_max=lifted.price_max,
        )


@dataclass(frozen=True)
class FilterReport:
    name: str
    value: object
    status: FilterStatus
    excluded: int = 0
    # У скольких кандидатов для этого фильтра не было данных.
    unknown: int = 0
    note: str = ""


@dataclass
class CatalogResult:
    query: CatalogQuery
    hits: list[SearchHit]
    filters: list[FilterReport]
    # Сколько товаров дал поиск и сколько осталось после фильтров — до обрезки по `limit`.
    candidates: int
    matched: int

    @property
    def products(self) -> list[Product]:
        return [hit.product for hit in self.hits]

    @property
    def not_applied(self) -> list[FilterReport]:
        return [report for report in self.filters if report.status is FilterStatus.NOT_APPLIED]

    def filter(self, name: str) -> FilterReport | None:
        return next((report for report in self.filters if report.name == name), None)
