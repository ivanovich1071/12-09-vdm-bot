"""Разбор выгрузки каталога — одна реализация для импорта и для сборки базы знаний.

Цикл по строкам жил в `ingest/build_kb.py` и о номерах строк не сообщал. Теперь
он здесь, а `build_kb` его вызывает (D9, решение B): при изменении формата
выгрузки разбор правится в одном месте. Парсер только собирает факты — что
считать ошибкой, решает `validator.py`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from catalog.text import normalize_name
from ingest.catalog_tree import CatalogPath
from ingest.html_text import html_to_text, split_kit_contents
from norms import documents as norm_docs
from norms.extract import NormLink, code_anomalies, extract, root_document_id

Row = dict[str, str]
NumberedRow = tuple[int, Row]

# Колонки листа с товарами. Заголовки проверяем при загрузке — если выгрузка изменится,
# лучше упасть с внятной ошибкой, чем молча собрать пустую базу.
EXPECTED_HEADERS = {
    "A": "Код в 1с8",
    "B": "Наименование",
    "C": "URL страницы детального просмотра",
    "D": "Розничная цена",
    "E": "Доступное количество",
    "F": "Короткая ссылка",
    "G": "Описание",
}


@dataclass
class ParsedProduct:
    """Товар в том виде, в каком он пишется в базу знаний и в товары импорта."""

    sku_1c: str
    name: str
    url: str | None
    short_url: str | None
    price: int | None
    currency: str
    # Пустая или нечитаемая ячейка — `None`, а не 0: «нет данных» и «нет в
    # наличии» — разные ответы покупателю (D8, решение E).
    in_stock: int | None
    # Один товар размещён сразу в нескольких разделах: например, мяч лежит и в спортивном
    # инвентаре детского сада, и в пункте приказа 838 по спортивному комплексу. Все
    # размещения нужны: по ним работают и навигация, и нормативная привязка.
    category_paths: list[list[str]]
    description: str
    kit_contents: list[str]
    norms: list[dict[str, Any]]
    bitrix_id: int | None
    images: list[str] = field(default_factory=list)
    # Страна, сертификат — со страницы товара. В выгрузке 1С этих полей нет.
    attributes: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    updated_at: str = ""


@dataclass(frozen=True)
class RowFact:
    """Что было в строке товара — для проверок и сообщений с номером строки."""

    row_number: int
    code: str
    name: str
    price: str
    stock: str
    has_section: bool


@dataclass(frozen=True)
class HeadingFact:
    row_number: int
    title: str


@dataclass
class ParsedSheet:
    header: Row = field(default_factory=dict)
    header_error: str | None = None
    products: list[ParsedProduct] = field(default_factory=list)
    # Непустых строк на листе, включая заголовок.
    rows_total: int = 0
    headings: list[HeadingFact] = field(default_factory=list)
    product_rows: list[RowFact] = field(default_factory=list)
    # Номера строк товара: ключ — код 1С, а у строки без кода — «название|адрес».
    rows_by_key: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    unnumbered_roots: set[str] = field(default_factory=set)
    norm_anomalies: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BitrixIds:
    by_name: dict[str, int]
    # Наименование, за которым несколько ID: связь с сайтом не ставим.
    # Значение — пары «номер строки, ID».
    ambiguous: dict[str, list[tuple[int, int]]]


def parse_products(
    rows: Iterable[NumberedRow],
    *,
    bitrix_ids: dict[str, int],
    now: str,
    source_name: str,
) -> ParsedSheet:
    """Лист с товарами: разделы, товары и их размещения.

    Строка с кодом и без наименования — раздел каталога, с наименованием — товар.
    Повтор кода добавляет товару размещение, а не создаёт второй товар.
    """
    iterator = iter(rows)
    first = next(iterator, None)
    sheet = ParsedSheet(header=first[1] if first else {}, rows_total=1 if first else 0)
    sheet.header_error = header_error(sheet.header)
    if sheet.header_error:
        sheet.rows_total += sum(1 for _ in iterator)
        return sheet

    path = CatalogPath()
    products: dict[str, ParsedProduct] = {}
    for number, row in iterator:
        sheet.rows_total += 1
        code = row.get("A", "").strip()
        name = row.get("B", "").strip()

        if code and not name:
            heading = path.push(code)
            sheet.headings.append(HeadingFact(number, code))
            if heading.kind == "numbered" and root_document_id(path.root) is None and path.root:
                sheet.unnumbered_roots.add(path.root)
            continue
        if not name:
            continue

        sheet.product_rows.append(
            RowFact(
                row_number=number,
                code=code,
                name=name,
                price=row.get("D", "").strip(),
                stock=row.get("E", "").strip(),
                has_section=bool(path.titles),
            )
        )
        text = html_to_text(row.get("G", ""))
        description, kit = split_kit_contents(text)
        url = row.get("C", "").strip() or None
        links = extract(path=path, url=url, description=text)
        key = code or f"{normalize_name(name)}|{url or ''}"
        sheet.rows_by_key[key].append(number)
        sheet.norm_anomalies.extend(f"{code}: {c}" for c in code_anomalies(path, links))

        existing = products.get(key)
        if existing is not None:
            merge_placement(existing, path.titles, links)
            continue

        products[key] = ParsedProduct(
            sku_1c=code,
            name=name,
            url=url,
            short_url=row.get("F", "").strip() or None,
            price=to_int(row.get("D")),
            currency="RUB",
            in_stock=to_int(row.get("E")),
            category_paths=[path.titles] if path.titles else [],
            description=description,
            kit_contents=kit,
            norms=[link_to_dict(link) for link in links],
            bitrix_id=bitrix_ids.get(normalize_name(name)),
            sources={"catalog": source_name},
            updated_at=now,
        )

    sheet.products = list(products.values())
    return sheet


def read_bitrix_ids(rows: Iterable[NumberedRow]) -> BitrixIds:
    """Второй лист выгрузки: ID элемента Битрикса и его наименование.

    Ключ связи с сайтом. Неоднозначные названия отбрасываем — лучше пустой ID,
    чем ссылка на чужой товар.
    """
    found: dict[str, list[tuple[int, int]]] = defaultdict(list)
    iterator = iter(rows)
    next(iterator, None)
    for number, row in iterator:
        raw_id, name = row.get("A", "").strip(), row.get("B", "").strip()
        if not raw_id.isdigit() or not name:
            continue
        found[normalize_name(name)].append((number, int(raw_id)))

    by_name: dict[str, int] = {}
    ambiguous: dict[str, list[tuple[int, int]]] = {}
    for name, entries in found.items():
        ids = {bitrix_id for _, bitrix_id in entries}
        if len(ids) == 1:
            by_name[name] = ids.pop()
        else:
            ambiguous[name] = entries
    return BitrixIds(by_name, ambiguous)


def header_error(header: Row) -> str | None:
    missing = {
        col: expected
        for col, expected in EXPECTED_HEADERS.items()
        if normalize_name(header.get(col, "")) != normalize_name(expected)
    }
    if not missing:
        return None
    got = {col: header.get(col, "") for col in EXPECTED_HEADERS}
    return (
        "Структура выгрузки изменилась. Ожидались колонки "
        f"{EXPECTED_HEADERS}, получены {got}. Загрузка остановлена."
    )


def merge_placement(product: ParsedProduct, titles: list[str], links: list[NormLink]) -> None:
    """Добавляет товару ещё одно размещение в каталоге и его нормативные основания."""
    if titles and titles not in product.category_paths:
        product.category_paths.append(titles)
    known = {(n["doc_id"], n["item_code"]): n for n in product.norms}
    for link in links:
        key = (link.doc_id, link.item_code)
        current = known.get(key)
        if current is None:
            product.norms.append(link_to_dict(link))
            known[key] = product.norms[-1]
        elif link.confidence > current["confidence"]:
            current.update(link_to_dict(link))
    product.norms.sort(key=lambda n: (-n["confidence"], n["doc_id"], n["item_code"] or ""))


def link_to_dict(link: NormLink) -> dict[str, Any]:
    doc = norm_docs.get(link.doc_id)
    return {
        "doc_id": link.doc_id,
        "doc_citation": doc.citation,
        "item_code": link.item_code,
        "item_title": link.item_title,
        "source": link.source,
        "confidence": link.confidence,
    }


def to_int(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(round(float(raw.replace(",", ".").replace("\xa0", "").replace(" ", ""))))
    except ValueError:
        return None
