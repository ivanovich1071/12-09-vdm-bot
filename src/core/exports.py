"""Список разговора файлом: комплектация консультанта или подобранные позиции.

14.09 на «сохрани в файл и дай скачать» бот ответил «не могу создавать файлы», хотя
спецификацию корзины он отдаёт в Excel и Word давно. Комплектация жила только текстом ответа
модели — выгружать было нечего. Теперь раздел перечня, который консультант разобрал
инструментом, лежит в профиле (`DialogProfile.kit`), и файл собирается из него и из каталога,
без модели: пункты и количество — из текста приказа, товары, цены и наличие — из каталога.

Формат выбирает человек кнопками «Скачать Excel» и «Скачать Word» (решение заказчика 14.09).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from catalog.models import Availability, Product
from core.ui import Button, Keyboard, Message, Response, plural, stock_text
from norms import documents as norm_docs

if TYPE_CHECKING:
    from core.dialog import DialogEngine, Session

EXCEL, WORD = "xlsx", "docx"
# Индексы стилей `documents/xlsx.py`: обычный, жирный, число с разрядами.
PLAIN, BOLD, NUMBER = 0, 1, 2

KIT_COLUMNS = ("Пункт", "Наименование по перечню", "Кол-во по перечню", "Товар в каталоге", "Код 1С", "Цена, ₽", "Наличие")
KIT_WIDTHS = [14, 60, 16, 60, 16, 12, 22]
LIST_COLUMNS = ("№", "Наименование", "Код 1С", "Цена, ₽", "Наличие", "Пункт перечня")
LIST_WIDTHS = [5, 70, 16, 12, 22, 18]
NOTE = "Предварительный список. Цены и наличие — по каталогу на дату выгрузки; окончательно их подтверждает менеджер."

Row = list[tuple[Any, int]]


@dataclass(frozen=True)
class ExportFile:
    filename: str
    content: bytes
    caption: str


def buttons(keyboard: Keyboard | None = None) -> Keyboard:
    return (keyboard or Keyboard()).row(
        Button("Скачать Excel", f"export:{EXCEL}"), Button("Скачать Word", f"export:{WORD}")
    )


def offer(engine: DialogEngine, session: Session) -> list[Response]:
    """Ответ на «сохрани в файл»: что будет в файле и выбор формата."""
    what = _subject(session)
    if what is None:
        text = (
            "Сохранять пока нечего: сначала соберём комплектацию или подберём позиции. "
            "Для какого помещения подбираем?"
        )
        session.remember("assistant", text)
        return [Message(text)]
    text = f"Пришлю {what} файлом. В каком виде?"
    session.remember("assistant", text)
    return [Message(text, keyboard=buttons())]


def build(engine: DialogEngine, session: Session, fmt: str) -> ExportFile | None:
    """Файл списка разговора. `None` — выгружать нечего."""
    profile = session.profile
    if _kit_first(profile):
        title, meta, header, rows, widths = _kit_table(engine, profile.kit)
    elif profile.shortlist:
        title, meta, header, rows, widths = _list_table(engine, session)
    else:
        return None
    meta = [*meta, f"Каталог: версия {engine.catalog_version or 'текущая'}, выгрузка {dt.date.today():%d.%m.%Y}"]

    if fmt == WORD:
        from documents.docx import write_document

        table = [list(header), *[[_text(value) for value, _ in row] for row in rows]]
        content = write_document(title, meta, table, "", [NOTE])
        extension = WORD
    else:
        from documents.xlsx import write_sheets

        sheet: list[Row] = [
            [(title, BOLD)],
            *[[(line, PLAIN)] for line in meta],
            [],
            [(name, BOLD) for name in header],
            *rows,
            [],
            [(NOTE, PLAIN)],
        ]
        content = write_sheets([("Список", sheet, widths)])
        extension = EXCEL
    return ExportFile(f"{_filename(title)}.{extension}", content, f"{title}. {NOTE}")


def _kit_first(profile) -> bool:  # noqa: ANN001 — core.profile.DialogProfile
    return bool(profile.kit) and (profile.export == "kit" or not profile.shortlist)


def _subject(session: Session) -> str | None:
    profile = session.profile
    if _kit_first(profile):
        kit = profile.kit or {}
        count = len(kit.get("positions") or [])
        return f"комплектацию по разделу {kit.get('code')} «{kit.get('title')}» — {count} {plural(count, 'позиция', 'позиции', 'позиций')}"
    if profile.shortlist:
        count = len(profile.shortlist)
        return f"список из {count} {plural(count, 'позиции', 'позиций', 'позиций')}"
    return None


def _kit_table(engine: DialogEngine, kit: dict[str, Any]):  # noqa: ANN202
    doc_id = kit.get("document") or ""
    doc = norm_docs.get(doc_id).short_name if doc_id in norm_docs.DOCUMENTS else doc_id
    title = f"Комплектация {kit.get('code', '')} {kit.get('title', '')}".strip()
    meta = [f"Основание: {doc}, раздел {kit.get('code')} «{kit.get('title')}»"]
    positions = kit.get("positions") or []
    codes = [str(position.get("code", "")) for position in positions]
    offers = _catalog_by_point(engine, doc_id)
    rows: list[Row] = []
    for position in positions:
        code, name = str(position.get("code", "")), str(position.get("title", ""))
        # Раздел внутри раздела — «1.13.3.1 Рабочее место педагога» — строка-заголовок группы.
        if any(other.startswith(f"{code}.") for other in codes):
            rows.append([(code, BOLD), (name, BOLD)])
            continue
        product = _best(offers.get(code, []))
        cells = _product_cells(product) if product is not None else [("в каталоге не найдено", PLAIN)]
        rows.append([(code, PLAIN), (name, PLAIN), (position.get("quantity") or "уточняется", PLAIN), *cells])
    return title, meta, KIT_COLUMNS, rows, KIT_WIDTHS


def _list_table(engine: DialogEngine, session: Session):  # noqa: ANN202
    rows: list[Row] = []
    audience = session.profile.audience
    for sku in session.profile.shortlist:
        product = engine.index.get(sku)
        if product is None:
            continue
        points = [ref.item_code for ref in product.norms_for(audience) if ref.item_code]
        name, code, price, stock = _product_cells(product)
        rows.append([(len(rows) + 1, PLAIN), name, code, price, stock, (", ".join(points[:2]), PLAIN)])
    return "Подобранные позиции", [], LIST_COLUMNS, rows, LIST_WIDTHS


def _catalog_by_point(engine: DialogEngine, doc_id: str) -> dict[str, list[Product]]:
    """Товары каталога по пунктам перечня — один проход по каталогу на файл."""
    found: dict[str, list[Product]] = {}
    for product in engine.index.products:
        if not product.is_active:
            continue
        for ref in product.norms:
            if ref.doc_id == doc_id and ref.item_code:
                found.setdefault(ref.item_code, []).append(product)
    return found


def _best(products: list[Product]) -> Product | None:
    """Сначала то, что есть в наличии и с ценой, дальше — дешевле."""
    if not products:
        return None
    return min(
        products,
        key=lambda product: (product.availability is not Availability.AVAILABLE, product.price is None, product.price or 0),
    )


def _product_cells(product: Product) -> Row:
    price = (product.price, NUMBER) if product.price is not None else ("по запросу", PLAIN)
    return [(product.name, PLAIN), (product.sku_1c, PLAIN), price, (stock_text(product), PLAIN)]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}".replace(",", " ")
    return str(value)


def _filename(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|«»]+', "", title)[:80].strip() or "Список"
