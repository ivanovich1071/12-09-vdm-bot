"""Текущий утверждённый каталог: указатель `data/kb/current` и резолвер (EPIC 4, D11).

Источник истины о том, какой каталог сейчас у бота, — указатель, а не файл
`products.jsonl`. Указатель — JSON:

    {"schema": 1, "version": "2026-09-13-001", "sha256": "…"}

Снимок версии лежит по пути, выведенному из номера:
`<папка KB_PATH>/versions/<версия>/products.jsonl`. Путей в указателе нет, поэтому
он одинаково читается на Windows, в Docker и на сервере. sha256 лежит в самом
указателе: процессы бота базу `catalog.sqlite3` не читают (D9) и сверяют снимок
только по нему.

Указателя нет — каталог прежний: файл `KB_PATH`. `KB_PATH` указателем не
становится: по нему находится папка базы знаний и legacy-файл.

Все, кому нужен текущий каталог, получают его здесь: бот, импорт 1С, команды
`search`, `norms`, `acts`, `media`, `ingest`. Повреждённый снимок резолвер не
отдаёт — лучше отказ, чем неверные цены.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POINTER_NAME = "current"
POINTER_SCHEMA = 1
VERSIONS_DIR = "versions"
SNAPSHOT_NAME = "products.jsonl"

# Номер версии становится именем папки: только безопасные символы, без точки в начале.
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# На Windows `os.replace` отказывает, пока другой процесс держит файл открытым.
# Читатель указателя держит его доли миллисекунды — хватает короткого повтора.
_REPLACE_ATTEMPTS = 5
_REPLACE_PAUSE = 0.1


class CatalogPointerError(RuntimeError):
    """Указатель есть, но по нему нельзя получить целый снимок."""


@dataclass(frozen=True)
class Pointer:
    version: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema": POINTER_SCHEMA, "version": self.version, "sha256": self.sha256}


@dataclass(frozen=True)
class CatalogSnapshot:
    """Проверенный каталог: путь к файлу, его sha256 и версия (`None` — legacy-файл)."""

    path: Path
    sha256: str
    version: str | None = None

    @property
    def is_legacy(self) -> bool:
        return self.version is None

    @property
    def base_version(self) -> str:
        """Версия, против которой считается diff. Без указателя — `legacy:<sha256>`."""
        return self.version or legacy_version(self.sha256)

    def records(self) -> list[dict[str, Any]]:
        return read_records(self.path)


def legacy_version(sha256: str) -> str:
    return f"legacy:{sha256}"


def kb_dir(kb_path: str | Path) -> Path:
    return Path(kb_path).parent


def pointer_path(directory: str | Path) -> Path:
    return Path(directory) / POINTER_NAME


def versions_dir(directory: str | Path) -> Path:
    return Path(directory) / VERSIONS_DIR


def snapshot_path(directory: str | Path, version: str) -> Path:
    check_version(version)
    return versions_dir(directory) / version / SNAPSHOT_NAME


def check_version(version: str) -> str:
    if not isinstance(version, str) or not _VERSION.match(version):
        raise CatalogPointerError(f"Недопустимый номер версии каталога: {version!r}.")
    return version


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_records(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read_pointer(directory: str | Path) -> Pointer | None:
    """Указатель папки базы знаний или `None`, если его нет."""
    path = pointer_path(directory)
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogPointerError(f"Указатель {path} не читается как JSON: {exc}.") from exc
    if not isinstance(data, dict) or data.get("schema") != POINTER_SCHEMA:
        raise CatalogPointerError(
            f"Указатель {path}: ожидается schema={POINTER_SCHEMA}, получено {data!r}."
        )
    version, sha = data.get("version"), data.get("sha256")
    check_version(version)
    if not isinstance(sha, str) or not _SHA256.match(sha):
        raise CatalogPointerError(f"Указатель {path}: неверный sha256 {sha!r}.")
    return Pointer(version=version, sha256=sha)


def write_pointer(directory: str | Path, pointer: Pointer) -> None:
    """Атомарная замена указателя: временный файл рядом, `fsync`, `os.replace`."""
    check_version(pointer.version)
    target = pointer_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{POINTER_NAME}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(pointer.to_dict(), ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    replace_with_retry(tmp, target)


def remove_pointer(directory: str | Path) -> None:
    pointer_path(directory).unlink(missing_ok=True)


def replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                source.unlink(missing_ok=True)
                raise
            time.sleep(_REPLACE_PAUSE)


def verify_pointer(directory: str | Path, pointer: Pointer) -> CatalogSnapshot:
    """Снимок версии из указателя — только если он есть и sha256 совпадает."""
    path = snapshot_path(directory, pointer.version)
    if not path.is_file():
        raise CatalogPointerError(
            f"Указатель {pointer_path(directory)} ссылается на версию {pointer.version}, "
            f"но снимка {path} нет. Каталог не загружен."
        )
    actual = file_sha256(path)
    if actual != pointer.sha256:
        raise CatalogPointerError(
            f"Снимок версии {pointer.version} повреждён: sha256 {actual[:12]}… не совпадает "
            f"с указателем {pointer.sha256[:12]}…. Каталог не загружен."
        )
    return CatalogSnapshot(path=path, sha256=actual, version=pointer.version)


def resolve_catalog(kb_path: str | Path) -> CatalogSnapshot:
    """Текущий каталог: снимок по указателю или, без указателя, legacy-файл `KB_PATH`."""
    directory = kb_dir(kb_path)
    pointer = read_pointer(directory)
    if pointer is not None:
        return verify_pointer(directory, pointer)
    legacy = Path(kb_path)
    if not legacy.is_file():
        raise FileNotFoundError(
            f"База знаний не собрана: {legacy}. Загрузите выгрузку: "
            "`python run.py import-1c --file <выгрузка.xlsx>`."
        )
    return CatalogSnapshot(path=legacy, sha256=file_sha256(legacy))


def _read_text(path: Path) -> str | None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except PermissionError:
            # Указатель как раз заменяют: на Windows чтение в этот миг отказывает.
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_PAUSE)
    return None
