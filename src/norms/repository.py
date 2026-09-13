"""Нормативная база как источник истины: документы и пункты перечней.

Цепочка подбора по нормативу — ДОКУМЕНТ → ПУНКТ → КАТЕГОРИЯ → ТОВАР (ТЗ §6).
Первые два звена здесь: реестр документов (`norms/documents.py`) и справочник
пунктов из текстов приказов (`norms/items.py`). Привязка «пункт → товар» лежит в
снимке каталога и читается `norms/mapping.py`.

Модель в цепочку не входит: пункт, его название и количество по перечню берутся
только отсюда.

**Версия нормативной базы** — отпечаток реестра документов и справочника пунктов.
Привязки товаров к пунктам входят в снимок каталога, их версия — версия каталога.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from norms import documents as docs
from norms.items import DEFAULT_ITEMS, ItemIndex, NormItem, load

_INTEGER = re.compile(r"^\d{1,5}$")


class QuantityRule(StrEnum):
    """Количество по перечню, заданное правилом, а не числом."""

    PER_CHILD = "per_child"
    PER_GROUP = "per_group"


def norm_quantity(item: NormItem | None) -> int | None:
    """Количество из таблицы приказа, только если это целое число.

    Разбор PDF 1057 даёт и «1», и «По количест ву окон», и хвосты соседних строк.
    Числом считается только ячейка из одних цифр: остальное — не количество.
    """
    if item is None or not item.quantity:
        return None
    text = item.quantity.strip()
    if not _INTEGER.match(text):
        return None
    value = int(text)
    return value if value > 0 else None


def quantity_rule(item: NormItem | None) -> QuantityRule | None:
    """«По количеству детей в группе», «1 шт. на каждую группу» — с учётом разрывов вёрстки."""
    if item is None or not item.quantity:
        return None
    folded = "".join(item.quantity.lower().split())
    if "количествудетей" in folded:
        return QuantityRule.PER_CHILD
    if "накаждуюгрупп" in folded:
        return QuantityRule.PER_GROUP
    return None


def code_key(code: str) -> tuple[int, ...]:
    """Естественный порядок пунктов: 1.2 < 1.10."""
    return tuple(int(part) for part in code.split(".") if part.isdigit())


class NormRepository(Protocol):
    @property
    def version(self) -> str: ...

    @property
    def loaded(self) -> bool: ...

    def documents(self) -> list[docs.NormDocument]: ...

    def document(self, doc_id: str) -> docs.NormDocument | None: ...

    def item(self, doc_id: str, code: str) -> NormItem | None: ...

    def has_items(self, doc_id: str) -> bool: ...

    def documents_with(self, code: str) -> list[str]: ...

    def children(self, doc_id: str, prefix: str) -> list[NormItem]: ...

    def search(self, text: str, doc_id: str | None = None, limit: int = 5) -> list[NormItem]: ...


class FileNormRepository:
    """Нормативная база из `norm_items.json` и реестра документов."""

    def __init__(self, items: dict[str, dict[str, NormItem]] | None = None) -> None:
        self._items = items or {}
        self._index = ItemIndex(self._items)
        self._version = _version(self._items)

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_ITEMS) -> FileNormRepository:
        return cls(load(Path(path)))

    @property
    def version(self) -> str:
        return self._version

    @property
    def loaded(self) -> bool:
        return bool(self._items)

    def documents(self) -> list[docs.NormDocument]:
        return list(docs.DOCUMENTS.values())

    def document(self, doc_id: str) -> docs.NormDocument | None:
        return docs.DOCUMENTS.get(doc_id)

    def item(self, doc_id: str, code: str) -> NormItem | None:
        return self._index.get(doc_id, code)

    def has_items(self, doc_id: str) -> bool:
        return self._index.count(doc_id) > 0

    def documents_with(self, code: str) -> list[str]:
        """Документы, где есть пункт или подраздел с таким номером."""
        return sorted(
            doc_id
            for doc_id, by_code in self._items.items()
            if code in by_code or any(known.startswith(f"{code}.") for known in by_code)
        )

    def children(self, doc_id: str, prefix: str) -> list[NormItem]:
        found = [
            item
            for code, item in self._items.get(doc_id, {}).items()
            if code.startswith(f"{prefix}.")
        ]
        return sorted(found, key=lambda item: code_key(item.code))

    def search(self, text: str, doc_id: str | None = None, limit: int = 5) -> list[NormItem]:
        return self._index.search(text, doc_id, limit)


def _version(items: dict[str, dict[str, NormItem]]) -> str:
    payload = {
        "documents": [
            [doc.id, doc.citation, doc.subject, doc.registration or ""]
            for doc in sorted(docs.DOCUMENTS.values(), key=lambda doc: doc.id)
        ],
        "items": {
            doc_id: [
                [item.code, item.title, item.section, item.unit, item.quantity]
                for item in sorted(by_code.values(), key=lambda item: code_key(item.code))
            ]
            for doc_id, by_code in sorted(items.items())
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "norms-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
