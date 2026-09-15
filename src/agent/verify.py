"""Проверка ответа модели на выдуманные цены и нормативные основания.

Промпт запрещает называть цены не из инструментов, но запрет — не гарантия.
В журнале 28.08 есть ответ, где модель на пустом месте сочинила пять позиций
со стоимостью: «Мяч резиновый для игр, 20 см — 190 ₽, позиция 2.1.14». Ни одной
такой позиции инструменты в тот ход не возвращали.

Цена — самое опасное, что бот может выдумать: по ней принимают решение о закупке,
её вставляют в спецификацию, на неё ссылаются при разговоре с менеджером. Поэтому
она проверяется механически: каждое число с рублями в ответе должно встречаться
среди того, что вернули инструменты. Не совпало — ответ переписывается.

Тем же способом проверяются ссылки на пункты приказов: они попадают в
спецификацию наравне с ценой, и «соответствует пункту 2.20.63 приказа 1057»
(на деле — фрезерный станок из приказа 838) стоит закупщику дороже, чем
ошибка в рублях.

Названия товаров так не проверить (модель законно склоняет и сокращает их), а вот
цифры совпадают дословно или не совпадают вовсе.
"""

from __future__ import annotations

import re

# «27 635 ₽», «1 990 руб.», «190 рублей», «253000 р.»
_PRICE = re.compile(r"(\d[\d\s ]{2,})\s*(?:₽|руб\w*|р\.)", re.IGNORECASE)
# Числа меньше сотни рублей в каталоге не встречаются, а вот «2 шт.» и «5 лет»
# ловятся легко — такие совпадения не считаем ценой вовсе.
MIN_PRICE = 100


def prices_in(text: str) -> set[int]:
    """Все суммы, названные в тексте."""
    found = set()
    for raw in _PRICE.findall(text or ""):
        digits = re.sub(r"\D", "", raw)
        if digits and int(digits) >= MIN_PRICE:
            found.add(int(digits))
    return found


def promises_goods(answer: str, listing: bool = False) -> bool:
    """Обещан ли подбор, которого в этом ходе не было, — по форме ответа.

    «Сейчас подберу…», «секунду, поищу» без вызова инструмента — ложное обещание:
    следующего хода у модели нет, и человек остаётся без товара. 13.09 на OpenRouter
    так ответили три хода подряд, а страховка дописывала к обещанию выдачу по
    помещению из профиля: на «массажные мячи» пришли тележка и ребристая доска.

    `listing` — показывать позиции уже можно, а модель перечислила оборудование
    списком, не вызвав подбор: «мячи, обручи, скакалки» вместо позиций каталога.

    Предложение подобрать — «хотите, подберу?» — обещанием не считается: это вопрос.
    Условие — «когда уточните возраст, подберу точнее» — тоже: подбор обещан потом и
    не без причины.
    """
    text = answer or ""
    for sentence in _SENTENCE.split(text):
        if _PROMISE_NOW.search(sentence):
            return True
        if _PROMISE_LATER.search(sentence) and not _CONDITION.search(sentence):
            return True
    return listing and len(_BULLET.findall(text)) >= 2


def without_promises(answer: str) -> str:
    """Ответ без предложений, в которых обещан подбор. Строки и списки сохраняются."""
    lines: list[str] = []
    for line in (answer or "").splitlines():
        parts = [part.strip() for part in re.split(r"(?<=[.!?)])\s+", line) if part.strip()]
        # Реплика целиком в скобках — «(Ожидаю результатов поиска.)» — служебная речь модели.
        kept = [
            part
            for part in parts
            if not promises_goods(part) and not (part.startswith("(") and part.endswith(")"))
        ]
        if kept or not parts:
            lines.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# Строка списка: «- Мячи», «• Обручи», «* Скакалки», «1. Маты».
_BULLET = re.compile(r"^\s*(?:[-*•—]|\d+[.)])\s+\S", re.MULTILINE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
# Действие, объявленное как идущее прямо сейчас, вместо выполненного.
_PROMISE_NOW = re.compile(
    r"\bсейчас\s+(?:я\s+)?(?:подбер|поищ|найд|посмотр|покаж|провер|собер|пришл)\w*|"
    r"\bсейчас\s+(?:я\s+)?уточню,?\s+(?:какие|что\s+есть|в\s+каталоге|наличие|цены)|"
    r"\bпока\s+(?:я\s+)?(?:ищу|подбираю|проверяю|смотрю)\b|\bвыполняю\s+поиск|"
    # Пересказ вызова инструмента вместо вызова (прогон 14.09, возражение «дорого»).
    r"\bвызываю\s+инструмент|\b(?:ожидаю|жду)\s+результат\w*|"
    r"\bсейчас\s+(?:я\s+)?выполн\w*\s+(?:подбор|поиск)|\bодин\s+момент\b|\bминуточку\b|\bподождите\b|"
    r"\b(?:подбираю|ищу|проверяю|смотрю)\s+(?:для\s+вас|варианты|позиции|товары|в\s+каталоге)|"
    r"\bодну\s+(?:секунду|минуту)|\bсекунд(?:у|очку)\b|\bминутк\w+|"
    r"уточню\s+и\s+вернусь|пришлю\s+позже|вернусь\s+с\s+(?:вариантами|подборкой|позициями)",
    re.IGNORECASE,
)
# «Подберу…», если дальше в предложении нет вопросительного знака.
_PROMISE_LATER = re.compile(r"\b(?:подберу|поищу|найду|покажу)\b(?![^?]*\?)", re.IGNORECASE)
_CONDITION = re.compile(r"\b(?:если|когда|как\s+только|после\s+того|чтобы)\b", re.IGNORECASE)


def invented_prices(answer: str, allowed: set[int]) -> set[int]:
    """Суммы из ответа, которых не было в результатах инструментов.

    Итоговые суммы корзины и умножения на количество сюда попадут тоже, поэтому
    вызывающая сторона добавляет их в `allowed` заранее — проще, чем угадывать
    арифметику модели постфактум.
    """
    return prices_in(answer) - allowed


# «пункт 2.1.14», «позиция 1.5.1.41», «п. 2.4», «соответствует пункту 2.20.63».
# Номер приказа берётся из хвоста той же фразы, если он там есть.
_NORM_MENTION = re.compile(
    r"(?:пункт\w*|позици\w+|п\.)\s*№?\s*(\d{1,2}(?:\.\d{1,3}){1,5})"
    r"(?:[^\n]{0,60}?(838|1057))?",
    re.IGNORECASE,
)
_DOC_BY_NUMBER = {"838": "order_838", "1057": "order_1057"}


def norm_refs_in(text: str) -> set[tuple[str | None, str]]:
    """Нормативные ссылки, названные в тексте: пары «документ, пункт».

    Документ бывает не назван — тогда в паре стоит `None`, и проверка
    ограничивается номером пункта: придираться к тому, чего модель не сказала,
    значило бы отвергать нормальные ответы.
    """
    found: set[tuple[str | None, str]] = set()
    for code, number in _NORM_MENTION.findall(text or ""):
        found.add((_DOC_BY_NUMBER.get(number), code))
    return found


def invented_norm_refs(
    answer: str, allowed: set[tuple[str, str]]
) -> set[tuple[str | None, str]]:
    """Ссылки на пункты приказов, которых не было в результатах инструментов.

    Цена — не единственное, что опасно выдумать. «Спортивный комплекс малый
    соответствует пункту 2.20.63 приказа 1057» выглядит так же убедительно, как
    цена, попадает в спецификацию так же охотно, — а 2.20.63 это фрезерный
    станок из приказа 838. Проверка та же, что для сумм: названное в ответе
    должно встречаться среди того, что вернули инструменты.
    """
    codes = {code for _, code in allowed}
    invented: set[tuple[str | None, str]] = set()
    for doc_id, code in norm_refs_in(answer):
        if doc_id is None:
            # Приказ не назван — довольствуемся тем, что пункт вообще звучал.
            if code not in codes:
                invented.add((doc_id, code))
        elif (doc_id, code) not in allowed:
            invented.add((doc_id, code))
    return invented


def without_unverified(
    answer: str, prices: set[int], refs: set[tuple[str, str]], bad_codes: set[str] | frozenset[str] = frozenset()
) -> str:
    """Ответ без строк, где есть неподтверждённая сумма, пункт приказа, чужой код или слова не по-русски.

    Консультанту выдача каталога вместо ответа не годится. 14.09 на «дай консультацию… кабинет
    логопеда» дважды не подтвердился один пункт, и человек получил фитбол и тактильные мячики,
    хотя остальная комплектация была подтверждена инструментом. Теряла её одна строка.
    """
    kept = [
        line
        for line in (answer or "").splitlines()
        if not invented_prices(line, prices)
        and not invented_norm_refs(line, refs)
        and not foreign_script(line)
        and not any(code in bad_codes for code, _ in listed_codes(line))
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# Иероглифы, кана, хангыль, арабица. Ночью 14.09 DeepSeek-V4-Flash вставил в русские ответы «不大», «走廊»,
# «换个». Латиница — названия брендов и моделей — разрешена.
_FOREIGN = re.compile(r"[؀-ۿ぀-ヿ㐀-䶿一-鿿가-힯]+")


def foreign_script(text: str) -> set[str]:
    """Слова не кириллицей и не латиницей."""
    return set(_FOREIGN.findall(text or ""))


# Код в начале строки списка — «- 1.14.5.7.1.39 Кубики», «2.12.2 — Мольберт» — и «раздел 2.12 «…»». Ночью 14.09
# такие строки проходили мимо проверки оснований: пунктов 1.14.5.7.1.39–48 в приказе нет, а раздел 2.12
# приказа 838 («Словари») модель расписала как кабинет ИЗО. Дата «25.12.2024» кодом не считается.
_LISTED_CODE = re.compile(
    r"^[ \t]*(?:[-•*–—]\s*|\d{1,2}[.)]\s+)?(\d{1,2}(?:\.\d{1,3}){2,5})(?!\d|\.\d)(.*)$", re.MULTILINE
)
_SECTION_CODE = re.compile(
    r"раздел\w*\s*№?\s*(\d{1,2}(?:\.\d{1,3}){1,5})(?!\d|\.\d)(?:\s*[«\"„]([^»\"“\n]{3,120})[»\"“])?",
    re.IGNORECASE,
)


def listed_codes(text: str) -> list[tuple[str, str]]:
    """Коды из строк списка и из «раздел X «название»» — с названием, как его написала модель."""
    found: list[tuple[int, str, str]] = []
    for match in _LISTED_CODE.finditer(text or ""):
        title = re.split(r"\s+[—–-]\s+", match.group(2).lstrip(" \t—–-:."), maxsplit=1)[0]
        found.append((match.start(), match.group(1), title.strip(" «»\"„“.,;:")))
    for match in _SECTION_CODE.finditer(text or ""):
        found.append((match.start(), match.group(1), (match.group(2) or "").strip()))
    return [(code, title) for _, code, title in sorted(found)]


def _title_stems(text: str) -> set[str]:
    return {word[:5] for word in re.findall(r"[а-яa-z]{4,}", (text or "").lower().replace("ё", "е"))}


def title_matches(claimed: str, titles: list[str]) -> bool:
    """Похоже ли название из ответа на формулировку приказа: сокращать модели можно, подменять — нет."""
    wanted = _title_stems(claimed)
    if not wanted:
        return True
    known = set().union(*(_title_stems(title) for title in titles))
    return len(wanted & known) * 2 >= len(wanted)


_SECTION_AGE = re.compile(r"для\s+детей\s+(?:от\s+)?(\d{1,2})\s*[-–—]\s*(\d{1,2})", re.IGNORECASE)
_UNDER_YEAR = re.compile(r"для\s+детей\s+до\s+(?:1|одного)\s+года", re.IGNORECASE)


def section_ages(title: str) -> tuple[int, int] | None:
    """Возраст группы из названия раздела: «Групповые помещения для детей 1 - 2 лет» → (1, 2)."""
    if _UNDER_YEAR.search(title or ""):
        return (0, 1)
    match = _SECTION_AGE.search(title or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def client_ages(age: str | None) -> tuple[int, int] | None:
    """Возраст из профиля: «5–6 лет» → (5, 6). Без чисел — `None`, сверять не с чем."""
    numbers = [int(number) for number in re.findall(r"\d{1,2}", age or "")]
    return (min(numbers), max(numbers)) if numbers else None


def describe_refs(refs: set[tuple[str | None, str]]) -> str:
    """Человеческий список ссылок для просьбы переписать ответ."""
    from norms import documents as docs

    parts = []
    for doc_id, code in sorted(refs, key=lambda ref: ref[1]):
        if doc_id:
            parts.append(f"пункт {code} приказа {docs.get(doc_id).short_name}")
        else:
            parts.append(f"пункт {code}")
    return ", ".join(parts)
