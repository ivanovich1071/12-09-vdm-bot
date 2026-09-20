"""Фотографии товаров: когда брать, где хранить, что делать при неудаче.

Две стратегии, обе нужны.

**Лениво.** Фото подтягивается после первого показа карточки и кладётся в кэш.
Ходить за 5 936 изображениями заранее незачем: показывают единицы, а нагрузка
на сайт заказчика вполне реальная. Сам показ при этом в сеть не идёт: товар
без снимка уходит в очередь `media/prefetch.py`, и фотография появляется со
следующего раза. Раньше ходил — и карточка стоила до минуты ожидания.

**Заранее, пакетом.** Перед демонстрацией удобно прогреть популярные разделы.
Здесь выгоднее страницы списков: одна отдаёт до трёх десятков превью сразу,
вместо тридцати обращений к карточкам.

Неудача — тоже результат и тоже запоминается. Иначе бот будет ходить на сайт
при каждом показе одной и той же карточки без фото.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from catalog.models import Product
from core.storage import Storage
from media.extract import decode, extract_attributes, extract_from_card, extract_from_listing
from media.fetcher import FetchError, PageFetcher
from media.files import PhotoStore

if TYPE_CHECKING:  # pragma: no cover — сборщик знает о службе, а не наоборот
    from media.prefetch import MediaPrefetcher

log = logging.getLogger(__name__)

# После стольких неудач подряд товар больше не трогаем: у части позиций
# фотографии на сайте просто нет.
MAX_FAILURES = 3


@dataclass
class MediaService:
    storage: Storage
    fetcher: PageFetcher
    enabled: bool = True
    # Хранилище файлов. Без него бот отдаёт только адреса, и Telegram остаётся
    # без фотографий: до vdm.ru его серверы не достают.
    photos: PhotoStore | None = None
    # Скачивать ли файл снимка при обходе каталога.
    download_files: bool = False
    # Фоновый сборщик недостающих снимков. Без него режим «только кэш» просто
    # отдаёт пустой список: показ карточки в сеть не ходит ни при каких условиях.
    prefetch: MediaPrefetcher | None = None

    # Сколько загрузок подряд закончились ошибкой сети. Отличает «у этого товара
    # нет фото» от «сайт не отвечает»: снаружи и то и другое выглядит как пустой
    # список, а решения требует разного.
    consecutive_errors: int = 0
    fetches: int = 0

    def images_for(self, product: Product, *, allow_fetch: bool = True) -> list[str]:
        """Адреса фотографий товара. Пустой список — фото нет, и это нормально.

        `allow_fetch=False` — режим хода диалога: отдаём только собранное, а товар
        без снимка откладываем фоновому сборщику. Сеть в ходе не трогаем вовсе.
        """
        if not self.enabled:
            return []

        cached = self.storage.load_media(product.sku_1c)
        if cached is not None and cached["failures"] >= MAX_FAILURES:
            return cached["images"]
        # Превью со страницы списка — 220×200: для карточки мелковато. Когда товар
        # показывают целиком, один раз доходим до его страницы за полным снимком.
        if cached is not None and cached["images"] and cached["source"] == "card":
            return cached["images"]
        if not product.url:
            return cached["images"] if cached else []
        if not allow_fetch:
            self.want(product)
            return cached["images"] if cached else []

        return self._fetch_card(product, cached)

    def main_image(self, product: Product, *, allow_fetch: bool = True) -> str | None:
        images = self.images_for(product, allow_fetch=allow_fetch)
        return images[0] if images else None

    def want(self, product: Product) -> bool:
        """Отложить товар фоновому сборщику. Возвращает, попал ли он в очередь."""
        if not self.enabled or self.prefetch is None:
            return False
        return self.prefetch.want(product)

    def fetch_now(self, product: Product) -> list[str]:
        """Сходить за снимком и положить файл на диск — работа фонового сборщика."""
        images = self.images_for(product)
        if images and self.photos is not None:
            self.photos.download(product.sku_1c, images)
        return images

    def collect(self, product: Product) -> list[str]:
        """Полный сбор по одному товару — для обхода каталога.

        От показа карточки отличается тем, что страница нужна ещё и ради
        характеристик: уже собранные фотографии сами по себе не повод её
        не открывать. Зато повторный проход по товару, у которого есть и то
        и другое, к сайту не обращается вовсе.
        """
        if not self.enabled:
            return []

        cached = self.storage.load_media(product.sku_1c)
        known = self.storage.has_attributes(product.sku_1c)
        exhausted = cached is not None and cached["failures"] >= MAX_FAILURES
        complete = known and cached is not None and cached["source"] == "card"

        if product.url and not exhausted and not complete:
            images = self._fetch_card(product, cached)
        else:
            images = cached["images"] if cached else []

        if self.download_files and self.photos is not None and images:
            self.photos.download(product.sku_1c, images)
        return images

    def local_photo(self, product: Product) -> str | None:
        """Путь к уже скачанному снимку. Сеть здесь не трогаем.

        Показ карточки не должен зависеть от того, отвечает ли сайт прямо сейчас:
        что собрано проходом — то и показываем.
        """
        if self.photos is None:
            return None
        path = self.photos.main(product.sku_1c)
        return str(path) if path else None

    def warm_up_from_listing(self, listing_url: str, products: list[Product]) -> int:
        """Прогрев по странице списка: одна загрузка — десятки превью.

        Сопоставление идёт по идентификатору Битрикса из выгрузки: он же стоит
        в разметке плитки, так что промахнуться товаром невозможно.
        """
        if not self.enabled:
            return 0
        try:
            result = self.fetcher.get(listing_url)
        except FetchError as exc:
            log.warning("Список %s не загружен: %s", listing_url, exc)
            return 0
        if result.body is None:
            return 0

        by_bitrix_id = {p.bitrix_id: p for p in products if p.bitrix_id is not None}
        saved = 0
        for item in extract_from_listing(decode(result.body), listing_url):
            product = by_bitrix_id.get(item.bitrix_id)
            if product is None or not item.image:
                continue
            cached = self.storage.load_media(product.sku_1c)
            if cached and cached["images"]:
                continue  # карточка даёт снимок лучшего качества — не затираем
            self.storage.save_media(product.sku_1c, [item.image], source="listing")
            saved += 1
        return saved

    # --- Внутреннее -----------------------------------------------------------

    def _fetch_card(self, product: Product, cached: dict | None) -> list[str]:
        etag = cached["etag"] if cached else None
        last_modified = cached["last_modified"] if cached else None
        failures = cached["failures"] if cached else 0

        self.fetches += 1
        try:
            result = self.fetcher.get(product.url, etag=etag, last_modified=last_modified)
        except FetchError as exc:
            self.consecutive_errors += 1
            log.info("Фото для %s не получено: %s", product.sku_1c, exc)
            self.storage.save_media(
                product.sku_1c,
                cached["images"] if cached else [],
                source=cached["source"] if cached else "",
                etag=etag,
                last_modified=last_modified,
                failures=failures + 1,
            )
            return cached["images"] if cached else []

        self.consecutive_errors = 0
        if result.not_modified and cached is not None:
            return cached["images"]
        if result.body is None:
            return cached["images"] if cached else []

        page = decode(result.body)
        media = extract_from_card(page, product.url)
        self.storage.save_media(
            product.sku_1c,
            media.images,
            source=media.source,
            etag=result.etag,
            last_modified=result.last_modified,
            failures=0 if media.images else failures + 1,
        )
        # Страница уже загружена — характеристики берём тем же проходом, второй
        # обход каталога ради страны и сертификата не нужен.
        self.storage.save_attributes(product.sku_1c, extract_attributes(page))
        return media.images
