"""Хранилище загруженных файлов.

Файл лежит под своей контрольной суммой: `data/uploads/ab/ab12…ef.xlsx`. Одно и то
же содержимое хранится один раз, как бы ни назывался файл. Папка в git не
попадает: там коммерческие данные заказчика.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

MB = 1024 * 1024
MIME_TYPES = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


class UploadRejected(ValueError):
    """Файл не принят: ничего не сохранено, импорт не создан."""


def check_upload(path: Path, max_bytes: int) -> None:
    if not path.is_file():
        raise UploadRejected(f"Файл не найден: {path}")
    if path.suffix.lower() not in MIME_TYPES:
        raise UploadRejected(
            f"Принимается только выгрузка .xlsx, а у файла «{path.suffix or 'без расширения'}»."
        )
    size = path.stat().st_size
    if size == 0:
        raise UploadRejected("Файл пустой.")
    if size > max_bytes:
        raise UploadRejected(
            f"Файл весит {megabytes(size)} МБ — больше предела {megabytes(max_bytes)} МБ."
        )


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(MB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def megabytes(size: int) -> str:
    return f"{size / MB:.1f}".replace(".", ",")


class FileStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def store(self, source: Path, digest: str) -> Path:
        """Копия под контрольной суммой. Запись через временный файл: прерванная
        копия не останется на месте готовой."""
        target = self.root / digest[:2] / f"{digest}{source.suffix.lower()}"
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        shutil.copyfile(source, partial)
        partial.replace(target)
        return target
