"""Проверка разобранной выгрузки (docs/DECISIONS.md, D9, решение C).

- Ошибка файла делает импорт `INVALID`: не те колонки, нет ни одной строки товара.
- Ошибка строки исключает товар из импорта: без кода его не сопоставить, а при
  отрицательной цене или двух разных ценах на один код неясно, что верно.
- Предупреждение товар оставляет: цена по запросу, наличие неизвестно.

В реальной выгрузке от 26.08.2026 ошибок строк нет, без цены — 32 позиции.
"""

from __future__ import annotations

import re
from collections import defaultdict

from catalog_import.models import Issue, Severity
from catalog_import.parser import BitrixIds, ParsedSheet, RowFact, normalize_name, to_int

ISSUE_LABELS = {
    "unreadable_file": "файл не читается",
    "no_sheets": "нет листов",
    "header_mismatch": "колонки не совпадают",
    "no_products": "нет строк товаров",
    "missing_code": "нет кода 1С",
    "code_without_name": "товар без наименования",
    "conflicting_rows": "один код — разные данные",
    "invalid_price": "цена не число",
    "negative_price": "отрицательная цена",
    "invalid_stock": "остаток не число",
    "negative_stock": "отрицательный остаток",
    "missing_price": "нет цены",
    "missing_stock": "нет остатка",
    "fractional_price": "дробная цена",
    "fractional_stock": "дробный остаток",
    "no_section": "товар вне разделов",
    "ambiguous_bitrix_id": "неоднозначный ID Битрикса",
}

# Раздел одним словом с цифрой: «0Э-00002542», «7513». Названия разделов в
# выгрузке всегда из нескольких слов («12.04 Мячи»), а такая строка — товар,
# у которого потерялось наименование. Разбор прочитал бы её как раздел молча.
_CODE_LIKE = re.compile(r"^(?=\S*\d)[0-9A-Za-zА-ЯЁа-яё]+(?:-[0-9A-Za-zА-ЯЁа-яё]+)*$")
_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?$")

_VALUE_TEXT = {
    "price": {
        "label": "Цена",
        "negative": "отрицательная",
        "fractional": "дробная",
        "missing": "Цена не указана — покупателю будет «цена по запросу».",
    },
    "stock": {
        "label": "Остаток",
        "negative": "отрицательный",
        "fractional": "дробный",
        "missing": "Остаток не указан — наличие неизвестно (UNKNOWN).",
    },
}


def file_issues(sheet: ParsedSheet) -> list[Issue]:
    """Проблемы, при которых импортировать нечего."""
    if sheet.header_error:
        return [Issue(Severity.ERROR, "header_mismatch", sheet.header_error, row_number=1)]
    if not sheet.product_rows:
        return [Issue(Severity.ERROR, "no_products", "В выгрузке нет ни одной строки товара.")]
    return []


def row_issues(sheet: ParsedSheet, bitrix: BitrixIds) -> list[Issue]:
    issues: list[Issue] = []

    for heading in sheet.headings:
        if _CODE_LIKE.match(heading.title):
            issues.append(
                Issue(
                    Severity.ERROR,
                    "code_without_name",
                    f"Строка «{heading.title}» похожа на товар без наименования, "
                    "а прочитана как раздел каталога.",
                    heading.row_number,
                    "B",
                )
            )

    by_code: dict[str, list[RowFact]] = defaultdict(list)
    for fact in sheet.product_rows:
        if not fact.code:
            issues.append(
                Issue(
                    Severity.ERROR,
                    "missing_code",
                    f"У товара «{fact.name}» нет кода 1С — без кода его не сопоставить с каталогом.",
                    fact.row_number,
                    "A",
                )
            )
            continue
        by_code[fact.code].append(fact)
        issues.extend(_value_issues(fact, "D", fact.price, "price"))
        issues.extend(_value_issues(fact, "E", fact.stock, "stock"))
        if not fact.has_section:
            issues.append(
                Issue(
                    Severity.WARNING,
                    "no_section",
                    "Товар стоит до первого раздела каталога: учреждение и кабинет не определить.",
                    fact.row_number,
                    "A",
                    fact.code,
                )
            )

    for code, facts in by_code.items():
        first = facts[0]
        for fact in facts[1:]:
            if _signature(fact) != _signature(first):
                issues.append(
                    Issue(
                        Severity.ERROR,
                        "conflicting_rows",
                        f"Код {code} уже был в строке {first.row_number} с другим названием, "
                        "ценой или остатком — неясно, какая строка верна.",
                        fact.row_number,
                        None,
                        code,
                    )
                )

    for name, entries in bitrix.ambiguous.items():
        ids = ", ".join(str(bitrix_id) for _, bitrix_id in entries)
        issues.append(
            Issue(
                Severity.WARNING,
                "ambiguous_bitrix_id",
                f"Наименованию «{name}» соответствуют ID Битрикса {ids}: связь с сайтом не поставлена.",
                entries[0][0],
                "A",
                sheet=2,
            )
        )
    return issues


def _value_issues(fact: RowFact, column: str, raw: str, kind: str) -> list[Issue]:
    text = _VALUE_TEXT[kind]

    def issue(severity: Severity, code: str, message: str) -> list[Issue]:
        return [Issue(severity, code, message, fact.row_number, column, fact.code)]

    if not raw:
        return issue(Severity.WARNING, f"missing_{kind}", text["missing"])
    compact = raw.replace("\xa0", "").replace(" ", "")
    if not _NUMBER.match(compact):
        return issue(Severity.ERROR, f"invalid_{kind}", f"{text['label']} не число: «{raw}».")
    value = float(compact.replace(",", "."))
    if value < 0:
        return issue(Severity.ERROR, f"negative_{kind}", f"{text['label']} {text['negative']}: {raw}.")
    if value != int(value):
        return issue(
            Severity.WARNING,
            f"fractional_{kind}",
            f"{text['label']} {text['fractional']}: {_plain(value)} — округлено до {to_int(raw)}.",
        )
    return []


def _plain(value: float) -> str:
    """Число для человека: Excel хранит 270,6 как `270.60000000000002`."""
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _signature(fact: RowFact) -> tuple[str, int | None, int | None]:
    return normalize_name(fact.name), to_int(fact.price), to_int(fact.stock)
