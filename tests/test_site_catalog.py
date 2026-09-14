"""Каталог с сайта vdm.ru вместо выгрузки 1С (14.09: «1С выгрузки нам не дадут»).

Разметка — синтетическая, но с теми же якорями, что на живых страницах 14.09: хлебные
крошки schema.org, блок «Уточнить раздел», плитки `item product sku` с
`data-max-quantity`, пагинация `PAGEN_1` и блок рекомендаций после списка.
"""

from __future__ import annotations

from pathlib import Path

from catalog_import.parser import parse_products, read_bitrix_ids
from ingest.xlsx_reader import XlsxFile
from media.fetcher import FetchError, FetchResult
from site_catalog.crawl import SiteCrawler
from site_catalog.export import Known, build_book
from site_catalog.extract import CardFacts, parse_card, parse_section

BASE = "https://vdm.ru/catalog/sad/"


def crumbs(*titles: str) -> str:
    items = ["Главная страница", "Каталог товаров", *titles]
    return "".join(
        f'<li itemprop="itemListElement" itemscope><a href="#" itemprop="item">'
        f'<span itemprop="name">{title}</span><meta itemprop="position" content="{n}"></a></li>'
        for n, title in enumerate(items, 1)
    )


def tile(bitrix_id: int, name: str, url: str, price: str, quantity: int) -> str:
    label = "inStock" if quantity else "onOrder"
    return (
        f'<div class="item product sku" id="bx_1_{bitrix_id}" data-product-id="{bitrix_id}">'
        f'<a href="{url}" class="picture"><img src="/upload/1.jpg"></a>'
        f'<a href="{url}" class="name"><span class="middle">{name}</span></a>'
        f'<a class="price">{price} &#8381; <span class="measure"> / шт</span><s class="discount"></s></a>'
        f'<input type="text" class="quantity" value="1" data-max-quantity="{quantity}">'
        f'<a class="{label} label">x</a></div>'
    )


def section(path: list[str], children: list[str] = (), tiles: str = "", pages: int = 1) -> str:
    links = "".join(f'<li><a href="{href}"><span>раздел</span></a><a href="{href}">12</a></li>' for href in children)
    pager = "".join(f'<li><a href="?PAGEN_1={n}">{n}</a></li>' for n in range(2, pages + 1))
    return (
        f"<ul>{crumbs(*path)}</ul><h1>{path[-1]}</h1>"
        f'<div id="nextSection"><ul>{links}</ul></div>'
        f'<div id="catalogSection"><div class="items productList">{tiles}</div></div>'
        f'<div class="bx-pagination"><ul>{pager}</ul></div>'
        f'<div id="bigdata_recommended">{tile(999, "Чужой товар", "/catalog/x.html", "1", 1)}</div>'
    )


def test_section_page_gives_path_children_pages_and_tiles():
    page = section(
        ["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА", "01. Образовательные комплекты"],
        children=[f"{BASE}01/01_01/", f"{BASE}02/"],
        tiles=tile(49220, "EKUD 0870 Набор &quot;Детям о Победе&quot;", "/catalog/sad/01/ekud.html", "7&nbsp;679", 3)
        + tile(75958, "Геоборд (набор из 8 шт)", "/catalog/sad/01/geobord.html", "4&nbsp;200", 0),
        pages=11,
    )
    parsed = parse_section(page, f"{BASE}01/")

    assert parsed.path == ["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА", "01. Образовательные комплекты"]
    assert parsed.children == [f"{BASE}01/01_01/"], "соседний раздел — не подраздел"
    assert parsed.pages == 11
    assert [(t.bitrix_id, t.price, t.quantity) for t in parsed.tiles] == [(49220, 7679, 3), (75958, 4200, 0)]
    assert parsed.tiles[0].name == 'EKUD 0870 Набор "Детям о Победе"'
    assert parsed.tiles[0].url == "https://vdm.ru/catalog/sad/01/ekud.html"
    assert 999 not in {t.bitrix_id for t in parsed.tiles}, "плитки блока рекомендаций в раздел не попадают"


def test_card_gives_code_description_and_price():
    page = (
        '<h1 class="changeName">Геоборд (набор из 8 шт)</h1>'
        '<meta itemprop="price" content="4200" /><input data-max-quantity="0">'
        '<div class="changeDescription">Набор <div class="note">для геометрии</div> и логики.</div></div>'
        '<div class="propertyList"><div class="propertyTable"><div class="propertyName">Код</div>'
        '<div class="propertyValue"> 0Э-00007103 </div></div></div>'
    )
    card = parse_card(page, "https://vdm.ru/catalog/sad/geobord.html")

    assert card.sku == "0Э-00007103" and card.price == 4200 and card.quantity == 0
    assert card.name == "Геоборд (набор из 8 шт)"
    assert card.description_html == 'Набор <div class="note">для геометрии</div> и логики.'


class Site:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requests: list[str] = []

    def get(self, url: str, etag=None, last_modified=None) -> FetchResult:  # noqa: ANN001
        self.requests.append(url)
        if url not in self.pages:
            raise FetchError(f"{url}: 404")
        return FetchResult(body=self.pages[url].encode("utf-8"), status=200)


def catalog_site() -> Site:
    root, first, second = BASE, f"{BASE}01/", f"{BASE}02/"
    ball = tile(1, "Мяч", "/catalog/sad/01/ball.html", "908", 4)
    return Site(
        {
            root: section(["САД"], children=[first, second], tiles=ball),
            first: section(["САД", "01. Спорт"], children=[second], tiles=ball, pages=2),
            f"{first}?PAGEN_1=2": section(["САД", "01. Спорт"], tiles=tile(2, "Мат", "/catalog/sad/01/mat.html", "2 500", 0)),
            second: section(["САД", "02. Игры"], children=[first], tiles=ball + tile(3, "Кубики", "/catalog/sad/02/cubes.html", "300", 7)),
        }
    )


def test_crawler_collects_leaves_with_every_page_and_every_placement(tmp_path):
    site = catalog_site()
    result = SiteCrawler(site, tmp_path / "crawl.json").crawl([BASE])

    assert result.complete and result.sections == 3 and result.leaves == 2
    assert sorted(result.products) == [1, 2, 3]
    assert result.products[1].paths == [["САД", "01. Спорт"], ["САД", "02. Игры"]]
    assert result.products[2].tile.price == 2500 and result.products[2].tile.quantity == 0
    assert site.requests.count(BASE) == 1, "родительский раздел не листается: его товары есть в листьях"


def test_crawler_resumes_from_its_file_and_reports_what_failed(tmp_path):
    cache = tmp_path / "crawl.json"
    SiteCrawler(catalog_site(), cache).crawl([BASE])

    offline = Site({})
    again = SiteCrawler(offline, cache).crawl([BASE])
    assert again.complete and sorted(again.products) == [1, 2, 3] and offline.requests == []

    broken = catalog_site()
    del broken.pages[f"{BASE}01/?PAGEN_1=2"]
    partial = SiteCrawler(broken, tmp_path / "other.json").crawl([BASE])
    assert not partial.complete and partial.failed == [f"{BASE}01/?PAGEN_1=2"]


def test_book_goes_through_the_1c_import_parser(tmp_path):
    result = SiteCrawler(catalog_site()).crawl([BASE])
    known = {
        1: Known("B1", "Мяч для игр.", ("мяч — 1 шт.", "насос — 1 шт."), "https://vdm.ru/s/b1"),
        2: Known("M2", "Мат гимнастический."),
    }
    cards = {3: CardFacts(sku=None, name="Кубики", description_html="", price=300, quantity=7)}
    book, report = build_book(result, known, cards)
    assert report.known == 2 and report.new == 0 and report.without_code == ["https://vdm.ru/catalog/sad/02/cubes.html"]

    path = Path(tmp_path / "site.xlsx")
    path.write_bytes(book)
    with XlsxFile(path) as workbook:
        bitrix = read_bitrix_ids(list(workbook.numbered_rows(1)))
        sheet = parse_products(list(workbook.numbered_rows(0)), bitrix_ids=bitrix.by_name, now="t", source_name="site")

    assert sheet.header_error is None
    products = {product.sku_1c: product for product in sheet.products}
    assert set(products) == {"B1", "M2"}
    ball = products["B1"]
    assert (ball.price, ball.in_stock, ball.bitrix_id, ball.short_url) == (908, 4, 1, "https://vdm.ru/s/b1")
    assert ball.category_paths == [["САД", "01. Спорт"], ["САД", "02. Игры"]]
    assert ball.kit_contents and ball.description.startswith("Мяч для игр")
    assert products["M2"].in_stock == 0, "«под заказ» — ноль, а не «нет данных»"

    cards[3] = CardFacts(sku="K3", name="Кубики", description_html="<p>Кубики</p>", price=300, quantity=7)
    _, report = build_book(result, known, cards)
    assert report.new == 1 and not report.without_code
