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
    # Вызов инструмента, написанный текстом, — то же ложное обещание: подбора не было.
    return looks_like_tool_call(text) or (listing and len(_BULLET.findall(text)) >= 2)


def without_promises(answer: str) -> str:
    """Ответ без предложений, в которых обещан подбор. Строки и списки сохраняются."""
    lines: list[str] = []
    for line in without_tool_calls(answer).splitlines():
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
    r"\bпока\s+(?:я\s+)?(?:ищу|подбираю|проверяю|смотрю)\b|\bвыполн(?:яю|ю)\s+(?:поиск|подбор)|"
    # Пересказ вызова инструмента вместо вызова (прогоны 14.09 и 16.09: «(Вызову инструмент
    # для подбора товаров.)» — и ни одного вызова за четыре хода подряд).
    r"\bвыз\w+\s+инструмент|\b(?:ожидаю|жду)\s+результат\w*|"
    r"\bсейчас\s+(?:я\s+)?выполн\w*\s+(?:подбор|поиск)|\bодин\s+момент\b|\bминуточку\b|\bподождите\b|"
    # «Подбираю оборудование для групповых комнат по приказу № 1057» — отчёт о работе,
    # которой не было. В вопросе («что вам подбираю — мебель или игры?») это не обещание.
    r"\b(?:подбираю|ищу|проверяю|смотрю)\b(?![^?]*\?)|"
    # Ход у модели один: «после этого я смогу предоставить размеры» — следующего не будет.
    r"\bпосле\s+(?:этого|подбора|поиска)\s+(?:я\s+)?(?:смогу|покажу|предоставл|расскажу)\w*|"
    r"\bодну\s+(?:секунду|минуту)|\bсекунд(?:у|очку)\b|\bминутк\w+|"
    r"уточню\s+и\s+вернусь|пришлю\s+позже|вернусь\s+с\s+(?:вариантами|подборкой|позициями)",
    re.IGNORECASE,
)
# «Подберу…», если дальше в предложении нет вопросительного знака.
_PROMISE_LATER = re.compile(
    r"\b(?:подберу|поищу|найду|покажу|выполню|вызову|запущу)\b(?![^?]*\?)", re.IGNORECASE
)
_CONDITION = re.compile(r"\b(?:если|когда|как\s+только|после\s+того|чтобы)\b", re.IGNORECASE)


# Вежливость в ответ на нашу же просьбу переписать: «Спасибо, что поправили», «Понял, спасибо за
# замечание», «Вы правы. Переписываю строго по данным из инструментов». Цифр в такой фразе нет —
# по ним отличается настоящее «Спасибо за уточнение: для 5–6 лет подойдёт раздел 1.14.5».
_META_MARK = re.compile(
    r"спасибо|поправил\w*|замечани\w*|перепис\w*|перепиш\w*|исправ\w*|переформулир\w*|"
    r"вы\s+прав\w*|прошу\s+прощени\w*|извин\w*|уч(?:ё|е)л|прин(?:ял|ято)|"
    r"по\s+данным\s+из\s+инструмент\w*|тольк\w*\s+по\s+.{0,30}инструмент\w*",
    re.IGNORECASE,
)
_FIRST_SENTENCE = re.compile(r"[^.!?\n]{1,160}[.!?…]+[ \t]*")
META_SENTENCE_CHARS = 160


def _meta_sentence(sentence: str) -> bool:
    return bool(_META_MARK.search(sentence)) and not re.search(r"\d", sentence)


def without_meta(answer: str) -> str:
    """Ответ без служебного вступления «Спасибо, что поправили. Переписываю…».

    Просьбу переписать ответ модель принимает за реплику человека и отвечает на неё
    извинением. Ночью 15.09 такие извинения ушли клиенту 26 раз в 17 диалогах из 25,
    а в двух ходах кроме них не было ничего: человек ничего не поправлял — жалоба наша.
    """
    text = (answer or "").lstrip()
    while text:
        match = _FIRST_SENTENCE.match(text)
        if match is None or not _meta_sentence(match.group(0)):
            break
        text = text[match.end() :].lstrip()
    if "\n" not in text and len(text) <= META_SENTENCE_CHARS and _meta_sentence(text):
        # Извинение без точки в конце — весь ответ и есть вежливость.
        return ""
    return text.strip()


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
    # «Пункт перечня: 1.1.1.1» (ночь 16.09, сц. 3) проверку обходил: между словом и
    # номером стояло «перечня:», и выдуманные пункты уходили человеку как настоящие.
    r"(?:пункт\w*|позици\w+|п\.)\s*(?:перечн\w+|приказ\w*|списка)?[\s:№]*"
    r"(\d{1,2}(?:\.\d{1,3}){1,5})"
    # Номер приказа берётся из хвоста той же ссылки, но не через следующую: «1.1.1.1,
    # пункт 2.1.4 приказа 838» — это два пункта, а не один пункт приказа 838.
    r"(?:(?:(?!пункт|позици|п\.)[^\n]){0,60}?(838|1057))?",
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
    lines = (answer or "").splitlines()
    keep = [
        not invented_prices(line, prices)
        and not invented_norm_refs(line, refs)
        and not foreign_script(line)
        and not any(code in bad_codes for code, _ in listed_codes(line))
        for line in lines
    ]
    _drop_empty_headings(lines, keep)
    kept = [line for index, line in enumerate(lines) if keep[index]]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _drop_empty_headings(lines: list[str], keep: list[bool]) -> None:
    """Убрать названия зон, под которыми не осталось ни одного пункта.

    Ночью 16.09 (сц. 2) человек получил «Групповое помещение 5–6 лет (раздел 1.14.6)»,
    «Общее для обеих групп» — и пустоту: строки списка проверка вырезала, а заголовки
    над ними остались. Заголовком считается только строка, под которой список и был.
    """
    for index, line in enumerate(lines):
        if not keep[index] or not line.strip() or _BULLET.match(line):
            continue
        items = _items_under(lines, index)
        if items and not any(keep[item] for item in items):
            keep[index] = False


def _items_under(lines: list[str], index: int) -> list[int]:
    """Номера строк списка сразу под строкой `index`: пустая строка разрывом не считается."""
    items: list[int] = []
    for number in range(index + 1, len(lines)):
        if not lines[number].strip():
            continue
        if _BULLET.match(lines[number]):
            items.append(number)
            continue
        break
    return items


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


# Строка из одной пунктуации: «;» на месте вызова инструмента, который DeepSeek
# пересказал словами (прогон 16.09, сц. 3, четыре хода подряд).
_SERVICE_LINE = re.compile(r"^[\s;:.,…—–*`~]+$")


def without_service_marks(answer: str) -> str:
    """Ответ без строк, в которых нет ничего, кроме знаков препинания."""
    kept = [line for line in (answer or "").splitlines() if not _SERVICE_LINE.match(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# Вызов инструмента, который модель написала текстом вместо того, чтобы его сделать.
# Ночь 16.09: клиенту ушёл голый JSON «{"name":"handoff_to_manager","parameters":{…}}»
# (сц. 50 — тот самый снимок экрана), «Функция вызывается:» с тремя такими блоками
# (сц. 47) и «*Вызываю инструменты для проверки.* Добавляю в корзину…» без единого
# вызова (сц. 48). Ни цены, ни пункты в таком ответе не подтвердить — он не показывается.
_TOOL_CALL_JSON = re.compile(
    r"```[a-z]*\s*\{.*?\}\s*```|"
    r"\{[^{}]*\"(?:name|arguments|parameters|code|document|query)\"\s*:[^{}]*\}",
    re.DOTALL,
)
_TOOL_CALL_WORDS = re.compile(
    r"функци\w+\s+вызыва\w+|вызыва\w+\s+(?:инструмент|функци|поиск)\w*|"
    r"уточн\w+[^.\n]{0,30}через\s+инструмент\w*|"
    r"\b(?:проверяю|запрашиваю|добавляю|уточняю|показываю)\b[^.\n]{0,60}\.{3}",
    re.IGNORECASE,
)
# Имена инструментов человеку не нужны ни в каком виде.
_TOOL_NAMES = re.compile(
    r"\b(?:search_products|find_by_norm_code|find_norm_item|explain_norm|get_product|"
    r"add_to_cart|get_cart|handoff_to_manager)\b"
)


def looks_like_tool_call(answer: str) -> bool:
    """Есть ли в ответе вызов инструмента, написанный текстом."""
    text = answer or ""
    return bool(
        _TOOL_CALL_JSON.search(text) or _TOOL_NAMES.search(text) or _TOOL_CALL_WORDS.search(text)
    )


# Остатки незакрытого блока: «```json» отдельной строкой и строки тела запроса. Ночью 16.09
# (сц. 47) таких блоков было три подряд, и последний модель не закрыла.
_FENCE_LINE = re.compile(r"^\s*(?:`{2,}\s*[a-z]*|json)\s*$", re.IGNORECASE)
_JSON_LINE = re.compile(r'^\s*[\[\]{},]*\s*(?:"[^"\n]*"\s*:.*|[\[\]{},]+)\s*$')


def without_tool_calls(answer: str) -> str:
    """Ответ без блоков и фраз, которыми модель пересказывает вызов инструмента."""
    text = _TOOL_CALL_JSON.sub("", answer or "")
    kept: list[str] = []
    for line in text.splitlines():
        if _TOOL_NAMES.search(line) or _FENCE_LINE.match(line) or _JSON_LINE.match(line):
            continue
        parts = [part.strip() for part in re.split(r"(?<=[.!?)])\s+", line) if part.strip()]
        stayed = [part for part in parts if not _TOOL_CALL_WORDS.search(part)]
        if stayed or not parts:
            kept.append(" ".join(stayed))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# «Я передал ваш запрос менеджеру» — бот не передаёт ничего сам: заявка уходит только
# с контактом человека. Ночью 16.09 так сказано в шести диалогах из пятидесяти, и ни
# одной заявки за этими словами не стояло.
_HANDOFF_CLAIM = re.compile(
    r"\b(?:передал|передала|передали|отправил|отправила|направил|направила)\w*\b"
    r"[^.!?\n]{0,60}?\bменеджер",
    re.IGNORECASE,
)
# «Хотите, чтобы я передал вопрос менеджеру?» и «могу передать» — предложение, не отчёт.
_HANDOFF_OFFER = re.compile(r"\b(?:хотите|могу|если|нужно\s+ли|давайте|готов)\b", re.IGNORECASE)


def claims_handoff(answer: str) -> bool:
    """Сказано ли в ответе, что заявка менеджеру уже передана."""
    for sentence in _SENTENCE.split(answer or ""):
        if "?" in sentence or _HANDOFF_OFFER.search(sentence):
            continue
        if _HANDOFF_CLAIM.search(sentence):
            return True
    return False


# «Список вы уже скачали файлом» — бот не знает, скачивал ли человек файл, и такого
# состояния у него нет вовсе. 23.09 (сц. 29) так отвечали дважды подряд.
_DOWNLOAD_CLAIM = re.compile(
    r"вы\s+уже\s+скачал\w*|уже\s+скачивал\w*|вы\s+файл\s+(?:уже\s+)?(?:скачал\w*|получил\w*|открыл\w*)|"
    r"файл\s+у\s+вас",
    re.IGNORECASE,
)


def claims_download(answer: str) -> bool:
    """Сказано ли в ответе, что человек уже скачал файл."""
    return bool(_DOWNLOAD_CLAIM.search(answer or ""))


# Дата приказа — часть основания: 23.09 (сц. 11) «приказ № 838 от 06.09.2022» выдал
# несуществующую дату, хотя правильная была в данных инструментов.
_DOC_DATE = re.compile(
    r"(?:приказ\w*|перечен\w*|№\s?\d{3,4})[^.\n]{0,60}?от\s+(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE
)


def invented_doc_dates(answer: str) -> list[str]:
    """Даты приказов, которых нет в справочнике документов."""
    from norms.documents import DOCUMENTS

    known = {
        date
        for doc in DOCUMENTS.values()
        for date in re.findall(r"\d{2}\.\d{2}\.\d{4}", getattr(doc, "citation", "") or "")
    }
    found = _DOC_DATE.findall(answer or "")
    return sorted({date for date in found if date not in known})
