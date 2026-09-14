"""Страницы каталога vdm.ru: раздел, плитки товаров, карточка.

Выгрузок 1С больше не будет (заказчик, 14.09) — каталог собирается с сайта. Со страницы
раздела берутся путь в каталоге (хлебные крошки), подразделы, число страниц и плитки
товаров: ID Битрикса, название, адрес, цена и остаток. Остаток — `data-max-quantity` поля
количества: у товара «в наличии» там число, у «под заказ» — 0. Сверено 14.09 на EKUD 0870:
на сайте 3, в выгрузке 1С от 26.08 — тоже 3.

Карточка товара нужна только новым позициям — ради кода 1С (характеристика «Код») и
описания. У известных они уже есть в каталоге.

Якоря — данные компонентов Битрикса (`data-product-id`, `itemprop`, `nextSection`), а не
оформление. Пропал якорь — пустой результат, а не чужие данные.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin, urlparse

from media.extract import extract_attributes, extract_from_card

_CRUMB = re.compile(r'itemprop="itemListElement".*?itemprop="name"[^>]*>([^<]*)<', re.S)
# Первые крошки — «Главная страница» и «Каталог товаров»: в путь каталога они не входят.
_SERVICE_CRUMBS = frozenset({"главная страница", "каталог товаров"})
_NEXT_SECTION = re.compile(r'<div id="nextSection">(.*?)</ul>', re.S)
_HREF = re.compile(r'href="([^"#]+)"')
_PAGE_NUMBER = re.compile(r"[?&](?:amp;)?PAGEN_1=(\d+)")
_LIST_START = 'id="catalogSection"'
# Где кончается список раздела: дальше пагинация, вкладки подвала и «рекомендуем». У блока
# рекомендаций те же плитки, но товары в нём чужие — в раздел они попасть не должны.
_LIST_END = re.compile(r'bx-pagination|<div id="footerTabs|<div id="bigdata')
_TILE = re.compile(r'<div class="item product sku"')
_BITRIX_ID = re.compile(r'data-product-id="(\d+)"')
_NAME = re.compile(r'<a href="([^"]+)"[^>]*class="name"[^>]*>\s*<span class="middle">(.*?)</span>', re.S)
_PRICE = re.compile(r'<a class="price[^"]*">(.*?)(?:<span class="measure"|</a>)', re.S)
_QUANTITY = re.compile(r'data-max-quantity="(\d+)"')
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_OFFER_PRICE = re.compile(r'itemprop="price"\s+content="([\d.]+)"')
_DESCRIPTION = '<div class="changeDescription">'
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
_DIV = re.compile(r"<div\b|</div>", re.I)
_LEADING_NUMBER = re.compile(r"\d+")


@dataclass(frozen=True)
class Tile:
    """Товар на странице раздела."""

    bitrix_id: int
    name: str
    url: str
    price: int | None
    quantity: int | None


@dataclass
class SectionPage:
    url: str
    path: list[str]
    # Более глубокие разделы. Соседей, которых сайт показывает в том же блоке, здесь нет.
    children: list[str] = field(default_factory=list)
    pages: int = 1
    tiles: list[Tile] = field(default_factory=list)


@dataclass
class CardFacts:
    """Со страницы товара — то, чего нет на плитке."""

    sku: str | None
    name: str
    description_html: str
    price: int | None
    quantity: int | None
    images: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)


def parse_section(page: str, url: str) -> SectionPage:
    return SectionPage(
        url=url,
        path=breadcrumbs(page),
        children=_children(page, url),
        pages=max((int(number) for number in _PAGE_NUMBER.findall(page)), default=1),
        tiles=_tiles(page, url),
    )


def parse_card(page: str, url: str) -> CardFacts:
    attributes = extract_attributes(page)
    name = _H1.search(page)
    price = _OFFER_PRICE.search(page)
    quantity = _QUANTITY.search(page)
    return CardFacts(
        sku=(attributes.get("Код") or "").strip() or None,
        name=_plain(name.group(1)) if name else "",
        description_html=_inner_div(page, _DESCRIPTION),
        price=int(float(price.group(1))) if price else None,
        quantity=int(quantity.group(1)) if quantity else None,
        images=extract_from_card(page, url).images,
        attributes=attributes,
    )


def breadcrumbs(page: str) -> list[str]:
    names = [_plain(name) for name in _CRUMB.findall(page)]
    return [name for name in names if name and name.lower() not in _SERVICE_CRUMBS]


# --- Внутреннее ---------------------------------------------------------------


def _children(page: str, url: str) -> list[str]:
    block = _NEXT_SECTION.search(page)
    if not block:
        return []
    here = urlparse(url).path
    found: list[str] = []
    for href in _HREF.findall(block.group(1)):
        target = urljoin(url, unescape(href))
        path = urlparse(target).path
        if path.startswith(here) and len(path) > len(here) and target not in found:
            found.append(target)
    return found


def _tiles(page: str, url: str) -> list[Tile]:
    start = page.find(_LIST_START)
    if start < 0:
        return []
    end = _LIST_END.search(page, start)
    listing = page[start : end.start() if end else len(page)]
    starts = [match.start() for match in _TILE.finditer(listing)]
    tiles: list[Tile] = []
    seen: set[int] = set()
    for index, begin in enumerate(starts):
        chunk = listing[begin : starts[index + 1] if index + 1 < len(starts) else len(listing)]
        tile = _tile(chunk, url)
        if tile is not None and tile.bitrix_id not in seen:
            seen.add(tile.bitrix_id)
            tiles.append(tile)
    return tiles


def _tile(chunk: str, base_url: str) -> Tile | None:
    bitrix_id = _BITRIX_ID.search(chunk)
    name = _NAME.search(chunk)
    if not bitrix_id or not name:
        return None
    price = _PRICE.search(chunk)
    quantity = _QUANTITY.search(chunk)
    return Tile(
        bitrix_id=int(bitrix_id.group(1)),
        name=_plain(name.group(2)),
        url=urljoin(base_url, unescape(name.group(1))),
        price=_money(price.group(1)) if price else None,
        quantity=int(quantity.group(1)) if quantity else None,
    )


def _money(raw: str) -> int | None:
    """«7&nbsp;679 &#8381;» → 7679. «Цена по запросу» — не цена."""
    text = re.sub(r"[\s\xa0]", "", unescape(raw).split("₽")[0])
    match = _LEADING_NUMBER.match(text)
    return int(match.group(0)) if match else None


def _inner_div(page: str, marker: str) -> str:
    """Содержимое блока до его закрывающего `</div>` — с учётом вложенных блоков."""
    start = page.find(marker)
    if start < 0:
        return ""
    body = start + len(marker)
    depth = 1
    for match in _DIV.finditer(page, body):
        depth += 1 if match.group(0).lower().startswith("<div") else -1
        if depth == 0:
            return page[body : match.start()].strip()
    return ""


def _plain(html: str) -> str:
    return _SPACES.sub(" ", unescape(_TAGS.sub(" ", html))).strip()
