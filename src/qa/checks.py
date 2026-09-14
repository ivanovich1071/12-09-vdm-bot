"""Проверки ответа бота по фактам — без модели.

Цена и код 1С сверяются с каталогом той версии, что лежит у бота; пункт перечня — со справочником
приказов. Карточка несёт «Код 1С: …», список — строки «N. Название — цена — наличие».
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from qa.models import Finding, Turn

SLOW_WARNING, SLOW_ERROR = 60.0, 180.0

LABELS = {
    "NO_REPLY": "нет ответа",
    "SLOW": "долгий ответ",
    "ERROR_TEXT": "текст ошибки",
    "MARKDOWN": "разметка markdown в ответе",
    "UNKNOWN_SKU": "код 1С не из каталога",
    "PRICE_MISMATCH": "цена не совпадает с каталогом",
    "UNKNOWN_POINT": "пункта нет в приказах",
    "FILE_MISSING": "файл не пришёл",
    "DUPLICATE": "одинаковые сообщения в одном ходе",
    "REPEAT": "повтор прошлого ответа",
}

_PRICE = re.compile(r"(\d{1,3}(?:[   ]\d{3})+|\d+)\s*₽")
_SKU = re.compile(r"Код 1С:\s*(\S+)")
_POINT = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d{1,3}){2,6})(?!\.?\d)")
_LIST_LINE = re.compile(r"^\s*\d{1,3}\.\s+(.+?)\s+—\s+(.+)$", re.MULTILINE)
_ERROR = re.compile(
    r"произошла\s+ошибка|что-то\s+пошло\s+не\s+так|попробуйте\s+(?:ещё\s+раз\s+)?позже|внутренняя\s+ошибка|"
    r"traceback|exception",
    re.IGNORECASE,
)
_MARKDOWN = re.compile(r"\*\*|^#{1,4}\s|^\s*\|.*\|\s*$", re.MULTILINE)


class CatalogFacts:
    def __init__(self, products: Iterable, points: Iterable[str] = ()) -> None:  # noqa: ANN001 — catalog.models.Product
        products = list(products)
        self.by_sku = {product.sku_1c: product for product in products}
        by_name: dict[str, list] = defaultdict(list)
        for product in products:
            by_name[_norm(product.name)].append(product)
        # Одинаковые названия у разных кодов цену по строке списка не проверяют.
        self.by_name = {name: found[0] for name, found in by_name.items() if len(found) == 1}
        self.points = set(points)

    @classmethod
    def load(cls, settings) -> CatalogFacts:  # noqa: ANN001 — core.config.Settings
        from catalog.runtime import CatalogRuntime
        from norms import items as norm_items

        state = CatalogRuntime.open(settings.kb_path).state
        path = Path(settings.norm_items_path)
        documents = norm_items.load(path) if path.exists() else {}
        return cls(state.index.products, {code for by_code in documents.values() for code in by_code})


def check_turn(turn: Turn, facts: CatalogFacts, history: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    if not turn.messages:
        findings.append(Finding("NO_REPLY", "error", f"бот не ответил за {turn.seconds:.0f} с"))
    elif turn.seconds > SLOW_ERROR:
        findings.append(Finding("SLOW", "error", f"первый ответ через {turn.seconds:.0f} с"))
    elif turn.seconds > SLOW_WARNING:
        findings.append(Finding("SLOW", "warning", f"первый ответ через {turn.seconds:.0f} с"))

    for message in turn.messages:
        text = message.text
        if error := _ERROR.search(text):
            findings.append(Finding("ERROR_TEXT", "error", _around(text, error.start())))
        if _MARKDOWN.search(text):
            findings.append(Finding("MARKDOWN", "warning", "в тексте **, # или таблица с «|»"))
        findings += _prices(text, facts)
        if facts.points:
            for code in _POINT.findall(text):
                if code not in facts.points:
                    findings.append(Finding("UNKNOWN_POINT", "warning", f"пункт {code}"))

    if turn.kind == "button" and turn.text.lower().startswith("скачать") and not any(m.file for m in turn.messages):
        findings.append(Finding("FILE_MISSING", "error", f"после «{turn.text}» файла нет"))

    seen = [_norm(message.text)[:300] for message in turn.messages if message.text.strip()]
    if len(set(seen)) < len(seen):
        findings.append(Finding("DUPLICATE", "warning", "бот прислал одно и то же несколько раз"))
    earlier = {_norm(text)[:300] for text in history if text.strip()}
    if any(text in earlier for text in seen):
        findings.append(Finding("REPEAT", "warning", "ответ слово в слово повторяет прошлый"))
    return _unique(findings)


def _prices(text: str, facts: CatalogFacts) -> list[Finding]:
    found: list[Finding] = []
    sku = _SKU.search(text)
    if sku:
        product = facts.by_sku.get(sku.group(1))
        if product is None:
            return [Finding("UNKNOWN_SKU", "error", f"код 1С {sku.group(1)}")]
        prices = [_amount(value) for value in _PRICE.findall(text)]
        if product.price is not None and prices and product.price not in prices:
            found.append(
                Finding("PRICE_MISMATCH", "error", f"«{product.name}»: в ответе {prices[0]} ₽, в каталоге {product.price} ₽")
            )
        return found
    for name, rest in _LIST_LINE.findall(text):
        product = facts.by_name.get(_norm(name))
        prices = [_amount(value) for value in _PRICE.findall(rest)]
        if product is not None and product.price is not None and prices and product.price not in prices:
            found.append(
                Finding("PRICE_MISMATCH", "error", f"«{product.name}»: в списке {prices[0]} ₽, в каталоге {product.price} ₽")
            )
    return found


def _amount(value: str) -> int:
    return int(re.sub(r"\D", "", value))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def _around(text: str, index: int) -> str:
    return "…" + text[max(0, index - 40) : index + 60].replace("\n", " ") + "…"


def _unique(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    result = []
    for finding in findings:
        if (finding.code, finding.text) not in seen:
            seen.add((finding.code, finding.text))
            result.append(finding)
    return result
