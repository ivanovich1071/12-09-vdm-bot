"""Карточки каталога: одна сборка для `ingest`, diff и снимка версии (EPIC 4, D11).

Запись карточки — словарь в том виде, в каком он лежит строкой `products.jsonl`.
Выгрузка 1С даёт только часть полей. Нормы реестра 1057 добавляет
`apply_registry`, фото и характеристики переносит `carry_over_collected`. Diff
сравнивает карточку, собранную этими же функциями, со снимком. Если сравнивать
сырой импорт с обогащённым снимком, фото и реестр дали бы `UPDATED` почти всему
каталогу.

У каждого поля есть владелец: выгрузка 1С, реестр 1057, страницы сайта или
служебные поля сборки. Версия `media` может менять только поля сайта, версия
`registry` — только нормы (`forbidden_changes`).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from catalog.models import Product
from catalog_import.parser import ParsedProduct
from catalog_versions.models import ChangeStatus, ProductChange
from media.sync import collected
from norms import documents as norm_docs
from norms.extract import SOURCE_WEIGHTS

if TYPE_CHECKING:
    from catalog_import.models import ImportItem

Record = dict[str, Any]

RECORD_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(ParsedProduct))
_DEFAULTS: dict[str, Any] = {
    "url": None,
    "short_url": None,
    "price": None,
    "currency": "RUB",
    "in_stock": None,
    "category_paths": [],
    "description": "",
    "kit_contents": [],
    "norms": [],
    "bitrix_id": None,
    "images": [],
    "attributes": {},
    "sources": {},
    "updated_at": "",
}

# Служебные поля сборки: время и имя файла выгрузки меняются каждый импорт.
SERVICE_FIELDS = frozenset({"updated_at", "sources"})
# Поля со страниц сайта.
MEDIA_FIELDS = frozenset({"images", "attributes"})
# Нормативные ссылки: из дерева выгрузки и из реестра 1057.
NORM_FIELDS = frozenset({"norms"})

REGISTRY_SOURCE = "registry"
REGISTRY_DOC = "order_1057"


def canonical(record: Mapping[str, Any]) -> Record:
    """Все поля карточки со значениями по умолчанию: старые записи без полей сравнимы."""
    result = {name: record.get(name, _DEFAULTS.get(name)) for name in RECORD_FIELDS}
    for name in sorted(set(record) - set(RECORD_FIELDS)):
        result[name] = record[name]
    return result


def changed_fields(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[str, ...]:
    """Изменившиеся поля карточки без служебных, в порядке полей записи."""
    left, right = canonical(old), canonical(new)
    names = list(RECORD_FIELDS) + sorted((set(left) | set(right)) - set(RECORD_FIELDS))
    return tuple(
        name
        for name in names
        if name not in SERVICE_FIELDS and left.get(name) != right.get(name)
    )


def forbidden_changes(
    old: Sequence[Mapping[str, Any]],
    new: Sequence[Mapping[str, Any]],
    allowed: frozenset[str],
) -> list[str]:
    """Нарушения для версий `media` и `registry`: набор кодов и чужие поля не меняются."""
    problems: list[str] = []
    before = {record["sku_1c"]: record for record in old}
    after = {record["sku_1c"]: record for record in new}
    if before.keys() != after.keys():
        added = sorted(after.keys() - before.keys())
        removed = sorted(before.keys() - after.keys())
        problems.append(f"изменился набор товаров: добавлено {added[:5]}, убрано {removed[:5]}")
    for code in sorted(before.keys() & after.keys()):
        extra = [name for name in changed_fields(before[code], after[code]) if name not in allowed]
        if extra:
            problems.append(f"код {code}: запрещено менять {', '.join(extra)}")
    return problems


def apply_registry(records: Iterable[Record], mapping: Mapping[str, list[dict[str, str]]]) -> int:
    """Достраивает привязку к приказу 1057 по реестру заказчика. Возвращает число товаров.

    Реестр — решение самого заказчика, какой товар какой позиции перечня
    соответствует, поэтому он старше всего, что мы вывели сами. Привязка к 1057
    без номера пункта у товара из реестра убирается: точный пункт уже известен.
    Эта чистка необратима. Если товар потом убрать из реестра, пересборка версии
    `registry` такую привязку не вернёт, а `ingest` из выгрузки вернёт (риск
    записан в ARCHITECTURE_CHANGE.md).
    """
    records = list(records)
    if not mapping:
        return 0
    doc = norm_docs.get(REGISTRY_DOC)
    count = 0
    for record in records:
        entries = mapping.get(record["sku_1c"])
        if not entries:
            continue
        norms = record.setdefault("norms", [])
        known = {
            norm["item_code"]
            for norm in norms
            if norm["doc_id"] == REGISTRY_DOC and norm["item_code"]
        }
        for entry in entries:
            if entry["item_code"] in known:
                continue
            norms.append(
                {
                    "doc_id": REGISTRY_DOC,
                    "doc_citation": doc.citation,
                    "item_code": entry["item_code"],
                    "item_title": entry["item_title"],
                    "source": REGISTRY_SOURCE,
                    "confidence": SOURCE_WEIGHTS[REGISTRY_SOURCE],
                }
            )
        count += 1

    for record in records:
        norms = record.get("norms") or []
        if any(norm["source"] == REGISTRY_SOURCE for norm in norms):
            record["norms"] = [
                norm for norm in norms if norm["item_code"] or norm["doc_id"] != REGISTRY_DOC
            ]
    return count


def strip_registry(records: Iterable[Record]) -> None:
    """Убирает ссылки, добавленные реестром, — перед повторным применением реестра."""
    for record in records:
        norms = record.get("norms") or []
        record["norms"] = [norm for norm in norms if norm.get("source") != REGISTRY_SOURCE]


def carry_over_collected(records: Iterable[Record], known: Mapping[str, Mapping[str, Any]]) -> None:
    """Фото и характеристики, собранные с сайта раньше, — товарам, у которых их нет."""
    for record in records:
        kept = known.get(record["sku_1c"])
        if not kept:
            continue
        if not record.get("images"):
            record["images"] = list(kept.get("images", []))
        if not record.get("attributes"):
            record["attributes"] = dict(kept.get("attributes", {}))


def assemble_import(
    items: Sequence[ImportItem],
    current: Sequence[Mapping[str, Any]] | None,
    registry: Mapping[str, list[dict[str, str]]],
    rejected_codes: Iterable[str] = (),
) -> list[Record]:
    """Карточки каталога после импорта — тем же порядком шагов, что и `ingest`.

    - товары импорта в порядке файла;
    - реестр 1057;
    - фото и характеристики текущего каталога;
    - существующие товары, чья строка исключена ошибкой, — прежней карточкой, в
      конце по коду.

    Исчезнувшие коды в результат не попадают.
    """
    records = [copy.deepcopy(item.payload) for item in items]
    apply_registry(records, registry)
    if current:
        carry_over_collected(records, collected(current))
        by_code = {record["sku_1c"]: record for record in current}
        imported = {record["sku_1c"] for record in records}
        records += [
            copy.deepcopy(dict(by_code[code]))
            for code in sorted(set(rejected_codes))
            if code in by_code and code not in imported
        ]
    return records


def validate_records(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Проверки снимка, которые `--force` не отменяет."""
    problems: list[str] = []
    if not records:
        return ["в снимке нет ни одного товара"]
    seen: set[str] = set()
    for number, record in enumerate(records, start=1):
        code = str(record.get("sku_1c") or "").strip()
        if not code:
            problems.append(f"строка {number}: нет кода 1С")
            continue
        if code in seen:
            problems.append(f"код {code} повторяется")
        seen.add(code)
        if not str(record.get("name") or "").strip():
            problems.append(f"код {code}: нет наименования")
        try:
            Product.from_dict(dict(record))
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"код {code}: карточка не читается ({type(exc).__name__}: {exc})")
        if len(problems) >= 20:
            break
    return problems


def serialize(records: Iterable[Mapping[str, Any]]) -> bytes:
    """Строки снимка — тем же форматом, что пишет `ingest`."""
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode(
        "utf-8"
    )


def product_changes(
    parent: Sequence[Mapping[str, Any]] | None, records: Sequence[Mapping[str, Any]]
) -> list[ProductChange]:
    """Изменения товаров между двумя снимками — основа истории `product_versions`.

    Считается только из снимков, а не из сохранённого diff: так история одинаково
    строится для импорта, фото, реестра, отката и при восстановлении после сбоя.
    """
    before = {record["sku_1c"]: record for record in parent or ()}
    changes: list[ProductChange] = []
    seen: set[str] = set()
    for record in records:
        code = record["sku_1c"]
        seen.add(code)
        old = before.get(code)
        if old is None:
            changes.append(ProductChange(code, ChangeStatus.NEW, dict(record)))
            continue
        changed = changed_fields(old, record)
        if changed:
            changes.append(ProductChange(code, ChangeStatus.UPDATED, dict(record), changed))
    changes += [
        ProductChange(code, ChangeStatus.REMOVED, dict(old))
        for code, old in before.items()
        if code not in seen
    ]
    return changes


def change_counters(
    changes: Sequence[ProductChange], products: int, parent_products: int
) -> dict[str, Any]:
    """Счётчики версии и доли для порогов безопасности."""
    removed = sum(change.change_status is ChangeStatus.REMOVED for change in changes)
    updated = [change for change in changes if change.change_status is ChangeStatus.UPDATED]
    price_changed = sum("price" in change.changed_fields for change in updated)
    existing = parent_products - removed
    return {
        "products": products,
        "parent_products": parent_products,
        "new": sum(change.change_status is ChangeStatus.NEW for change in changes),
        "updated": len(updated),
        "removed": removed,
        "price_changed": price_changed,
        "stock_changed": sum("in_stock" in change.changed_fields for change in updated),
        "removed_share": round(removed / parent_products, 4) if parent_products else 0.0,
        "price_changed_share": round(price_changed / existing, 4) if existing else 0.0,
    }


def load_registry(path: Path | None) -> tuple[dict[str, list[dict[str, str]]], str | None]:
    """Реестр 1057 и sha256 его файла. Нет файла — пустой реестр и `None`."""
    from catalog.current import file_sha256

    if path is None or not path.is_file():
        return {}, None
    mapping = json.loads(path.read_text(encoding="utf-8")).get("products", {})
    return mapping, file_sha256(path)
