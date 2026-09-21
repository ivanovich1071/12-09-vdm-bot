"""Понимание задачи из свободной речи: что уже известно о закупке.

Разбор детерминированный и опирается на те же правила, что профиль разговора
(`core/profile.py`) и разделы каталога (`catalog/placement.py`): бот и ядро понимают
одну фразу одинаково. Добавлено то, что нужно закупке: класс, количество, число
детей, пункт перечня, «без норматива».

Уже известное не стирается: человек уточняет задачу, а не начинает её заново.
"""

from __future__ import annotations

import re

from catalog import placement
from core import intent
from core import profile as dialog_profile
from core.profile import DialogProfile
from procurement.models import ProcurementTask

_GRADE = re.compile(
    r"\b(\d{1,2})\s*(?:[-–—]\s*(\d{1,2}))?\s*(?:-?[йхе]\s+)?класс\w*", re.IGNORECASE
)
_AGE_SPAN = re.compile(r"\b(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*(?:год\w*|лет)\b", re.IGNORECASE)
_AGE_FROM_TO = re.compile(r"\bот\s+(\d{1,2})\s+до\s+(\d{1,2})\s*(?:год\w*|лет)\b", re.IGNORECASE)
_QUANTITY = re.compile(
    r"\b(\d{1,4})\s*(?:шт\b\.?|штук\w*|комплект\w*|единиц\w*|экземпляр\w*)", re.IGNORECASE
)
_PARTICIPANTS = re.compile(
    r"\bна\s+(\d{1,3})\s+(?:учени\w*|школьник\w*|ребен\w*|ребён\w*|дет\w*|человек\w*|"
    r"воспитанник\w*|мест\w*)",
    re.IGNORECASE,
)
_GROUPS = re.compile(r"\b(\d{1,2})\s+групп\w*", re.IGNORECASE)
# Номер пункта: с «п.», «пункт», «позиция» — любой глубины, без них — от трёх
# уровней. Иначе «1.5 млн» и «2.5 года» становятся пунктами перечня.
_POINT = re.compile(
    r"(?:\bп\.?|\bпункт\w*|\bпозици\w*)\s*(\d+(?:\.\d+){1,5})\b|\b(\d+(?:\.\d+){2,5})\b",
    re.IGNORECASE,
)
_NO_NORM = re.compile(
    r"без\s+(?:норматив\w*|приказ\w*|перечн\w*)|не\s+по\s+(?:норматив\w*|приказ\w*|перечн\w*)"
    r"|норматив\w*\s+не\s+(?:нуж|важ)\w*",
    re.IGNORECASE,
)
_WITH_NORM = re.compile(
    r"\bпо\s+(?:норматив\w*|приказ\w*|перечн\w*)|нормативн\w+\s+(?:подбор|оснащени\w*|перечн\w*)",
    re.IGNORECASE,
)
_AVAILABLE = re.compile(r"в\s+наличии|со\s+склада", re.IGNORECASE)
_REJECTION = re.compile(
    r"не\s+подход\w+|\bне\s+то\b|\bдорог\w+|\bдешевле\b|не\s+нравит\w*", re.IGNORECASE
)
_PRICE_OBJECTION = re.compile(r"\bдорог\w+|\bдешевле\b|\bбюджет\w*\s+мал", re.IGNORECASE)

# Слова постановки задачи. Они описывают закупку, а не товар, и в текстовый поиск
# не идут: иначе «нужно оснастить» ищется в описаниях товаров.
_TASK_WORDS = re.compile(
    r"\b(?:нужн\w*|надо|нам|мне|хотим|хочу|хотел\w*|подбер\w*|подбор\w*|подобра\w*|покаж\w*|показать|"
    r"оснасти\w*|оснащ\w*|укомплект\w*|закуп\w*|купи\w*|приобре\w*|помоги\w*|помочь|"
    r"пожалуйста|оборудовани\w*|для|в|во|на|по|и|или|с|со|к|до|от|из|у|за|кабинет\w*|"
    r"учреждени\w*|бюджет\w*|срок\w*|рубл\w*|тысяч\w*|тыс|млн|класс\w*|групп\w*|дет\w*|"
    r"ребят\w*|лет|год\w*|приказ\w*|норматив\w*|перечн\w*|пункт\w*|позици\w*|шт|штук\w*|"
    r"наличи\w*|без|не|что|как\w*|есть|можно|школ\w*|сад\w*|доу|сош|зал\w*|помещени\w*|"
    r"возраст\w*|учени\w*|воспитанник\w*|№|n|"
    # Просьба без предмета: «да, подберите варианты» — не товар «варианты» (прогон 14.09).
    r"вариант\w*|давайте|да|ещ[её]|посмотр\w*|подходящ\w*|товар\w*|что-нибудь|какие-нибудь|"
    # Слова разговора, а не товара: «дай консультацию», «общий подбор», «дай список всего» уходили
    # в поиск и находили фитбол с тактильными мячиками (прогон 14.09).
    r"консультаци\w*|консульт\w*|рекомендаци\w*|посовет\w*|дай|дайте|списк\w*|список|перечень|"
    r"всего|вс[её]|общ(?:ий|ая|ее|ие|его|ему|ую|им|ей)|предлож\w*|выведи|приведи|сохрани\w*|скача\w*|"
    r"файл\w*|можешь|может\w*|ты|вы|мы|наш\w*|его|их|эт(?:от|а|у|и|ой|ого|ому|им|ими|их)|то|либо|"
    r"нибудь|что-либо|полн(?:ый|ая|ое|ые|ого|ому|ую|остью)|полноценн\w*|частн\w*|открыл\w*|открыва\w*|"
    r"заказ\w*|огромн\w*|какой|какое|какая|какие|"
    # «По каталогу подбери…»: слово о каталоге, не о товаре — иначе оно становилось
    # запросом и затирало предмет разговора (прогон 21.09).
    r"каталог\w*|прайс\w*|ассортимент\w*|номенклатур\w*|"
    r"хорошо|ладно)\b",
    re.IGNORECASE,
)
_DOCUMENT_NUMBER = re.compile(r"\b(?:838|1057)\b")


def apply_text(task: ProcurementTask, text: str) -> list[str]:
    """Обновить задачу по реплике. Возвращает изменившиеся поля."""
    text = (text or "").strip()
    if not text:
        return []
    low = text.lower()
    profile = DialogProfile()
    profile.update_from_text(text)
    changed: list[str] = []

    def put(name: str, value: object) -> None:
        if value is None or getattr(task, name) == value:
            return
        setattr(task, name, value)
        changed.append(name)

    def prefer(name: str, value: object) -> None:
        if value is None or task.preferences.get(name) == value:
            return
        task.preferences[name] = value
        changed.append(name)

    if task.institution_type is None and profile.institution:
        put("institution_type", placement.institution_code(profile.institution) or profile.institution)
    put("room", placement.room_in_title(low) or profile.room)
    put("age_group", _age(low) or profile.age)
    put("grade", _grade(low))
    put("budget", _budget(profile.budget))
    put("deadline", profile.deadline)

    if profile.norm_doc_ids:
        put("norm_document", profile.norm_doc_ids[0])
        put("norm_required", True)
    point = _point(text)
    if point:
        put("norm_item", point)
        put("norm_required", True)
    if _NO_NORM.search(low):
        put("norm_required", False)
    elif _WITH_NORM.search(low):
        put("norm_required", True)

    if match := _QUANTITY.search(low):
        put("quantity", int(match.group(1)) or None)
    if match := _PARTICIPANTS.search(low):
        prefer("participants", int(match.group(1)) or None)
    if match := _GROUPS.search(low):
        prefer("groups", int(match.group(1)) or None)
    if _AVAILABLE.search(low):
        prefer("available_only", True)

    # Приветствие, вежливость и вопрос о документе — не слова о товаре.
    if intent.classify(text) not in (intent.GREETING, intent.SMALL_TALK, intent.NORM_QUESTION):
        query = query_from_text(text)
        if query:
            prefer("query", query)
    return changed


def query_from_text(text: str) -> str:
    """Слова о самом товаре — то, что остаётся после учреждения, помещения, возраста, бюджета.

    «Нужны мячи для спортзала в саду, дети 3–4 лет» → «мячи». Пусто — в реплике
    нет ничего, кроме описания задачи.
    """
    rest = text
    patterns = [
        *(re.compile(pattern, re.IGNORECASE) for _, pattern in dialog_profile._INSTITUTIONS),
        *(re.compile(pattern, re.IGNORECASE) for _, pattern in dialog_profile._ROOMS),
        *(pattern for _, pattern in placement._ROOM_PATTERNS),
        *(re.compile(pattern, re.IGNORECASE) for _, pattern in dialog_profile._AGE_GROUPS),
        *(re.compile(pattern, re.IGNORECASE) for _, pattern in dialog_profile._DEADLINES),
        dialog_profile._BUDGET,
        _GRADE, _AGE_SPAN, _AGE_FROM_TO, _QUANTITY, _PARTICIPANTS, _GROUPS, _POINT,
        _NO_NORM, _WITH_NORM, _AVAILABLE, _DOCUMENT_NUMBER,
    ]
    for pattern in patterns:
        rest = pattern.sub(" ", rest.lower())
    rest = _TASK_WORDS.sub(" ", rest)
    words = [word for word in re.findall(r"[а-яёa-z][а-яёa-z-]+", rest) if len(word) > 2]
    return " ".join(words)


def is_rejection(text: str) -> bool:
    return bool(_REJECTION.search(text or ""))


def objection_of(text: str) -> str:
    return "price" if _PRICE_OBJECTION.search(text or "") else "other"


def _age(low: str) -> str | None:
    match = _AGE_FROM_TO.search(low) or _AGE_SPAN.search(low)
    if match is None:
        return None
    return f"{int(match.group(1))}–{int(match.group(2))} лет"


def _grade(low: str) -> str | None:
    match = _GRADE.search(low)
    if match is None:
        return None
    first, last = match.group(1), match.group(2)
    return f"{int(first)}–{int(last)} классы" if last else f"{int(first)} класс"


def _budget(value: str | None) -> int | None:
    digits = "".join(ch for ch in value or "" if ch.isdigit())
    return int(digits) if digits else None


def _point(text: str) -> str | None:
    for match in _POINT.finditer(text):
        return match.group(1) or match.group(2)
    return None
