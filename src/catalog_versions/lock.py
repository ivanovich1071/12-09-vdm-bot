"""Блокировка применения версии каталога на уровне ОС.

Одновременно применяется одна версия. Блокировка — замок файла средствами ОС
(`msvcrt.locking` на Windows, `fcntl.flock` на Linux). Если процесс упал, ОС снимает
замок сама, и файл-замок не остаётся «занятым» навсегда. Транзакция SQLite на время
работы со снимками не держится: база остаётся доступной загрузке импортов.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType


class CatalogLockTimeout(RuntimeError):
    """Другой процесс применяет версию каталога дольше, чем мы готовы ждать."""


class CatalogApplyLock:
    def __init__(self, path: str | Path, timeout: float = 60.0, pause: float = 0.1) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.pause = pause
        self._fh = None

    def __enter__(self) -> CatalogApplyLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+b")
        if fh.seek(0, os.SEEK_END) == 0:
            fh.write(b"\0")
            fh.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _lock(fh)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise CatalogLockTimeout(
                        f"Каталог сейчас применяет другой процесс (замок {self.path}). "
                        "Повторите команду позже."
                    ) from exc
                time.sleep(self.pause)
        self._fh = fh
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            fh.close()


if os.name == "nt":
    import msvcrt

    def _lock(fh) -> None:  # noqa: ANN001 — файловый объект
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:  # noqa: ANN001
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fh) -> None:  # noqa: ANN001
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:  # noqa: ANN001
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
