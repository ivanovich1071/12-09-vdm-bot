"""Проверка пункта перечня до выдачи товаров (план 05-10, шаг 3.3).

Прогон 04.10: пункта 2.30.x в 838 не существует, но бот 11 раз показывал
случайные товары; «2.1.14 по приказу 1057» выдавал позиции по 1.13.4.3.1.7.
Одна функция для ядра и инструментов: пункта нет нигде — честный отказ с
ближайшим разделом; пункт в другом документе — называем где; товаров по
пункту нет — так и говорим, без подмены.
"""

from __future__ import annotations

from dataclasses import dataclass

from norms import documents as norm_docs

ORDERS = ("order_1057", "order_838")


@dataclass(frozen=True)
class PointCheck:
    """Результат проверки: есть ли пункт, где он найден и что сказать человеку."""

    code: str
    named_document: str | None
    # Документ, в котором пункт действительно есть (если есть).
    found_in: str | None = None
    title: str | None = None
    # Человеческий текст для случая «товаров не будет».
    note: str = ""

    @property
    def exists(self) -> bool:
        return self.found_in is not None

    @property
    def in_other_document(self) -> bool:
        """Пункт существует, но не в названном собеседником перечне."""
        return self.exists and self.named_document is not None and self.found_in != self.named_document


def check_point(index, code: str, document: str | None = None, suggestion: str = "") -> PointCheck:  # noqa: ANN001 — norms.items.ItemIndex
    """Проверка «существует ли пункт X в документе Y» по справочнику приказов."""
    code = (code or "").strip().rstrip(".")
    homes = index.documents_with(code) if code else []
    named = document or None

    if not homes:
        extra = f" {suggestion}" if suggestion else ""
        return PointCheck(
            code=code,
            named_document=named,
            note=f"Пункта {code} в приказах 838 и 1057 нет.{extra}",
        )

    item = index.get(homes[0], code)
    title = item.title if item is not None else None
    if named and named not in homes:
        other = _short(homes[0])
        heading = f' в приказе {other} это «{title}»' if title else f" он есть в приказе {other}"
        return PointCheck(
            code=code,
            named_document=named,
            found_in=homes[0],
            title=title,
            note=f"Пункта {code} в приказе {_short(named)} нет;{heading}.",
        )
    return PointCheck(code=code, named_document=named, found_in=homes[0], title=title)


def _short(doc_id: str) -> str:
    """«838», «1057» — как человек называет приказы."""
    if doc_id in norm_docs.DOCUMENTS:
        return norm_docs.get(doc_id).short_name
    return doc_id


def nearest_section(index, text: str) -> str:  # noqa: ANN001 — norms.items.ItemIndex
    """Ближайший раздел по словам реплики: «для логопеда это раздел 2.4 «Кабинет учителя-логопеда»."""
    words = " ".join(w for w in text.split() if len(w) > 3 and not w[0].isdigit())
    if not words:
        return ""
    found = index.search(words, None, limit=1)
    if not found:
        return ""
    item = found[0]
    title = getattr(item, "title", None)
    code = getattr(item, "code", None)
    return f"Похоже, вам нужен пункт {code} «{title}» — показать?" if code and title else ""
