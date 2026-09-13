"""Сборка базы знаний из выгрузки 1С.

Вход — XLSX-выгрузка заказчика (обновляется 2–3 раза в месяц), выход — products.jsonl
и отчёт о покрытии. Загрузка идемпотентна: один и тот же файл даёт один и тот же результат.

Разбор строк общий с импортом на проверку — `catalog_import/parser.py` (D9): здесь
только сборка базы знаний из разобранного. Эта команда по-прежнему сразу
перезаписывает базу знаний бота; импорт с проверкой — `run.py import-1c`.

    python -m ingest.build_kb --source data/raw/Pricelist20260826.xlsx
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from catalog.current import replace_with_retry
from catalog_import.parser import EXPECTED_HEADERS as EXPECTED_HEADERS
from catalog_import.parser import ParsedProduct as Product
from catalog_import.parser import parse_products, read_bitrix_ids
from catalog_versions.cards import apply_registry, carry_over_collected, serialize
from ingest import norm_registry
from ingest.xlsx_reader import XlsxFile
from media.sync import collected_in_kb


@dataclass
class Report:
    source_file: str
    generated_at: str
    rows_with_product: int = 0
    products: int = 0
    cross_listed: int = 0
    with_price: int = 0
    with_stock: int = 0
    stock_unknown: int = 0
    with_description: int = 0
    with_kit_contents: int = 0
    with_bitrix_id: int = 0
    with_images: int = 0
    with_attributes: int = 0
    with_norms: int = 0
    with_norm_item_code: int = 0
    from_registry: int = 0
    headings: int = 0
    roots: dict[str, int] = field(default_factory=dict)
    norm_documents: dict[str, int] = field(default_factory=dict)
    norm_sources: dict[str, int] = field(default_factory=dict)
    norm_anomalies: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


def build(source: Path, out_dir: Path, previous: Path | None = None) -> Report:
    """Собрать `products.jsonl` в `out_dir`.

    `previous` — каталог, из которого переносятся фото и характеристики. По умолчанию —
    прежний файл в `out_dir`; при версиях каталога команда передаёт текущий снимок.
    Карточки собираются теми же функциями, что снимок версии (`catalog_versions/cards.py`).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    report = Report(source_file=source.name, generated_at=now)

    with XlsxFile(source) as book:
        bitrix_ids = _read_bitrix_ids(book)
        products = list(_read_products(book, bitrix_ids, now, report))

    kb_file = out_dir / "products.jsonl"
    records = [asdict(product) for product in products]
    report.from_registry += apply_registry(
        records, norm_registry.load(out_dir / norm_registry.DEFAULT_REGISTRY.name)
    )
    carry_over_collected(records, collected_in_kb(previous or kb_file))
    products = [Product(**record) for record in records]
    _fill_report(report, products)
    _write_jsonl(kb_file, records)
    (out_dir / "report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


# --- Чтение листов ---------------------------------------------------------------


def _read_products(
    book: XlsxFile,
    bitrix_ids: dict[str, int],
    now: str,
    report: Report,
) -> list[Product]:
    sheet = parse_products(
        book.numbered_rows(0), bitrix_ids=bitrix_ids, now=now, source_name=report.source_file
    )
    if sheet.header_error:
        raise ValueError(sheet.header_error)

    report.headings += len(sheet.headings)
    report.rows_with_product += len(sheet.product_rows)
    report.norm_anomalies.extend(sheet.norm_anomalies)
    if sheet.unnumbered_roots:
        report.open_questions.append(
            "Нумерация разделов не подтверждена номером документа, привязка не проставлена: "
            + "; ".join(sorted(sheet.unnumbered_roots))
            + ". Уточнить у заказчика, какому перечню она соответствует."
        )
    return sheet.products


def _read_bitrix_ids(book: XlsxFile) -> dict[str, int]:
    """Второй лист выгрузки: ID элемента Битрикса по наименованию."""
    if len(book.sheet_names) < 2:
        return {}
    return read_bitrix_ids(book.numbered_rows(1)).by_name


# --- Отчёт -----------------------------------------------------------------------


def _fill_report(report: Report, products: list[Product]) -> None:
    roots: Counter[str] = Counter()
    by_doc: Counter[str] = Counter()
    by_source: Counter[str] = Counter()

    for product in products:
        report.products += 1
        report.cross_listed += len(product.category_paths) > 1
        report.with_price += product.price is not None
        report.with_stock += (product.in_stock or 0) > 0
        report.stock_unknown += product.in_stock is None
        report.with_description += bool(product.description)
        report.with_kit_contents += bool(product.kit_contents)
        report.with_bitrix_id += product.bitrix_id is not None
        report.with_images += bool(product.images)
        report.with_attributes += bool(product.attributes)
        if product.norms:
            report.with_norms += 1
        if any(norm["item_code"] for norm in product.norms):
            report.with_norm_item_code += 1
        for path in product.category_paths:
            if path:
                roots[path[0]] += 1
        for norm in product.norms:
            by_doc[norm["doc_id"]] += 1
            by_source[norm["source"]] += 1

    report.roots = dict(roots.most_common())
    report.norm_documents = dict(by_doc.most_common())
    report.norm_sources = dict(by_source.most_common())
    report.norm_anomalies = report.norm_anomalies[:50]
    if report.with_price < report.products:
        report.open_questions.append(
            f"Без цены {report.products - report.with_price} позиций. "
            "Что бот отвечает по ним: «цена по запросу» или скрывать из выдачи?"
        )


def _carry_over_collected(products: list[Product], previous: Path) -> None:
    """Сохраняет собранное с сайта до этой пересборки: фотографии и характеристики.

    Выгрузка приходит два-три раза в месяц, а с сайта всё берётся отдельным
    проходом длиной в полтора часа. Без переноса каждая новая выгрузка обнуляла
    бы собранное, и обход пришлось бы начинать заново.
    """
    records = [asdict(product) for product in products]
    carry_over_collected(records, collected_in_kb(previous))
    for product, record in zip(products, records, strict=True):
        product.images, product.attributes = record["images"], record["attributes"]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Через временный файл: прерванная запись не обрезает базу знаний."""
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as fh:
        fh.write(serialize(records))
        fh.flush()
        os.fsync(fh.fileno())
    replace_with_retry(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Сборка базы знаний из выгрузки 1С")
    parser.add_argument("--source", default="data/raw/Pricelist20260826.xlsx")
    parser.add_argument("--out", default="data/kb")
    args = parser.parse_args()

    report = build(Path(args.source), Path(args.out))
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
