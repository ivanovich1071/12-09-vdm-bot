"""Недостающие снимки собираются в фоне.

Показ карточки не должен зависеть от того, отвечает ли сейчас vdm.ru. Раньше
зависел: `_image()` и `photo_path()` шли на сайт прямо в ходе диалога, а у
`PageFetcher` есть и таймаут, и повторные попытки, и общий для процесса
шлагбаум в один запрос в секунду. В худшем случае одна карточка стоила минуты
ожидания — и не ей одной, а всем, кто в это время писал боту.

Теперь ход берёт только то, что уже лежит в базе и на диске, а товар без
снимка откладывает сюда. Очередь разбирает один поток-демон: следующий показ
того же товара будет уже с фотографией.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — только для подсказок типов
    from catalog.models import Product
    from media.service import MediaService

log = logging.getLogger(__name__)

# Очередь ограничена: если сайт лежит, а разговоров много, копить бесконечно
# нечего — товары вернутся в очередь при следующем показе.
QUEUE_SIZE = 500


class MediaPrefetcher:
    """Очередь товаров, которым не хватает снимка, и поток, который её разбирает."""

    def __init__(self, service: MediaService, *, size: int = QUEUE_SIZE) -> None:
        self.service = service
        self._queue: queue.Queue[Product] = queue.Queue(maxsize=size)
        # Что уже в очереди или уже обработано в этом запуске: второй показ
        # того же товара не ставит его в очередь снова.
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- Постановка в очередь -------------------------------------------------

    def want(self, product: Product) -> bool:
        """Отложить товар на потом. Возвращает, попал ли он в очередь."""
        with self._seen_lock:
            if product.sku_1c in self._seen:
                return False
            self._seen.add(product.sku_1c)
        try:
            self._queue.put_nowait(product)
        except queue.Full:
            with self._seen_lock:
                self._seen.discard(product.sku_1c)
            return False
        return True

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    # --- Разбор очереди -------------------------------------------------------

    def run_once(self, timeout: float | None = None) -> bool:
        """Взять один товар и собрать по нему фото. Возвращает, было ли что брать."""
        try:
            product = self._queue.get(timeout=timeout) if timeout else self._queue.get_nowait()
        except queue.Empty:
            return False
        try:
            self.service.fetch_now(product)
        except Exception as exc:  # фоновая работа не должна ронять процесс
            log.warning("Фото для %s не собрано: %s", product.sku_1c, exc)
        finally:
            self._queue.task_done()
        return True

    def drain(self) -> int:
        """Разобрать очередь до конца, не заводя потока. Для команд и тестов."""
        done = 0
        while self.run_once():
            done += 1
        return done

    # --- Поток ----------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="media-prefetch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Пауза в очереди — обычное состояние: ждём товара, а не крутим цикл.
            self.run_once(timeout=1.0)
