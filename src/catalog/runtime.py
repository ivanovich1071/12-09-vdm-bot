"""Состояние каталога в процессе бота и горячая замена (EPIC 4, D11).

Всё, что процесс знает о каталоге, лежит в одном неизменяемом объекте
`CatalogRuntimeState`: версия, sha256, путь к снимку, индекс поиска, сервис
каталога, разделы. Новая версия собирается целиком и ставится одним
присваиванием. `index`, `catalog` и разделы никогда не меняются по отдельности.

Ход пользователя закрепляет состояние в начале (`CatalogRuntime.turn`) и до
конца работает с ним. Ход агента с моделью длится минуты: без закрепления поиск
мог бы пройти по одной версии, а цена в карточке — по другой.

Перед каждым ходом и фоновым потоком раз в `CATALOG_RELOAD_SECONDS` указатель
сверяется с состоянием. Если версия новая, в фоне строится новое состояние, а ходы
пока обслуживает прежнее. Одновременно строится не больше одного состояния.
Повреждённый снимок состояние не заменяет: процесс остаётся на прежнем, ошибка
пишется в журнал.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from catalog.current import (
    CatalogPointerError,
    CatalogSnapshot,
    kb_dir,
    read_pointer,
    resolve_catalog,
)
from catalog.models import Product
from catalog.repository import InMemoryCatalogRepository
from catalog.search import CatalogIndex
from catalog.service import CatalogService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogRuntimeState:
    index: CatalogIndex
    catalog: CatalogService
    roots: tuple[str, ...]
    # `None` — legacy-файл без указателя или индекс, переданный напрямую (тесты).
    version: str | None = None
    sha256: str | None = None
    snapshot_path: Path | None = None
    loaded_at: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> tuple[str | None, str | None]:
        return self.version, self.sha256

    @property
    def label(self) -> str:
        """Версия для записи в подбор, спецификацию, оценку заказа и предзаказ.

        Без указателя версии нет — тогда отпечаток файла; у индекса из тестов нет и его.
        """
        if self.version:
            return self.version
        if self.sha256:
            return f"legacy:{self.sha256[:12]}"
        return "unversioned"

    @classmethod
    def from_index(
        cls,
        index: CatalogIndex,
        *,
        version: str | None = None,
        sha256: str | None = None,
        snapshot_path: Path | None = None,
    ) -> CatalogRuntimeState:
        roots: dict[str, None] = {}
        for product in index.products:
            for root in product.roots:
                roots.setdefault(root, None)
        return cls(
            index=index,
            catalog=CatalogService(InMemoryCatalogRepository(index)),
            roots=tuple(roots),
            version=version,
            sha256=sha256,
            snapshot_path=snapshot_path,
        )

    @classmethod
    def load(cls, snapshot: CatalogSnapshot) -> CatalogRuntimeState:
        """Состояние из проверенного снимка. Байты сверяются ещё раз при чтении."""
        data = snapshot.path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if not snapshot.is_legacy and actual != snapshot.sha256:
            raise CatalogPointerError(
                f"Снимок версии {snapshot.version} изменился при чтении: sha256 не совпадает."
            )
        products = [
            Product.from_dict(json.loads(line))
            for line in data.decode("utf-8").splitlines()
            if line.strip()
        ]
        if not products:
            raise CatalogPointerError(f"В каталоге {snapshot.path} нет ни одного товара.")
        return cls.from_index(
            CatalogIndex(products),
            version=snapshot.version,
            sha256=actual,
            snapshot_path=snapshot.path,
        )


class CatalogRuntime:
    """Текущее состояние каталога процесса, закрепление на ход и горячая замена."""

    def __init__(
        self,
        state: CatalogRuntimeState,
        kb_path: str | Path | None = None,
        *,
        loader: Callable[[CatalogSnapshot], CatalogRuntimeState] = CatalogRuntimeState.load,
    ) -> None:
        self._state = state
        self.kb_path = Path(kb_path) if kb_path is not None else None
        self._loader = loader
        # Своя переменная контекста у каждого экземпляра: в тестах движков несколько.
        self._pinned: ContextVar[CatalogRuntimeState | None] = ContextVar(
            f"catalog_state_{id(self)}", default=None
        )
        # Замок сборки держит только сам поток сборки. Запуск потока защищён своим
        # замком: иначе поток не мог бы взять замок сборки, пока его запускают.
        self._build_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        self._failed: tuple[str, str] | None = None
        self._pointer_missing_logged = False
        self.reloads = 0

    @classmethod
    def open(cls, kb_path: str | Path, **kwargs) -> CatalogRuntime:  # noqa: ANN003
        """Состояние при старте — синхронно. Повреждённый указатель останавливает старт."""
        loader = kwargs.get("loader", CatalogRuntimeState.load)
        return cls(loader(resolve_catalog(kb_path)), kb_path, **kwargs)

    # --- Чтение ----------------------------------------------------------------

    @property
    def state(self) -> CatalogRuntimeState:
        """Закреплённое за текущим ходом состояние, а вне хода — текущее."""
        pinned = self._pinned.get()
        return pinned if pinned is not None else self._state

    def current(self) -> CatalogRuntimeState:
        return self._state

    def replace(self, state: CatalogRuntimeState) -> None:
        """Одна атомарная операция: ссылка на новое состояние."""
        self._state = state

    @contextmanager
    def turn(self) -> Iterator[CatalogRuntimeState]:
        """Закрепить одно состояние на весь ход. Вложенный вызов закрепление не меняет."""
        pinned = self._pinned.get()
        if pinned is not None:
            yield pinned
            return
        self.refresh()
        state = self._state
        token = self._pinned.set(state)
        try:
            yield state
        finally:
            self._pinned.reset(token)

    # --- Горячая замена --------------------------------------------------------

    def refresh(self, wait: bool = False) -> bool:
        """Сверить указатель с состоянием. Новая версия строится в фоне (или сразу при `wait`).

        Возвращает `True`, только если состояние заменено в этом вызове.
        """
        target = self._pending()
        if target is None:
            return False
        if wait:
            return self._reload(target)
        with self._thread_lock:
            running = self._thread is not None and self._thread.is_alive()
            if not running and not self._build_lock.locked():
                self._thread = threading.Thread(
                    target=self._reload, args=(target,), name="catalog-reload", daemon=True
                )
                self._thread.start()
        return False

    def join(self, timeout: float | None = None) -> None:
        """Дождаться фоновой сборки — для тестов и остановки процесса."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def start_watching(self, interval: float) -> None:
        """Фоновый поток: указатель сверяется раз в `interval` секунд."""
        if self.kb_path is None or interval <= 0 or self._watcher is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    target = self._pending()
                    if target is not None:
                        self._reload(target)
                except Exception:  # поток не должен умирать из-за одной неудачи
                    log.exception("Проверка версии каталога не удалась")

        self._watcher = threading.Thread(target=loop, name="catalog-watch", daemon=True)
        self._watcher.start()

    def stop(self) -> None:
        self._stop.set()

    def _pending(self) -> tuple[str, str] | None:
        if self.kb_path is None:
            return None
        try:
            pointer = read_pointer(kb_dir(self.kb_path))
        except (CatalogPointerError, OSError) as exc:
            log.error("Указатель каталога не читается, остаётся прежняя версия: %s", exc)
            return None
        if pointer is None:
            if self._state.version is not None and not self._pointer_missing_logged:
                log.warning(
                    "Указатель каталога пропал; процесс остаётся на версии %s до перезапуска.",
                    self._state.version,
                )
                self._pointer_missing_logged = True
            return None
        target = (pointer.version, pointer.sha256)
        if target == self._state.key or target == self._failed:
            return None
        return target

    def _reload(self, target: tuple[str, str]) -> bool:
        if not self._build_lock.acquire(blocking=False):
            return False
        try:
            if self._pending() is None:
                return False
            try:
                state = self._loader(resolve_catalog(self.kb_path))
            except (CatalogPointerError, OSError, ValueError) as exc:
                self._failed = target
                log.error(
                    "Версия каталога %s не загружена, работаем на %s: %s",
                    target[0],
                    self._state.version or "legacy",
                    exc,
                )
                return False
            previous = self._state
            self._state = state
            self._failed = None
            self._pointer_missing_logged = False
            self.reloads += 1
            log.info(
                "Каталог обновлён без перезапуска: %s → %s, товаров %s",
                previous.version or "legacy",
                state.version,
                len(state.index.products),
            )
            return True
        finally:
            self._build_lock.release()
