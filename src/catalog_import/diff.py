"""Diff импорта 1С против текущего каталога (EPIC 4, D11).

База — текущий каталог по резолверу (`catalog/current.py`), ключ — код 1С.

- Сравниваются карточки, собранные так же, как снимок (`catalog_versions/cards.py`):
  товары импорта, реестр 1057, фото и характеристики текущего каталога. Повторная
  загрузка той же выгрузки против снимка из неё даёт `UNCHANGED` по всем товарам.
- Состояние кода — `EXISTING` / `NEW` / `MISSING` — решает импорт по точному коду.
  Сопоставление EPIC 3 проверяет пару «код → товар» и ищет новым кодам пару только
  среди исчезнувших, никогда по всему каталогу.
- Статусы позиции: `NEW`, `UPDATED`, `UNCHANGED`, `REMOVED` (код `MISSING`),
  `AMBIGUOUS` (новый код, несколько кандидатов среди исчезнувших). Новый код с
  одним кандидатом — `NEW` с отметкой `recoding`. Автоматически ничего не
  связывается.

Отпечаток diff — sha256 базового снимка, реестра 1057 и строк diff без времени.
Утверждение пересчитывает diff и сравнивает отпечаток: утверждается только то,
что видел менеджер.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from catalog.matcher import CatalogMatcher, MatchSettings, MatchStatus
from catalog.models import Product
from catalog.repository import ProductListRepository
from catalog_import.matching import CodeMatch, CodeState, compare_with_catalog
from catalog_import.models import CatalogComparison, ImportItem
from catalog_versions.cards import Record, assemble_import, changed_fields

if TYPE_CHECKING:
    from catalog.current import CatalogSnapshot
    from catalog_import.models import CatalogImport

FINGERPRINT_SCHEMA = 1


class DiffStatus(StrEnum):
    NEW = "NEW"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    REMOVED = "REMOVED"
    AMBIGUOUS = "AMBIGUOUS"


class PriceStatus(StrEnum):
    UNCHANGED = "UNCHANGED"
    INCREASED = "INCREASED"
    DECREASED = "DECREASED"
    # У существующего товара — цена появилась, у нового товара — просто новая.
    NEW = "NEW"
    # У существующего товара — цена снята («по запросу»), у исчезнувшего — вместе с товаром.
    REMOVED = "REMOVED"


DIFF_STATUS_LABELS: dict[DiffStatus, str] = {
    DiffStatus.NEW: "новый товар",
    DiffStatus.UPDATED: "изменился",
    DiffStatus.UNCHANGED: "без изменений",
    DiffStatus.REMOVED: "исчез из файла",
    DiffStatus.AMBIGUOUS: "новый код, несколько кандидатов среди исчезнувших",
}

FIELD_LABELS: dict[str, str] = {
    "name": "название",
    "url": "ссылка",
    "short_url": "короткая ссылка",
    "price": "цена",
    "currency": "валюта",
    "in_stock": "остаток",
    "category_paths": "разделы",
    "description": "описание",
    "kit_contents": "состав комплекта",
    "norms": "нормативные ссылки",
    "bitrix_id": "ID Битрикса",
    "images": "фото",
    "attributes": "характеристики",
}


@dataclass(frozen=True)
class DiffRow:
    """Позиция diff: `old_*` — текущий каталог, `new_*` — каталог после импорта."""

    sku_1c: str
    state: CodeState
    diff_status: DiffStatus
    price_status: PriceStatus
    changed_fields: tuple[str, ...] = ()
    old_name: str | None = None
    new_name: str | None = None
    old_price: int | None = None
    new_price: int | None = None
    price_delta: int | None = None
    price_delta_pct: float | None = None
    old_stock: int | None = None
    new_stock: int | None = None
    stock_changed: bool = False
    match_status: str | None = None
    match_method: str | None = None
    match_confidence: float | None = None
    matched_product_id: str | None = None
    candidates: tuple[dict[str, Any], ...] = ()
    reason_codes: tuple[str, ...] = ()
    needs_review: bool = False
    recoding: bool = False
    row_error: bool = False
    returning: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Машинный контракт: коды, списки вместо кортежей."""
        data = asdict(self)
        data["state"] = str(self.state)
        data["diff_status"] = str(self.diff_status)
        data["price_status"] = str(self.price_status)
        data["changed_fields"] = list(self.changed_fields)
        data["candidates"] = [dict(candidate) for candidate in self.candidates]
        data["reason_codes"] = list(self.reason_codes)
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DiffRow:
        known = {f.name for f in fields(cls)}
        data = {key: value for key, value in raw.items() if key in known}
        return cls(
            **{
                **data,
                "state": CodeState(data["state"]),
                "diff_status": DiffStatus(data["diff_status"]),
                "price_status": PriceStatus(data["price_status"]),
                "changed_fields": tuple(data.get("changed_fields") or ()),
                "candidates": tuple(data.get("candidates") or ()),
                "reason_codes": tuple(data.get("reason_codes") or ()),
                "stock_changed": bool(data.get("stock_changed")),
                "needs_review": bool(data.get("needs_review")),
                "recoding": bool(data.get("recoding")),
                "row_error": bool(data.get("row_error")),
                "returning": bool(data.get("returning")),
            }
        )


@dataclass(frozen=True)
class DiffCounters:
    # Товаров в текущем каталоге — база для доли исчезнувших.
    current_products: int = 0
    # Кодов, которые есть и в каталоге, и в файле, — база для доли смен цены.
    existing: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    # Новый код с одним кандидатом среди исчезнувших.
    recoding: int = 0
    ambiguous: int = 0
    row_errors: int = 0
    needs_review: int = 0
    returning: int = 0
    price_changed: int = 0
    price_increased: int = 0
    price_decreased: int = 0
    stock_changed: int = 0

    @property
    def removed_share(self) -> float:
        return self.missing / self.current_products if self.current_products else 0.0

    @property
    def price_changed_share(self) -> float:
        return self.price_changed / self.existing if self.existing else 0.0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> DiffCounters:
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in (raw or {}).items() if key in known})

    @classmethod
    def of(cls, rows: Iterable[DiffRow], current_products: int) -> DiffCounters:
        rows = list(rows)
        existing = [row for row in rows if row.state is CodeState.EXISTING]
        priced = [row for row in existing if not row.row_error]
        return cls(
            current_products=current_products,
            existing=len(existing),
            new=sum(row.diff_status is DiffStatus.NEW for row in rows),
            updated=sum(row.diff_status is DiffStatus.UPDATED for row in rows),
            unchanged=sum(row.diff_status is DiffStatus.UNCHANGED for row in rows),
            missing=sum(row.diff_status is DiffStatus.REMOVED for row in rows),
            recoding=sum(row.recoding for row in rows),
            ambiguous=sum(row.diff_status is DiffStatus.AMBIGUOUS for row in rows),
            row_errors=sum(row.row_error for row in rows),
            needs_review=sum(row.needs_review for row in rows),
            returning=sum(row.returning for row in rows),
            price_changed=sum(row.price_status is not PriceStatus.UNCHANGED for row in priced),
            price_increased=sum(row.price_status is PriceStatus.INCREASED for row in priced),
            price_decreased=sum(row.price_status is PriceStatus.DECREASED for row in priced),
            stock_changed=sum(row.stock_changed for row in priced),
        )


@dataclass(frozen=True)
class CatalogDiff:
    base_version: str
    base_sha256: str
    registry_sha256: str | None
    rows: tuple[DiffRow, ...]
    counters: DiffCounters
    fingerprint: str
    comparison: CatalogComparison
    # Карточки каталога после импорта — из них собирается снимок при утверждении.
    candidate: list[Record] = field(default_factory=list, repr=False, compare=False)


def compute_diff(
    items: Sequence[ImportItem],
    file_codes: Iterable[str],
    snapshot: CatalogSnapshot,
    *,
    registry: Mapping[str, list[dict[str, str]]] | None = None,
    registry_sha256: str | None = None,
    match_settings: MatchSettings | None = None,
    returning: Callable[[Iterable[str]], set[str]] | None = None,
    current_records: Sequence[Record] | None = None,
    matcher_factory: Callable[..., CatalogMatcher] = CatalogMatcher,
) -> CatalogDiff:
    """Diff товаров импорта против снимка.

    `file_codes` — все коды файла, включая исключённые ошибками: код с ошибкой не
    считается исчезнувшим, а у существующего товара остаётся прежняя карточка.
    """
    current_records = list(current_records) if current_records is not None else snapshot.records()
    imported = {item.sku_1c for item in items}
    rejected = set(file_codes) - imported
    candidate = assemble_import(items, current_records, registry or {}, rejected)

    current = {record["sku_1c"]: record for record in current_records}
    after = {record["sku_1c"]: record for record in candidate}
    repository = ProductListRepository([Product.from_dict(record) for record in current_records])
    matching = compare_with_catalog(
        items,
        [item.sku_1c for item in items] + sorted(rejected),
        repository,
        matcher_factory(repository, match_settings),
    )
    existing = {position.new_article: position for position in matching.existing}
    fresh = {position.new_article: position for position in matching.new}
    back = returning(list(fresh)) if returning is not None and fresh else set()

    rows: list[DiffRow] = []
    for item in items:
        code = item.sku_1c
        if code in current:
            rows.append(_existing_row(code, current[code], after[code], existing[code]))
        else:
            rows.append(_new_row(code, after[code], fresh[code], code in back))
    rows += [_row_error(code, current[code]) for code in sorted(rejected) if code in current]
    rows += [_removed_row(position.old_article, current[position.old_article]) for position in matching.missing]

    rows_tuple = tuple(rows)
    return CatalogDiff(
        base_version=snapshot.base_version,
        base_sha256=snapshot.sha256,
        registry_sha256=registry_sha256,
        rows=rows_tuple,
        counters=DiffCounters.of(rows_tuple, len(current)),
        fingerprint=fingerprint(snapshot.sha256, registry_sha256, rows_tuple),
        comparison=matching.comparison,
        candidate=candidate,
    )


def fingerprint(base_sha256: str, registry_sha256: str | None, rows: Iterable[DiffRow]) -> str:
    payload = {
        "schema": FINGERPRINT_SCHEMA,
        "base_sha256": base_sha256,
        "registry_sha256": registry_sha256,
        "rows": [row.to_dict() for row in sorted(rows, key=lambda row: row.sku_1c)],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def price_change(old: int | None, new: int | None) -> tuple[PriceStatus, int | None, float | None]:
    if old is None and new is None:
        return PriceStatus.UNCHANGED, None, None
    if old is None:
        return PriceStatus.NEW, None, None
    if new is None:
        return PriceStatus.REMOVED, None, None
    delta = new - old
    status = (
        PriceStatus.UNCHANGED
        if delta == 0
        else PriceStatus.INCREASED
        if delta > 0
        else PriceStatus.DECREASED
    )
    # Процент от нулевой цены смысла не имеет.
    pct = round(delta * 100 / old, 2) if old else None
    return status, delta, pct


def _match_fields(position: CodeMatch) -> dict[str, Any]:
    raw = position.to_dict()
    return {
        "match_status": raw["match_status"],
        "match_method": raw["match_method"],
        "match_confidence": raw["confidence"],
        "matched_product_id": raw["matched_product_id"],
        "candidates": tuple(raw["candidates"]),
        "reason_codes": tuple(raw["reason_codes"]),
    }


def _existing_row(code: str, before: Record, after: Record, position: CodeMatch) -> DiffRow:
    changed = changed_fields(before, after)
    price_status, delta, pct = price_change(before.get("price"), after.get("price"))
    old_stock, new_stock = before.get("in_stock"), after.get("in_stock")
    match = position.match
    return DiffRow(
        sku_1c=code,
        state=CodeState.EXISTING,
        diff_status=DiffStatus.UPDATED if changed else DiffStatus.UNCHANGED,
        price_status=price_status,
        changed_fields=changed,
        old_name=before.get("name"),
        new_name=after.get("name"),
        old_price=before.get("price"),
        new_price=after.get("price"),
        price_delta=delta,
        price_delta_pct=pct,
        old_stock=old_stock,
        new_stock=new_stock,
        stock_changed=old_stock != new_stock,
        needs_review=match is not None and match.status is MatchStatus.MATCHED_REVIEW,
        **_match_fields(position),
    )


def _new_row(code: str, after: Record, position: CodeMatch, returning: bool) -> DiffRow:
    match = position.match
    ambiguous = match is not None and match.status is MatchStatus.AMBIGUOUS
    return DiffRow(
        sku_1c=code,
        state=CodeState.NEW,
        diff_status=DiffStatus.AMBIGUOUS if ambiguous else DiffStatus.NEW,
        price_status=PriceStatus.NEW,
        new_name=after.get("name"),
        new_price=after.get("price"),
        new_stock=after.get("in_stock"),
        recoding=position.is_recoding_candidate and not ambiguous,
        returning=returning,
        **_match_fields(position),
    )


def _row_error(code: str, current: Record) -> DiffRow:
    return DiffRow(
        sku_1c=code,
        state=CodeState.EXISTING,
        diff_status=DiffStatus.UNCHANGED,
        price_status=PriceStatus.UNCHANGED,
        old_name=current.get("name"),
        new_name=current.get("name"),
        old_price=current.get("price"),
        new_price=current.get("price"),
        price_delta=0 if current.get("price") is not None else None,
        price_delta_pct=0.0 if current.get("price") else None,
        old_stock=current.get("in_stock"),
        new_stock=current.get("in_stock"),
        row_error=True,
    )


def _removed_row(code: str, current: Record) -> DiffRow:
    return DiffRow(
        sku_1c=code,
        state=CodeState.MISSING,
        diff_status=DiffStatus.REMOVED,
        price_status=PriceStatus.REMOVED,
        old_name=current.get("name"),
        old_price=current.get("price"),
        old_stock=current.get("in_stock"),
    )


# --- Просмотр ------------------------------------------------------------------

DIFF_FILTERS: dict[str, tuple[str, Callable[[DiffRow], bool]]] = {
    "price-up": ("цена выросла", lambda row: row.price_status is PriceStatus.INCREASED),
    "price-down": ("цена снизилась", lambda row: row.price_status is PriceStatus.DECREASED),
    "new": (
        "новые товары",
        lambda row: row.diff_status in (DiffStatus.NEW, DiffStatus.AMBIGUOUS),
    ),
    "removed": ("исчезли из файла", lambda row: row.diff_status is DiffStatus.REMOVED),
    "updated": ("изменились", lambda row: row.diff_status is DiffStatus.UPDATED),
    "stock": ("изменился остаток", lambda row: row.stock_changed),
    "review": (
        "требуют проверки менеджера",
        lambda row: row.needs_review or row.recoding or row.diff_status is DiffStatus.AMBIGUOUS,
    ),
    "recoding": (
        "возможная перекодировка",
        lambda row: row.recoding or row.diff_status is DiffStatus.AMBIGUOUS,
    ),
    "errors": ("строка с ошибкой, оставлена прежняя карточка", lambda row: row.row_error),
}


def filter_rows(rows: Iterable[DiffRow], name: str | None) -> list[DiffRow]:
    if not name:
        return [row for row in rows if row.diff_status is not DiffStatus.UNCHANGED or row.row_error]
    return [row for row in rows if DIFF_FILTERS[name][1](row)]


def format_diff(
    record: CatalogImport, rows: Sequence[DiffRow], filter_name: str | None = None, limit: int = 30
) -> str:
    """Изменения импорта по позициям — только сохранённые числа."""
    counters = DiffCounters.from_dict(record.summary.diff)
    lines = [f"Diff импорта {record.id} — {record.status}"]
    if record.summary.diff is None or record.diff_fingerprint is None:
        lines.append(
            "Diff не сохранён: импорт загружен до EPIC 4 или сравнивать было не с чем. "
            f"Пересчитать: python run.py import-1c --rediff {record.id}"
        )
        return "\n".join(lines)
    lines += [
        f"Против версии каталога: {record.base_version}",
        f"Отпечаток diff: {record.diff_fingerprint}",
        f"Товаров в текущем каталоге: {_n(counters.current_products)}",
        f"NEW {_n(counters.new)} · UPDATED {_n(counters.updated)} · "
        f"UNCHANGED {_n(counters.unchanged)} · MISSING {_n(counters.missing)} · "
        f"RECODING {_n(counters.recoding)} · AMBIGUOUS {_n(counters.ambiguous)}",
        f"Цена изменилась у {_n(counters.price_changed)} из {_n(counters.existing)} "
        f"({counters.price_changed_share:.1%}): выросла {_n(counters.price_increased)}, "
        f"снизилась {_n(counters.price_decreased)}; остаток изменился у {_n(counters.stock_changed)}",
        f"Исчезает {counters.removed_share:.1%} товаров текущего каталога; "
        f"строк с ошибкой {_n(counters.row_errors)}, на проверку {_n(counters.needs_review)}, "
        f"вернувшихся кодов {_n(counters.returning)}",
    ]
    shown = filter_rows(rows, filter_name)
    title = DIFF_FILTERS[filter_name][0] if filter_name else "все изменения"
    lines.append(f"Позиции — {title}: {_n(len(shown))}" + (f", показано {limit}" if len(shown) > limit else ""))
    lines += [f"  {row_line(row)}" for row in shown[:limit]]
    return "\n".join(lines)


def row_line(row: DiffRow) -> str:
    name = row.new_name or row.old_name or ""
    parts = [f"{row.sku_1c} {row.diff_status}: {name[:60]}"]
    if row.price_status is not PriceStatus.UNCHANGED or row.diff_status is DiffStatus.NEW:
        pct = f" ({row.price_delta_pct:+.2f} %)" if row.price_delta_pct is not None else ""
        parts.append(f"цена {_price(row.old_price)} → {_price(row.new_price)}{pct} [{row.price_status}]")
    if row.stock_changed:
        parts.append(f"остаток {_stock(row.old_stock)} → {_stock(row.new_stock)}")
    if row.changed_fields:
        parts.append("поля: " + ", ".join(FIELD_LABELS.get(name, name) for name in row.changed_fields))
    flags = [
        label
        for flag, label in (
            (row.needs_review, "на проверку"),
            (row.recoding, "возможная перекодировка"),
            (row.row_error, "строка с ошибкой — прежняя карточка"),
            (row.returning, "код возвращается"),
        )
        if flag
    ]
    if row.candidates:
        flags.append(
            "кандидаты: "
            + "; ".join(f"{c.get('product_id')} {str(c.get('name', ''))[:40]}" for c in row.candidates)
        )
    if flags:
        parts.append(", ".join(flags))
    return " · ".join(parts)


def _price(value: int | None) -> str:
    return "—" if value is None else f"{_n(value)} ₽"


def _stock(value: int | None) -> str:
    return "неизвестно" if value is None else _n(value)


def _n(value: int) -> str:
    return f"{value:,}".replace(",", " ")
