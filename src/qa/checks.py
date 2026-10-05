"""Проверки ответа бота по фактам — без модели.

Цена и код 1С сверяются с каталогом той версии, что лежит у бота; пункт перечня — со справочником
приказов. Карточка несёт «Код 1С: …», список — строки «N. Название — цена — наличие».
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from agent.verify import foreign_script
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
    # Ночь 14.09: судья засчитывал «файл получен», хотя файл был не тот, — эти проверки ловят такое кодом.
    "WRONG_FILE": "файл не по разделу из ответа",
    "AGE_MISMATCH": "раздел для другого возраста",
    "FOREIGN_SCRIPT": "слова не на русском",
    "CARD_TEXT": "карточка не из списка ответа",
    "DEAD_BUTTON": "кнопка менеджера не ведёт к менеджеру",
    "ZERO_PREORDER": "предзаказ на 0 ₽",
    "FALSE_HANDOFF": "«передал» без заявки",
    "PROMISE": "скидка или бесплатная доставка без источника",
    # Прогон 04.10: ходы без модели считаются отдельно — так видно работу запасного пути (К1/К8).
    "DEGRADED": "ответ-заглушка «консультант недоступен»",
    "FALLBACK_OFFER": "подбор без модели (запасной путь)",
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
_FILE_CODE = re.compile(r"Комплектация[ _](\d{1,2}(?:[._]\d{1,3}){0,5})")
_SECTION_WORD = re.compile(r"раздел\w*\s*№?\s*(\d{1,2}(?:\.\d{1,3}){1,5})(?!\d|\.\d)", re.IGNORECASE)
_AGE_SAID = re.compile(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*(?:лет|года)|(\d{1,2})\s*(?:лет|года)\b", re.IGNORECASE)
_AGE_SECTION = re.compile(r"для\s+детей\s+(?:от\s+)?(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*(?:лет|года)", re.IGNORECASE)
_CONTACT = re.compile(r"\+7|\b8\s*\(?\d{3}|@\w")
_ZERO_PREORDER = re.compile(r"Предварительный заказ PO-\S+: позиций \d+ на 0 ₽")
_HANDOFF = re.compile(r"\bпередал[аи]?\b", re.IGNORECASE)
# Эталон сценариев 15.09 обещает «скидку 5–10 %» и «бесплатную доставку от 100 000» — у магазина таких правил нет.
_DISCOUNT = re.compile(r"скидк\w*[^.\n]{0,40}?\d{1,2}\s*(?:[-–—]\s*\d{1,2}\s*)?%|\d{1,2}\s*%[^.\n]{0,20}скидк", re.IGNORECASE)
_FREE_DELIVERY = re.compile(r"бесплатн\w*\s+доставк|доставк\w*[^.\n]{0,40}бесплатн", re.IGNORECASE)
# Деградация и запасной подбор без модели (прогон 04.10: 23 деградации, 74 хода без вызова модели).
_DEGRADED = re.compile(r"проще\s+обычного", re.IGNORECASE)
_FALLBACK_OFFER = re.compile(r"могу\s+предложить\s+товары\s+из\s+каталога", re.IGNORECASE)
# Служебные сообщения и строки («Что дальше?», «Подбор из каталога», меню документов) — не основной
# текст ответа: в сравнении повторов REPEAT они шумели (41 % «повторов» прогона 04.10 — это меню).
_SERVICE_MESSAGE = re.compile(
    r"^\s*(?:что\s+дальше\s*\??|выберите\s+раздел\s+каталога\s*:?|по\s+какому\s+документу\s+подбираем\s*\??|"
    r"подбор\s+из\s+каталога\s*:?|меню|главное\s+меню|начать\s+заново\s*\??)[\s:!.]*$",
    re.IGNORECASE,
)
_SERVICE_LINE = re.compile(
    r"^\s*(?:что\s+дальше\s*\??|подбор\s+из\s+каталога\s*:?|могу\s+предложить\s+товары\s+из\s+каталога\s*[:—-]?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Названия товаров и цитаты: код внутри («7.71.2», артикул «1.13.3.2.2и») — не пункт приказа (BUG-13).
_QUOTED = re.compile(r"[«»„“\"`][^«»„“\"`]{1,120}[«»„“\"`]")


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


def check_turn(turn: Turn, facts: CatalogFacts, history: list[str], said: list[str] | None = None) -> list[Finding]:
    """Замечания к ходу. `history` — прежние ответы бота, `said` — реплики тестировщика, включая эту."""
    findings: list[Finding] = []
    if not turn.messages:
        findings.append(Finding("NO_REPLY", "error", f"бот не ответил за {turn.seconds:.0f} с"))
    elif turn.seconds > SLOW_ERROR:
        findings.append(Finding("SLOW", "error", f"первый ответ через {turn.seconds:.0f} с"))
    elif turn.seconds > SLOW_WARNING:
        findings.append(Finding("SLOW", "warning", f"первый ответ через {turn.seconds:.0f} с"))

    joined = "\n".join(message.text for message in turn.messages if message.text)
    if _DEGRADED.search(joined):
        findings.append(Finding("DEGRADED", "warning", "бот ответил заглушкой «проще обычного»"))
    if turn.seconds < 2.0 and _FALLBACK_OFFER.search(joined):
        findings.append(Finding("FALLBACK_OFFER", "warning", f"подбор без модели за {turn.seconds:.1f} с"))

    for message in turn.messages:
        text = message.text
        if error := _ERROR.search(text):
            findings.append(Finding("ERROR_TEXT", "error", _around(text, error.start())))
        if _MARKDOWN.search(text):
            findings.append(Finding("MARKDOWN", "warning", "в тексте **, # или таблица с «|»"))
        findings += _prices(text, facts)
        if foreign := foreign_script(text):
            findings.append(Finding("FOREIGN_SCRIPT", "error", ", ".join(sorted(foreign))))
        if _ZERO_PREORDER.search(text):
            findings.append(Finding("ZERO_PREORDER", "error", text.splitlines()[0][:120]))
        if _HANDOFF.search(text) and "PO-" not in text:
            findings.append(Finding("FALSE_HANDOFF", "warning", _around(text, _HANDOFF.search(text).start())))
        if promise := _DISCOUNT.search(text):
            findings.append(Finding("PROMISE", "error", _around(text, promise.start())))
        elif promise := _FREE_DELIVERY.search(text):
            findings.append(Finding("PROMISE", "warning", _around(text, promise.start())))
        if facts.points:
            # Код внутри названия товара или цитаты — не пункт приказа (артикул «7.71.2» в названии).
            scan = text
            if _SKU.search(text):  # карточка: первая строка — название товара
                scan = "\n".join(scan.splitlines()[1:])
            for code in _POINT.findall(_QUOTED.sub(" ", _LIST_LINE.sub(" ", scan))):
                if code not in facts.points:
                    findings.append(Finding("UNKNOWN_POINT", "warning", f"пункт {code}"))

    if turn.kind == "button" and turn.text.lower().startswith("скачать") and not any(m.file for m in turn.messages):
        findings.append(Finding("FILE_MISSING", "error", f"после «{turn.text}» файла нет"))
    if turn.kind == "button" and "менеджер" in turn.text.lower():
        texts = [message.text for message in turn.messages if message.text]
        if texts and not any("менеджер" in text.lower() or _CONTACT.search(text) for text in texts):
            findings.append(Finding("DEAD_BUTTON", "error", f"«{turn.text}» → «{texts[0][:60]}»"))
    findings += _wrong_files(turn, history)
    findings += _other_age(turn, said or [])
    findings += _cards_off_list(turn)

    seen = [_norm(message.text)[:300] for message in turn.messages if message.text.strip()]
    if len(set(seen)) < len(seen):
        findings.append(Finding("DUPLICATE", "warning", "бот прислал одно и то же несколько раз"))
    # REPEAT — только по основному тексту ответа, без служебных сообщений и строк-обёрток:
    # иначе меню и «Что дальше?» считают повтором (К8, прогон 04.10).
    main = _substantive(message.text for message in turn.messages if message.text.strip())
    if main and any(text in _substantive(history) for text in main):
        findings.append(Finding("REPEAT", "warning", "ответ слово в слово повторяет прошлый"))
    return _unique(findings)


def _substantive(texts: Iterable[str]) -> list[str]:
    """Основной текст ответов: без чисто служебных сообщений и строк-обёрток выдачи."""
    result = []
    for text in texts:
        if _SERVICE_MESSAGE.match(text):
            continue
        cleaned = _norm(_SERVICE_LINE.sub(" ", text))
        if len(cleaned) >= 30:
            result.append(cleaned[:300])
    return result


def _prices(text: str, facts: CatalogFacts) -> list[Finding]:
    found: list[Finding] = []
    sku = _SKU.search(text)
    if sku:
        # Код в обратных кавычках или цитате — не повод для «не из каталога» (0Э-00006646 ушёл за кавычки).
        code = sku.group(1).strip("`«»„“\"'(),.;:")
        product = facts.by_sku.get(code)
        if product is None:
            return [Finding("UNKNOWN_SKU", "error", f"код 1С {code}")]
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


def _wrong_files(turn: Turn, history: list[str]) -> list[Finding]:
    """Файл комплектации не по тому разделу, о котором последний ответ (сц. 24: технопарк → стулья)."""
    texts = [m.text for m in turn.messages if m.text and not m.file] + list(reversed(history))
    named = next((codes for text in texts if not text.startswith("Комплектация ") and (codes := _codes_in(text))), set())
    found = []
    for message in turn.messages:
        match = _FILE_CODE.search(message.text or "") or _FILE_CODE.search(message.file or "") if message.file else None
        if match is None or not named:
            continue
        code = match.group(1).replace("_", ".")
        if not any(other == code or other.startswith(f"{code}.") or code.startswith(f"{other}.") for other in named):
            found.append(Finding("WRONG_FILE", "error", f"файл по разделу {code}, а в ответе — {', '.join(sorted(named)[:3])}"))
    return found


def _codes_in(text: str) -> set[str]:
    return set(_POINT.findall(text)) | set(_SECTION_WORD.findall(text))


def _other_age(turn: Turn, said: list[str]) -> list[Finding]:
    """Раздел «для детей A–B лет», не пересекающийся с возрастом, который назвал клиент (сц. 2, 5)."""
    numbers: list[int] = []
    for low, high, single in _AGE_SAID.findall(" ".join(said)):
        numbers += [int(low), int(high)] if low else [int(single)]
    if not numbers:
        return []
    client = (min(numbers), max(numbers))
    found = []
    for message in turn.messages:
        groups = [(int(low), int(high)) for low, high in _AGE_SECTION.findall(message.text or "")]
        if groups and not any(low <= client[1] and client[0] <= high for low, high in groups):
            found.append(
                Finding(
                    "AGE_MISMATCH",
                    "error",
                    f"раздел для детей {groups[0][0]}–{groups[0][1]} лет, а клиенту нужно {client[0]}–{client[1]}",
                )
            )
    return found


def _cards_off_list(turn: Turn) -> list[Finding]:
    """Карточка товара, которого нет в списке того же хода (15.09: «Д-214» в списке, карточка «Д-222»)."""
    listed = [
        _plain(name)
        for message in turn.messages
        if message.text and not _SKU.search(message.text)
        for name, _ in _LIST_LINE.findall(message.text)
    ]
    if not listed:
        return []
    found = []
    for message in turn.messages:
        if not _SKU.search(message.text or ""):
            continue
        title = message.text.splitlines()[0]
        if not any(_plain(title) in name or name in _plain(title) for name in listed):
            found.append(Finding("CARD_TEXT", "warning", f"«{title[:80]}»"))
    return found


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[«»\"„“*]", "", _norm(text))).strip()


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
