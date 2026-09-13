"""Доступ к каталогу.

`CatalogRepository` — то, на что опирается `CatalogService`. Реализация пока одна:
каталог в памяти поверх `CatalogIndex`, собранного из базы знаний. Хранилище за
ней заменяется (SQLite, PostgreSQL), не трогая сервис и его потребителей (D4).

`load_products` и `load_index` остаются: ими пользуется сборка приложения.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from catalog.models import Product
from catalog.search import CatalogIndex, SearchHit, SearchQuery

DEFAULT_KB = Path("data/kb/products.jsonl")


class CatalogRepository(Protocol):
    def get_product(self, product_id: str) -> Product | None: ...

    def get_by_article(self, article: str) -> Product | None: ...

    def list_active(self) -> list[Product]: ...

    def search_text(
        self,
        text: str,
        *,
        limit: int,
        audience: str | None = None,
        norm_point: str | None = None,
    ) -> list[SearchHit]:
        """Кандидаты по словам и номеру пункта, в порядке релевантности."""
        ...


class InMemoryCatalogRepository:
    """Репозиторий поверх существующего индекса. Алгоритм поиска не меняется."""

    def __init__(self, index: CatalogIndex) -> None:
        self.index = index

    @classmethod
    def from_path(cls, path: str | Path = DEFAULT_KB) -> InMemoryCatalogRepository:
        return cls(load_index(path))

    def get_product(self, product_id: str) -> Product | None:
        return self.index.get(product_id)

    def get_by_article(self, article: str) -> Product | None:
        # Артикул — код 1С, то есть тот же ключ, что и id (D8, решение A).
        # Артикул поставщика не ищем: он не уникален («065», «221»).
        article = (article or "").strip()
        return self.index.get(article) if article else None

    def list_active(self) -> list[Product]:
        return [product for product in self.index.products if product.is_active]

    def search_text(
        self,
        text: str,
        *,
        limit: int,
        audience: str | None = None,
        norm_point: str | None = None,
    ) -> list[SearchHit]:
        return self.index.search(
            SearchQuery(text=text, limit=limit, audience=audience, norm_code=norm_point)
        )


class ProductListRepository:
    """Репозиторий без поискового индекса — для сопоставления и diff.

    `CatalogMatcher` нужны только товар по коду и список активных. Строить ради
    этого `CatalogIndex` — лишние 2,7 с на каждый импорт (D10, ограничения).
    """

    def __init__(self, products: list[Product]) -> None:
        self._products = list(products)
        self._by_code = {product.sku_1c: product for product in self._products}

    def get_product(self, product_id: str) -> Product | None:
        return self._by_code.get(product_id)

    def get_by_article(self, article: str) -> Product | None:
        article = (article or "").strip()
        return self._by_code.get(article) if article else None

    def list_active(self) -> list[Product]:
        return [product for product in self._products if product.is_active]

    def search_text(
        self,
        text: str,
        *,
        limit: int,
        audience: str | None = None,
        norm_point: str | None = None,
    ) -> list[SearchHit]:
        raise NotImplementedError("Поиск по словам требует CatalogIndex: InMemoryCatalogRepository.")


def load_products(path: str | Path = DEFAULT_KB) -> list[Product]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"База знаний не собрана: {path}. Запустите "
            "`python -m ingest.build_kb --source <выгрузка.xlsx>`."
        )
    with path.open(encoding="utf-8") as fh:
        return [Product.from_dict(json.loads(line)) for line in fh if line.strip()]


@lru_cache(maxsize=4)
def load_index(path: str | Path = DEFAULT_KB) -> CatalogIndex:
    """Индекс собирается один раз на процесс: построение занимает секунды."""
    return CatalogIndex(load_products(path))
