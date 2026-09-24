"""Список разговора файлом: комплектация консультанта или подобранные позиции.

14.09 на «сохрани в файл и дай скачать» бот ответил «не могу создавать файлы», хотя
спецификацию корзины он отдаёт в Excel и Word давно. Комплектация жила только текстом ответа
модели — выгружать было нечего. Теперь раздел перечня, который консультант разобрал
инструментом, лежит в профиле (`DialogProfile.kit`), и файл собирается из него и из каталога,
без модели: пункты и количество — из текста приказа, товары, цены и наличие — из каталога.

Формат выбирает человек кнопками «Скачать Excel» и «Скачать Word» (решение заказчика 14.09).

**Шапка таблицы — общая для всех файлов бота** (`ORDER_COLUMNS`): комплектация, список подбора и
спецификация предзаказа называют колонки одинаково. Это не косметика: свой же файл человек
скачивает, проставляет количество и присылает обратно, а разбор заказа узнаёт колонки по точному
названию. До 16.09 комплектация уходила с колонками «Наименование по перечню» и «Кол-во по
перечню», которых разбор не знал, — присланный обратно файл терял и названия, и количество.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from catalog.points import REGISTRY, PointFinder, PointMatch
from core.ui import Button, Keyboard, Message, Response, plural, stock_text
from norms import documents as norm_docs

if TYPE_CHECKING:
    from core.dialog import DialogEngine, Session

EXCEL, WORD = "xlsx", "docx"
# Индексы стилей `documents/xlsx.py`: обычный, жирный, число с разрядами.
PLAIN, BOLD, NUMBER = 0, 1, 2

# Единая шапка заказа: первые восемь колонок — как в спецификации предзаказа
# (`documents/templates/specification.json`), дальше — перечень, ради которого всё собирается.
ORDER_COLUMNS = (
    "№",
    "Код 1С",
    "Наименование",
    "Кол-во",
    "Ед.",
    "Цена, ₽",
    "Сумма, ₽",
    "Наличие",
    "Пункт",
    "Наименование по перечню",
    "Норма по перечню",
    "Сопоставление",
)
ORDER_WIDTHS = [5, 16, 55, 8, 6, 12, 13, 18, 12, 50, 18, 26]
# Номер колонки «Сумма, ₽» в строке: по ней считается итог файла.
AMOUNT_CELL = 6

NOTE = "Предварительный список. Цены и наличие — по каталогу на дату данных выше; окончательно их подтверждает менеджер."
FILL_NOTE = (
    "Чтобы заказать: впишите количество в колонку «Кол-во», сохраните файл и пришлите его боту — "
    "он пересчитает цены по текущему каталогу и соберёт предзаказ. Остальные колонки не меняйте: "
    "по коду 1С и пункту перечня бот узнаёт позицию."
)
NOT_IN_CATALOG = "нет в каталоге"

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


def ready(session: Session) -> bool:
    """Есть ли что выгружать: список подбора, комплектация или присланный заказ."""
    return _subject(session) is not None


def offer(engine: DialogEngine, session: Session) -> list[Response] | None:
    """Ответ на «сохрани в файл»: что будет в файле и выбор формата. `None` — выгружать нечего.

    Раньше на пустом месте отвечали «Сохранять пока нечего: сначала соберём комплектацию».
    Ночью 15.09 это пришло на «нужна спецификация в Excel и счёт» в 11 диалогах из 25, и разговор
    кончался: спецификацию просят как раз тогда, когда список ещё не собран. Теперь отвечает агент.
    """
    what = _subject(session)
    if what is None:
        return None
    text = f"Пришлю {what} файлом. В каком виде?"
    session.remember("assistant", text)
    return [Message(text, keyboard=buttons())]


def build(engine: DialogEngine, session: Session, fmt: str) -> ExportFile | None:
    """Файл списка разговора. `None` — выгружать нечего."""
    profile = session.profile
    if _kit_first(profile):
        title, meta, rows = _kit_table(engine, session, profile.kit)
    elif profile.shortlist or profile.offered:
        # Обычный поиск не собирает ни кита, ни shortlist — только запоминает показанное
        # (`remember_offered`). После такого диалога кнопка «Скачать» говорила «Сохранять
        # пока нечего» (23.09, сц. 4, 10, 14, 19, 22) — выгружаем показанные позиции.
        title, meta, rows = _list_table(engine, session)
    else:
        return None
    # «Выгрузка 15.09» читалась как дата данных, а данные каталога были на 27.08 (разбор 15.09).
    stamp = _data_date(engine)
    meta = [
        *meta,
        f"Каталог: версия {engine.catalog_version or 'текущая'}"
        + (f", данные на {stamp}" if stamp else "")
        + f"; файл сформирован {dt.date.today():%d.%m.%Y}",
    ]
    amount = _amount(rows)
    if amount:
        rows = [*rows, _totals_row(amount)]
    header = list(ORDER_COLUMNS)

    if fmt == WORD:
        from documents.docx import write_document

        table = [header, *[_row_text(row) for row in rows]]
        content = write_document(title, meta, table, "", [FILL_NOTE, NOTE])
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
            [(FILL_NOTE, PLAIN)],
            [(NOTE, PLAIN)],
        ]
        content = write_sheets([("Заказ", sheet, ORDER_WIDTHS)])
        extension = EXCEL
    return ExportFile(f"{_filename(title)}.{extension}", content, f"{title}. {FILL_NOTE}")


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
    if profile.offered:
        count = len(profile.offered)
        return f"список из {count} {plural(count, 'позиции', 'позиций', 'позиций')} последнего подбора"
    return None


def _kit_table(engine: DialogEngine, session: Session, kit: dict[str, Any]):  # noqa: ANN202
    doc_id = kit.get("document") or ""
    doc = norm_docs.get(doc_id).short_name if doc_id in norm_docs.DOCUMENTS else doc_id
    title = f"Комплектация {kit.get('code', '')} {kit.get('title', '')}".strip()
    meta = [f"Основание: {doc}, раздел {kit.get('code')} «{kit.get('title')}»"]
    positions = kit.get("positions") or []
    codes = [str(position.get("code", "")) for position in positions]
    finder = PointFinder(
        engine.index, engine.norm_texts, (doc_id,) if doc_id else (), session.profile.audience
    )
    rows: list[Row] = []
    number = 0
    for position in positions:
        code, name = str(position.get("code", "")), str(position.get("title", ""))
        # Раздел внутри раздела — «1.13.3.1 Рабочее место педагога» — строка-заголовок группы.
        # Наименование у неё пустое: разбор присланного файла такую строку позицией не считает.
        if any(other.startswith(f"{code}.") for other in codes):
            rows.append(_group_row(code, name))
            continue
        number += 1
        norm = " ".join(str(position.get("quantity") or "").split())
        rows.append(_order_row(number, finder.find(code, name), code, name, norm))
    return title, meta, rows


def _list_table(engine: DialogEngine, session: Session):  # noqa: ANN202
    """Подобранные позиции: пункт и формулировка — из оснований самого товара."""
    rows: list[Row] = []
    profile = session.profile
    finder = engine.point_finder(session)
    quantities = _quantities(engine, session)
    skus = profile.shortlist or profile.offered
    for sku in skus:
        product = engine.index.get(sku)
        if product is None:
            continue
        points = [ref.item_code for ref in product.norms_for(profile.audience) if ref.item_code]
        code = points[0] if points else ""
        rows.append(
            _order_row(
                len(rows) + 1,
                PointMatch(code, product, REGISTRY) if code else None,
                code,
                finder.title(code),
                finder.norm_quantity(code),
                quantity=_count(quantities.get(product.id) or quantities.get(product.sku_1c)),
                product=product,
            )
        )
    return "Подобранные позиции", [], rows


def _order_row(  # noqa: ANN202
    number: int,
    match: PointMatch | None,
    code: str,
    norm_title: str,
    norm_quantity: str,
    quantity: int | None = None,
    product=None,  # noqa: ANN001 — catalog.models.Product
) -> Row:
    """Строка единой таблицы заказа. Количество — заказанное, иначе норма перечня числом."""
    found = product if product is not None else (match.product if match is not None else None)
    count = quantity if quantity is not None else _count(norm_quantity)
    price = found.price if found is not None else None
    return [
        (number, PLAIN),
        (found.sku_1c if found is not None else "", PLAIN),
        (found.name if found is not None else norm_title, PLAIN),
        (count, NUMBER) if count else ("", PLAIN),
        (_unit(norm_quantity), PLAIN),
        (price, NUMBER) if price is not None else ("по запросу", PLAIN),
        (price * count, NUMBER) if price is not None and count else ("", PLAIN),
        (stock_text(found) if found is not None else "", PLAIN),
        (code, PLAIN),
        (norm_title, PLAIN),
        (norm_quantity, PLAIN),
        (match.label if match is not None else NOT_IN_CATALOG, PLAIN),
    ]


def _group_row(code: str, name: str) -> Row:
    return [("", PLAIN)] * 8 + [(code, BOLD), (name, BOLD)]


def _totals_row(amount: int) -> Row:
    return [("", PLAIN), ("", PLAIN), ("Итого", BOLD), ("", PLAIN), ("", PLAIN), ("", PLAIN), (amount, NUMBER)]


def _amount(rows: list[Row]) -> int:
    total = 0
    for row in rows:
        value = row[AMOUNT_CELL][0] if len(row) > AMOUNT_CELL else None
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def _count(value: Any) -> int | None:
    """Количество числом: «2 Шт.» → 2, «По количеству детей в группе» → числа нет."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value or None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    match = re.match(r"\s*(\d{1,4})\b", str(value))
    return (int(match.group(1)) or None) if match else None


def _unit(norm_quantity: str) -> str:
    """Единица из нормы перечня: «2 Шт.» → «шт.». Пусто и непонятное — штуки."""
    match = re.search(r"([А-Яа-яA-Za-z]+\.?)\s*$", (norm_quantity or "").strip())
    unit = match.group(1).lower() if match else ""
    return unit if unit.startswith(("шт", "компл", "набор", "пар")) else "шт."


def _quantities(engine: DialogEngine, session: Session) -> dict[str, Any]:
    """Количество позиции списка: из присланного заказа, иначе из корзины."""
    found: dict[str, Any] = {item.sku_1c: item.quantity for item in engine.storage.load_cart(session.user_id).items}
    for position in (session.profile.order or {}).get("positions") or []:
        quantity = position.get("quantity")
        if position.get("sku") and quantity is not None:
            found[position["sku"]] = int(quantity) if isinstance(quantity, float) and quantity.is_integer() else quantity
    return found


def _data_date(engine: DialogEngine) -> str | None:
    """Дата данных каталога — самое свежее обновление товара, а не день выгрузки файла."""
    stamps = [product.updated_at for product in engine.index.products if product.updated_at]
    try:
        return dt.date.fromisoformat(max(stamps)[:10]).strftime("%d.%m.%Y") if stamps else None
    except ValueError:
        return None


def _row_text(row: Row) -> list[str]:
    """Строка для Word: все колонки, пустые в том числе, — иначе таблица разъезжается."""
    values = [_text(value) for value, _ in row]
    return values + [""] * (len(ORDER_COLUMNS) - len(values))


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}".replace(",", " ")
    return str(value)


def _filename(title: str) -> str:
    clean = re.sub(r'[\\/:*?"<>|«»]+', "", title).strip()
    if len(clean) > 60:
        # По границе слова: «…по_высотестул_у.xlsx» из сц. 29 — обрыв посреди склейки.
        space = clean[:60].rfind(" ")
        clean = clean[:space] if space > 30 else clean[:60]
    return clean.rstrip(" ,;-") or "Список"
