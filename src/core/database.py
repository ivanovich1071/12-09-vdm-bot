"""База доменных сущностей ядра: закупки, заказы клиентов, предзаказы, сессии API.

SQLite за репозиториями (D4), схема — версионированными миграциями `core/schema/`.
Файл по умолчанию тот же, что у хранилища бота (`STORAGE_PATH`): предзаказ несёт
контакты клиента, и удаление данных по требованию субъекта проходит там же, где
удаляются корзина, заказы и согласия.

Соединение одно на процесс и защищено замком: запросы Core API идут из пула потоков.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from core.migrations import apply_migrations

SCHEMA = Path(__file__).parent / "schema"


class CoreDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            apply_migrations(self._db, SCHEMA)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._db

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Одна транзакция: всё или ничего."""
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()
