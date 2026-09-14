"""Каталог с сайта — книгой в формате выгрузки 1С.

Импорт, diff, пороги безопасности, версии каталога и откат уже построены вокруг выгрузки
1С (EPIC 2–4). Поэтому обход сайта каталог сам не пишет, а собирает такую же книгу:
заголовок A–G, строки разделов и строки товаров, второй лист — ID Битрикса. Дальше — обычные
`import-1c --file` и `catalog approve`: сайт проходит те же проверки, что прошла бы
выгрузка, и нормативная привязка выводится тем же разбором.

Цена, остаток, название, адрес и размещения — с сайта. Код 1С, описание и короткую ссылку
известного товара берём из текущего каталога: на плитке их нет. У нового товара код и
описание — со страницы товара. Новый товар без кода 1С в книгу не попадает: ключ каталога
выдумывать нельзя.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from catalog_import.parser import EXPECTED_HEADERS
from documents.xlsx import BOLD, PLAIN, write_sheets
from site_catalog.crawl import CrawlResult
from site_catalog.extract import CardFacts

_COLUMNS = sorted(EXPECTED_HEADERS)
_WIDTHS = [16.0, 60.0, 50.0, 14.0, 12.0, 24.0, 60.0]
# Управляющие символы XML не допускает: одна такая буква в описании портит всю книгу.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_KIT_HEADER = "Состав комплекта:"


@dataclass(frozen=True)
class Known:
    """Что берём из текущего каталога для товара, который там уже есть."""

    sku: str
    description: str
    kit_contents: tuple[str, ...] = ()
    short_url: str | None = None


@dataclass
class ExportReport:
    products: int = 0
    known: int = 0
    new: int = 0
    sections: int = 0
    # Адреса новых товаров, у которых на странице не нашлось кода 1С.
    without_code: list[str] = field(default_factory=list)


def build_book(
    result: CrawlResult, known: dict[int, Known], cards: dict[int, CardFacts]
) -> tuple[bytes, ExportReport]:
    report = ExportReport()
    codes: dict[int, str] = {}
    for bitrix_id, product in result.products.items():
        if bitrix_id in known:
            codes[bitrix_id] = known[bitrix_id].sku
            report.known += 1
        elif bitrix_id in cards and cards[bitrix_id].sku:
            codes[bitrix_id] = cards[bitrix_id].sku or ""
            report.new += 1
        else:
            report.without_code.append(product.tile.url)
    report.products = len(codes)

    placements: dict[tuple[str, ...], list[int]] = {}
    for bitrix_id, product in result.products.items():
        if bitrix_id not in codes:
            continue
        for path in product.paths:
            placements.setdefault(tuple(path), []).append(bitrix_id)

    rows: list[list[tuple]] = [[(EXPECTED_HEADERS[column], BOLD) for column in _COLUMNS]]
    for path, ids in placements.items():
        report.sections += 1
        rows.extend([(title, PLAIN)] for title in path)
        for bitrix_id in ids:
            tile = result.products[bitrix_id].tile
            source = known.get(bitrix_id)
            card = cards.get(bitrix_id)
            description = _known_text(source) if source else (card.description_html if card else "")
            rows.append(
                [
                    (codes[bitrix_id], PLAIN),
                    (_clean(tile.name), PLAIN),
                    (tile.url, PLAIN),
                    (tile.price, PLAIN),
                    (tile.quantity, PLAIN),
                    (source.short_url if source else None, PLAIN),
                    (_clean(description), PLAIN),
                ]
            )

    ids_sheet: list[list[tuple]] = [[("ID", BOLD), ("Наименование", BOLD)]]
    ids_sheet.extend(
        [(bitrix_id, PLAIN), (_clean(result.products[bitrix_id].tile.name), PLAIN)] for bitrix_id in codes
    )
    book = write_sheets([("Каталог", rows, _WIDTHS), ("ID Битрикса", ids_sheet, [12.0, 60.0])])
    return book, report


def _known_text(known: Known) -> str:
    """Описание в том виде, из которого разбор выгрузки снова отделит состав комплекта."""
    if not known.kit_contents:
        return known.description
    return "\n".join([known.description, "", _KIT_HEADER, *known.kit_contents]).strip()


def _clean(text: str | None) -> str:
    return _CONTROL.sub("", text or "")
