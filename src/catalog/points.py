"""Товар по пункту перечня: привязка реестра, код в названии, формулировка приказа.

«Пункт без привязки» — не то же самое, что «товара нет». Каталог заказчика назван по перечню:
у полутора тысяч товаров название начинается с кода пункта («1.5.1.5 Балансиры напольные разного
типа»), а привязки к реестру у них нет. 16.09 из двадцати пунктов спортзала бот собрал тринадцать
и семь объявил отсутствующими — притом что четыре из семи лежат в каталоге под своим же номером.

Три ступени, от точного к приблизительному:

1. привязка реестра — основание товара (`product.norms`);
2. код пункта в начале названия товара — тот же номер, написанный самим заказчиком;
3. формулировка приказа словами — и только если название товара о том же предмете.

Третья ступень одна может ошибиться, поэтому ограничена дважды. Товар, чьё название начинается
с **другого** номера, заменой не считается: по формулировке «Мат гимнастический 1000×1000×80»
ближайшим оказывается «1.5.1.9 Мат гимнастический 2000×1100×80» — другой пункт и другой товар.
И главное слово пункта должно стоять в названии: «Тоннель для эстафет» не подменяется канатом.
Подбор третьей ступени помечается (`confirmed` = False) — человек видит, что позиция на проверку.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from catalog.models import Availability, Product
from catalog.search import CatalogIndex, SearchQuery
from catalog.text import stems

REGISTRY, NAME_CODE, TITLE = "registry", "name_code", "title"
# Способы, в которых номер пункта назван прямо: реестром или самим каталогом.
CONFIRMED = frozenset({REGISTRY, NAME_CODE})
HOW_LABELS = {
    REGISTRY: "по перечню",
    NAME_CODE: "по номеру в названии",
    TITLE: "по формулировке — проверьте",
}

# Название товара, начинающееся с кода пункта: «2.14.106 Установка для изучения фотоэффекта».
_NAME_CODE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3}){1,5})(?![\d.])")
# Сколько кандидатов поиска по формулировке рассматриваем.
CANDIDATES = 5
# Сколько совпавших слов довольно, чтобы считать предмет тем же: у длинных пунктов хвост —
# это размеры и цвет, и требовать половину от восьми слов значит не найти ничего.
ENOUGH = 4
# Слова размеров и упаковки: они есть у всего и о предмете не говорят.
_SKIP = frozenset(
    {
        "шт", "см", "мм", "размер", "длин", "ширин", "высот", "диаметр", "толщин",
        "тип", "разн", "цвет", "прим", "миллиметр", "метр", "модел", "вид",
    }
)


@dataclass(frozen=True)
class PointMatch:
    code: str
    product: Product
    how: str

    @property
    def confirmed(self) -> bool:
        """Номер пункта назван прямо, а не выведен из слов."""
        return self.how in CONFIRMED

    @property
    def label(self) -> str:
        return HOW_LABELS[self.how]


def name_code(name: str) -> str:
    """Код пункта в начале названия товара, если он там есть."""
    match = _NAME_CODE.match(name or "")
    return match.group(1) if match else ""


class PointFinder:
    """Поиск товара по пункту перечня. Один на операцию: словарь названий строится один раз."""

    def __init__(
        self,
        index: CatalogIndex,
        registry=None,  # noqa: ANN001 — norms.items.ItemIndex, необязателен
        doc_ids: tuple[str, ...] = (),
        audience: str | None = None,
    ) -> None:
        self.index = index
        self.registry = registry
        self.doc_ids = tuple(doc_ids)
        self.audience = audience
        self._named: dict[str, list[Product]] | None = None

    def find(self, code: str, title: str | None = None) -> PointMatch | None:
        found = best(self._registry(code))
        if found is not None:
            return PointMatch(code, found, REGISTRY)
        found = best(self._named_by_code().get(code, []))
        if found is not None:
            return PointMatch(code, found, NAME_CODE)
        wording = title or self.title(code)
        found = self._by_title(code, wording) if wording else None
        return PointMatch(code, found, TITLE) if found is not None else None

    def title(self, code: str) -> str:
        """Формулировка пункта из реестра приказов."""
        if self.registry is None:
            return ""
        documents = self.doc_ids or tuple(self.registry.documents_with(code))
        for doc_id in documents:
            item = self.registry.get(doc_id, code)
            if item is not None:
                return item.title
        return ""

    def norm_quantity(self, code: str) -> str:
        """Сколько предписывает перечень: «2», «По количеству детей в группе»."""
        if self.registry is None:
            return ""
        documents = self.doc_ids or tuple(self.registry.documents_with(code))
        for doc_id in documents:
            item = self.registry.get(doc_id, code)
            if item is not None:
                return " ".join((item.quantity or "").split())
        return ""

    def _registry(self, code: str) -> list[Product]:
        for doc_id in self.doc_ids:
            found = self.index.by_norm_code(code, doc_id)
            if found:
                return found
        return self.index.by_norm_code(code)

    def _named_by_code(self) -> dict[str, list[Product]]:
        if self._named is None:
            named: dict[str, list[Product]] = {}
            for product in self.index.products:
                code = name_code(product.name) if product.is_active else ""
                if code:
                    named.setdefault(code, []).append(product)
            self._named = named
        return self._named

    def _by_title(self, code: str, wording: str) -> Product | None:
        wanted = key_stems(wording)
        if not wanted:
            return None
        query = SearchQuery(text=wording, limit=CANDIDATES, audience=self.audience)
        for hit in self.index.search(query):
            other = name_code(hit.product.name)
            if other and other != code:
                # Товар назван другим пунктом перечня — это не замена, это соседняя позиция.
                continue
            if fits(wanted, hit.product.name):
                return hit.product
        return None


def key_stems(text: str) -> list[str]:
    """Основы значимых слов: без размеров, единиц и слов-заполнителей."""
    found: list[str] = []
    for word in stems(text or ""):
        if len(word) < 3 or word.isdigit() or word in _SKIP or word in found:
            continue
        found.append(word)
    return found


def fits(wanted: list[str], name: str) -> bool:
    """Об одном ли предмете говорят формулировка пункта и название товара."""
    known = set(key_stems(name))
    if not wanted or wanted[0] not in known:
        return False
    return len(set(wanted) & known) * 2 >= min(len(wanted), ENOUGH)


def best(products: list[Product]) -> Product | None:
    """Из нескольких позиций пункта — та, что в наличии и с ценой, дальше дешевле."""
    if not products:
        return None
    return min(
        products,
        key=lambda product: (
            product.availability is not Availability.AVAILABLE,
            product.price is None,
            product.price or 0,
            product.sku_1c,
        ),
    )
