"""Обход каталога vdm.ru: дерево разделов → страницы конечных разделов → плитки.

Родительский раздел на сайте показывает товары всех своих подразделов (у корня детского
сада — 149 страниц), поэтому плитки собираются только в конечных разделах: в их блоке
«Уточнить раздел» нет более глубоких ссылок, только соседние. Весь каталог так — около
трёхсот страниц, а не тысяча.

Товар в нескольких разделах получает несколько путей: это размещения каталога, по ним же
разбор выгрузки выводит нормативную привязку.

Обход прерываемый: загруженные страницы пишутся в файл обхода, и повторный запуск
продолжает с места остановки. Файл старше `MAX_AGE_HOURS` не используется — цены в нём
устарели. Незагруженная страница — не пустой раздел: такие адреса возвращаются, и выгрузку
по неполному обходу не собирают, иначе товары пропущенных разделов ушли бы в «исчезнувшие».
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from media.extract import decode
from media.fetcher import FetchError
from site_catalog.extract import CardFacts, SectionPage, Tile, parse_card, parse_section

SAVE_EVERY = 10
# Столько страниц подряд не загрузилось — сайт недоступен, обход останавливается.
GIVE_UP_AFTER = 3
MAX_AGE_HOURS = 12


@dataclass
class SiteProduct:
    tile: Tile
    paths: list[list[str]] = field(default_factory=list)


@dataclass
class CrawlResult:
    products: dict[int, SiteProduct] = field(default_factory=dict)
    sections: int = 0
    leaves: int = 0
    pages: int = 0
    fetched: int = 0
    failed: list[str] = field(default_factory=list)
    stopped: bool = False

    @property
    def complete(self) -> bool:
        return not self.failed and not self.stopped


class SiteCrawler:
    def __init__(
        self,
        fetcher,  # noqa: ANN001 — media.fetcher.PageFetcher
        cache_path: Path | None = None,
        on_progress: Callable[[CrawlResult, str], None] | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.cache_path = cache_path
        self.on_progress = on_progress
        self.created = time.time()
        self.pages: dict[str, dict] = {}
        self.cards: dict[str, dict] = {}
        self.resumed = False
        self._load()
        self._unsaved = 0
        self._errors = 0

    # --- Разделы ------------------------------------------------------------------

    def crawl(self, roots: list[str], limit_leaves: int | None = None) -> CrawlResult:
        result = CrawlResult()
        queue = deque(roots)
        seen: set[str] = set()
        try:
            while queue and not result.stopped:
                url = queue.popleft()
                if url in seen:
                    continue
                seen.add(url)
                first = self._section(url, result)
                if first is None:
                    continue
                result.sections += 1
                if first.children:
                    queue.extend(child for child in first.children if child not in seen)
                    continue

                result.leaves += 1
                pages = [first]
                for number in range(2, first.pages + 1):
                    page = self._section(_page_url(url, number), result)
                    if result.stopped:
                        break
                    if page is not None:
                        pages.append(page)
                for page in pages:
                    for tile in page.tiles:
                        product = result.products.setdefault(tile.bitrix_id, SiteProduct(tile))
                        if first.path and first.path not in product.paths:
                            product.paths.append(list(first.path))
                if limit_leaves and result.leaves >= limit_leaves:
                    break
        finally:
            self.save()
        return result

    # --- Карточки новых товаров ----------------------------------------------------

    def fetch_cards(
        self,
        products: list[SiteProduct],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[dict[int, CardFacts], list[str]]:
        cards: dict[int, CardFacts] = {}
        failed: list[str] = []
        try:
            for number, product in enumerate(products, 1):
                key = str(product.tile.bitrix_id)
                if key not in self.cards:
                    try:
                        fetched = self.fetcher.get(product.tile.url)
                    except FetchError:
                        failed.append(product.tile.url)
                        self._errors += 1
                        if self._errors >= GIVE_UP_AFTER:
                            break
                        continue
                    self._errors = 0
                    if fetched.body is None:
                        failed.append(product.tile.url)
                        continue
                    self.cards[key] = asdict(parse_card(decode(fetched.body), product.tile.url))
                    self._mark_unsaved()
                cards[product.tile.bitrix_id] = CardFacts(**self.cards[key])
                if on_progress is not None:
                    on_progress(number, len(products))
        finally:
            self.save()
        return cards, failed

    # --- Файл обхода ---------------------------------------------------------------

    def save(self) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        data = {"created": self.created, "pages": self.pages, "cards": self.cards}
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.cache_path)
        self._unsaved = 0

    def _load(self) -> None:
        if self.cache_path is None or not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        created = float(data.get("created") or 0)
        if time.time() - created > MAX_AGE_HOURS * 3600:
            return
        self.created = created
        self.pages = dict(data.get("pages") or {})
        self.cards = dict(data.get("cards") or {})
        self.resumed = bool(self.pages)

    def _section(self, url: str, result: CrawlResult) -> SectionPage | None:
        cached = self.pages.get(url)
        if cached is not None:
            result.pages += 1
            return _from_cache(url, cached)
        try:
            fetched = self.fetcher.get(url)
        except FetchError:
            result.failed.append(url)
            self._errors += 1
            result.stopped = self._errors >= GIVE_UP_AFTER
            return None
        self._errors = 0
        if fetched.body is None:
            result.failed.append(url)
            return None
        page = parse_section(decode(fetched.body), url)
        result.pages += 1
        result.fetched += 1
        self.pages[url] = _to_cache(page)
        self._mark_unsaved()
        if self.on_progress is not None:
            self.on_progress(result, url)
        return page

    def _mark_unsaved(self) -> None:
        self._unsaved += 1
        if self._unsaved >= SAVE_EVERY:
            self.save()


def _page_url(url: str, number: int) -> str:
    return f"{url}{'&' if '?' in url else '?'}PAGEN_1={number}"


def _to_cache(page: SectionPage) -> dict:
    return {
        "path": page.path,
        "children": page.children,
        "pages": page.pages,
        "tiles": [asdict(tile) for tile in page.tiles],
    }


def _from_cache(url: str, raw: dict) -> SectionPage:
    return SectionPage(
        url=url,
        path=list(raw["path"]),
        children=list(raw["children"]),
        pages=int(raw["pages"]),
        tiles=[Tile(**tile) for tile in raw["tiles"]],
    )
