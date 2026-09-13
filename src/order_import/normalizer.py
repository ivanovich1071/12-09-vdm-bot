"""Нормализация строк заказа: колонки по заголовку, значения в единый вид.

Заголовок ищется в первых строках таблицы: перед ним в файлах клиентов обычно
шапка — название организации, дата, номер закупки. Колонка распознаётся по
точному названию после чистки знаков, а не по вхождению слова: «Источник
количества» — не количество, «Основание подбора» — не пункт перечня.

Исходное значение каждой колонки хранится рядом с нормализованным, а вся строка —
ячейками. Отсутствующее не выдумывается: нет количества — `None` и отметка.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from catalog.matcher import canonical_name
from core.errors import Notice
from norms.extract import document_ids_in_text
from norms.selector import document_ids, point_code
from order_import.models import UploadedOrderItem
from order_import.parsers import RawDocument, RawRow, RawTable

HEADER_SCAN_ROWS = 40

FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern)
    for name, pattern in {
        "line_no": r"№|n|no|№ п/п|п/п|номер|№ строки",
        "article": r"код(?: 1с| товара| по 1с| номенклатуры)?|артикул(?: поставщика| товара)?|арт|sku",
        "name": r"наименование(?: товара| позиции| оборудования| товара работы услуги)?|название(?: товара)?|товар|позиция|предмет закупки",
        "manufacturer": r"производитель|бренд|изготовитель|марка",
        "characteristics": r"(?:технические )?характеристик[аи]|описание|комплектация",
        "dimensions": r"размеры?|габариты|габаритные размеры",
        "quantity": r"кол-?во(?: шт)?|количество(?: шт)?|кол",
        "unit": r"ед|ед изм|единица(?: измерения)?",
        "price": r"цена(?: за (?:ед|единицу|шт))?(?: с ндс)?|стоимость (?:за )?(?:ед|единицы|единицу)",
        "total": r"сумма(?: с ндс)?|стоимость|итого|всего",
        "norm_document": r"документ|нормативный документ|приказ",
        "norm_item": r"пункт(?: перечня| приказа)?|позиция перечня|норматив|основание",
    }.items()
}

_HEADER_NOISE = re.compile(r"[₽,.:;()\"«»]|\bруб\b")
_TOTAL_ROW = re.compile(r"^\s*(?:итого|всего)\b", re.IGNORECASE)
_QUANTITY_NOISE = re.compile(r"(?:шт|штук\w*|компл\w*|ед)\.?", re.IGNORECASE)
_MONEY_NOISE = re.compile(r"[^\d,.\-]")
_DIMENSIONS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)(?:\s*[xх×*]\s*(\d+(?:[.,]\d+)?))?",
    re.IGNORECASE,
)
_EXCEL_INTEGER = re.compile(r"^(\d+)\.0+$")
_DASHES = frozenset({"-", "–", "—"})


@dataclass(frozen=True)
class ColumnMap:
    header_index: int
    columns: dict[str, int]


def header_field(cell: str) -> str | None:
    key = " ".join(_HEADER_NOISE.sub(" ", cell.lower().replace("ё", "е")).split())
    for field_name, pattern in FIELD_PATTERNS.items():
        if pattern.fullmatch(key):
            return field_name
    return None


def detect_columns(table: RawTable) -> ColumnMap | None:
    """Строка заголовка — с наибольшим числом узнанных колонок, среди них имя или артикул."""
    best: ColumnMap | None = None
    for index, row in enumerate(table.rows[:HEADER_SCAN_ROWS]):
        columns: dict[str, int] = {}
        for position, cell in enumerate(row.cells):
            found = header_field(cell)
            if found and found not in columns:
                columns[found] = position
        if len(columns) < 2 or not ({"name", "article"} & columns.keys()):
            continue
        if best is None or len(columns) > len(best.columns):
            best = ColumnMap(index, columns)
    return best


class OrderNormalizer:
    def normalize(self, document: RawDocument) -> tuple[list[UploadedOrderItem], list[Notice]]:
        items: list[UploadedOrderItem] = []
        warnings = list(document.warnings)
        headers = 0
        for table_number, table in enumerate(document.tables, 1):
            mapping = detect_columns(table)
            if mapping is None:
                continue
            headers += 1
            for row in table.rows[mapping.header_index + 1 :]:
                item = self._item(len(items) + 1, table_number, row, mapping)
                if item is not None:
                    items.append(item)
        if headers == 0 and document.tables and not warnings:
            warnings.append(
                Notice(
                    "HEADER_NOT_FOUND",
                    "Не найдена строка заголовков: нужна колонка «Наименование» или «Артикул» "
                    "и хотя бы ещё одна — «Количество», «Цена».",
                )
            )
        return items, warnings

    def _item(self, line_no: int, table: int, row: RawRow, mapping: ColumnMap) -> UploadedOrderItem | None:
        raw = {
            field_name: row.cells[position].strip() if position < len(row.cells) else ""
            for field_name, position in mapping.columns.items()
        }
        # Прочерк вместо значения — пустая ячейка: так таблицы PDF держат колонки.
        raw = {name: "" if value in _DASHES else value for name, value in raw.items()}
        name = " ".join(raw.get("name", "").split()) or None
        article = _article(raw.get("article", ""))
        if not name and not article:
            return None
        if _TOTAL_ROW.match(name or "") or (not name and _TOTAL_ROW.match(article or "")):
            return None

        issues: list[str] = []
        quantity_raw = raw.get("quantity", "")
        quantity = parse_quantity(quantity_raw)
        if not quantity_raw:
            issues.append("QUANTITY_MISSING")
        elif quantity is None:
            issues.append("QUANTITY_INVALID")

        price = parse_money(raw.get("price", ""))
        if raw.get("price") and price is None:
            issues.append("PRICE_INVALID")
        total = parse_money(raw.get("total", ""))
        if price is None and total is not None and quantity:
            price = round(total / quantity)
            issues.append("PRICE_FROM_TOTAL")

        norm_text = raw.get("norm_item", "")
        documents = document_ids(raw.get("norm_document")) or document_ids_in_text(norm_text)
        return UploadedOrderItem(
            line_no=line_no,
            source_line=row.number,
            source_table=table,
            raw=raw,
            cells=row.cells,
            article=article,
            name=name,
            name_canonical=canonical_name(name) if name else None,
            manufacturer=" ".join(raw.get("manufacturer", "").split()) or None,
            characteristics=" ".join(raw.get("characteristics", "").split()) or None,
            dimensions=dimensions(raw.get("dimensions", "")) or dimensions(name or ""),
            quantity=quantity,
            unit=raw.get("unit") or None,
            price=price,
            total=total,
            norm_document=documents[0] if documents else None,
            norm_item=point_code(norm_text),
            issues=tuple(issues),
        )


def parse_quantity(value: str | None) -> int | None:
    text = _QUANTITY_NOISE.sub("", (value or "").replace("\xa0", " ")).replace(" ", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() and number > 0 else None


def parse_money(value: str | None) -> int | None:
    """«12 500,00 ₽», «12500.5», «12.500,00» → рубли целым. Копейки округляются, как в каталоге."""
    text = _MONEY_NOISE.sub("", (value or "").replace("\xa0", ""))
    if not text or text.startswith("-"):
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
        if text.count(".") > 1:
            head, _, tail = text.rpartition(".")
            text = head.replace(".", "") + ("." + tail if len(tail) != 3 else tail)
    try:
        return round(float(text))
    except ValueError:
        return None


def dimensions(text: str) -> str | None:
    match = _DIMENSIONS.search(text or "")
    if match is None:
        return None
    return "x".join(part.replace(",", ".") for part in match.groups() if part)


def _article(value: str) -> str | None:
    text = " ".join(value.split())
    if match := _EXCEL_INTEGER.match(text):
        text = match.group(1)
    return text or None
