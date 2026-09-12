"""Размещение товара в каталоге: учреждение, помещение, раздел, возраст.

Всё выводится из дерева разделов, которое разложил сам заказчик, а не из
нормативного движка (docs/DECISIONS.md, D8). Разбор детерминированный: раздел,
название которого не узнали, даёт «не определено», а не догадку.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from catalog.text import stems

PRESCHOOL = "preschool"
SCHOOL = "school"

# Корневые разделы каталога заказчика и то, кому они адресованы. «Оснащение
# новостроек» отнесено к садам не на глаз: все 1 048 позиций этой ветки привязаны
# к приказу № 1057. «Коррекционная среда» и «Инновационные решения» смешанные,
# поэтому их здесь нет — учреждение у них не определено.
AUDIENCE_BY_ROOT = {
    "ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА": PRESCHOOL,
    "ОСНАЩЕНИЕ НОВОСТРОЕК": PRESCHOOL,
    "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838": SCHOOL,
}

# Помещения, которые заказчик выделил разделами. Шаблоны разобраны на настоящих
# названиях выгрузки от 26.08.2026: «Подраздел 18. Кабинет информатики»,
# «1.5 Спортивный зал», «04. Игровые пособия и материалы для кабинета логопеда».
#
# Где помещение уже есть в профиле разговора (core/profile.py), название взято
# оттуда, иначе кабинет из профиля не находил бы своих товаров. Шаблоны свои:
# профиль разбирает речь, а в каталоге «02.16 Мастерская» — сюжетная игра, а не
# кабинет труда. Поэтому предметные кабинеты узнаются только со словом «кабинет».
_ROOMS: tuple[tuple[str, str], ...] = (
    ("кабинет логопеда", r"логопед"),
    # Сенсорную комнату профиль разговора уже считает кабинетом психолога.
    ("кабинет психолога", r"психолог|сенсорн\w*\s+комнат"),
    ("кабинет дефектолога", r"дефектолог"),
    ("кабинет информатики", r"кабинет\w*\s+информатик"),
    ("кабинет физики", r"кабинет\w*\s+физик"),
    ("кабинет химии", r"кабинет\w*\s+хими"),
    ("кабинет биологии", r"кабинет\w*\s+биологи"),
    ("кабинет технологии", r"кабинет\w*\s+(?:труда|технологи)"),
    ("кабинет географии", r"кабинет\w*\s+географи"),
    ("кабинет истории", r"кабинет\w*\s+истори"),
    ("кабинет музыки", r"кабинет\w*\s+музык"),
    ("кабинет ИЗО", r"кабинет\w*\s+(?:изобразительн|изо\b)"),
    ("кабинет ОБЗР", r"кабинет\w*\s+(?:основ\w*\s+безопасност|обзр)"),
    ("кабинет начальных классов", r"кабинет\w*\s+начальн\w*\s+класс"),
    ("кабинет проектной деятельности", r"кабинет\w*\s+проектн"),
    ("кабинет дополнительного образования", r"кабинет\w*\s+дополнительн"),
    # «Спортивный комплекс» — так назван спортзал в школьной ветке по приказу 838.
    (
        "спортивный зал",
        r"спортзал|спортивн\w*\s+(?:зал|комплекс)|физкультурн\w*\s+зал"
        r"|спортивн\w*\s+оборудовани\w*\s+для\s+зала",
    ),
    ("музыкальный зал", r"музыкальн\w*\s+зал"),
    ("бассейн", r"бассейн"),
    ("групповая комната", r"группов\w*\s+(?:помещени|комнат|ячейк)"),
    ("игровая продлённого дня", r"игров\w*\s+(?:для\s+)?(?:групп\w*\s+)?продл"),
)
_ROOM_PATTERNS = tuple((name, re.compile(pattern)) for name, pattern in _ROOMS)
ROOM_NAMES = tuple(name for name, _ in _ROOMS)

# «1.14.3 Групповые помещения для детей 1 - 2 лет», «… для детей до 1 года».
_GROUP_AGE = re.compile(r"детей\s+(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*(?:год|лет)")
_GROUP_UNDER = re.compile(r"детей\s+до\s+(\d{1,2})\s*(?:год|лет)")

_AGE_FROM_TO = re.compile(r"от\s+(\d{1,2})\s+до\s+(\d{1,2})")
_AGE_SPAN = re.compile(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})")
_AGE_UNDER = re.compile(r"\bдо\s+(\d{1,2})")
_AGE_PLUS = re.compile(r"(\d{1,2})\s*\+")
_AGE_EXACT = re.compile(r"^(\d{1,2})\s*(?:год\w*|лет)?$")

# «Подраздел 18. », «Раздел 2. », «12.04 », «1.13.1 »
_NUMBERING = re.compile(r"^\s*(?:(?:под)?раздел\s+\d+\.?|\d+(?:\.\d+)*\.?)\s*", re.IGNORECASE)

# «Дошкольное» содержит «школ», поэтому сад проверяется первым.
_PRESCHOOL_WORDS = re.compile(r"сад|\bдоу\b|дошкол|\bясл")
_SCHOOL_WORDS = re.compile(r"школ|гимнази|лице[йия]|\bсош\b")


@dataclass(frozen=True)
class AgeRange:
    """Возраст в годах. `max_years=None` — без верхней границы, как «3+» на сайте."""

    min_years: int
    max_years: int | None = None

    def overlaps(self, other: AgeRange) -> bool:
        return self.min_years <= _upper(other) and other.min_years <= _upper(self)


@dataclass(frozen=True)
class Placement:
    """Одно размещение товара: путь от корня и то, что из него следует."""

    path: tuple[str, ...]
    institution: str | None
    room: str | None
    age: AgeRange | None

    @property
    def sections(self) -> tuple[str, ...]:
        """Разделы ниже корня без нумерации: «Кабинет информатики», «Мячи»."""
        return tuple(section_title(title) for title in self.path[1:])


def placement_of(path: Iterable[str]) -> Placement:
    """Размещение по пути в дереве.

    Кабинет и возраст берутся с самого глубокого раздела, где они названы. В
    выгрузке «Оснащения новостроек» кабинеты вложены в «1.6 Бассейн» — по первому
    совпадению весь кабинет логопеда оказался бы бассейном.
    """
    titles = tuple(path)
    return Placement(
        path=titles,
        institution=institution_of_root(titles[0]) if titles else None,
        room=_deepest(titles, room_in_title),
        age=_deepest(titles, age_in_title),
    )


def institution_of_root(root: str) -> str | None:
    return AUDIENCE_BY_ROOT.get(root.strip().upper())


def institution_code(value: str | None) -> str | None:
    """`preschool` / `school` из кода или слова: «детский сад», «ДОУ», «школа».

    Колледж и центр развития не угадываются: в каталоге для них веток нет.
    """
    text = _normalize(value or "")
    if text in (PRESCHOOL, SCHOOL):
        return text
    if _PRESCHOOL_WORDS.search(text):
        return PRESCHOOL
    if _SCHOOL_WORDS.search(text):
        return SCHOOL
    return None


def room_in_title(title: str) -> str | None:
    text = _normalize(title)
    for name, pattern in _ROOM_PATTERNS:
        if pattern.search(text):
            return name
    return None


def room_in(value: str | None) -> str | None:
    """Помещение из запроса: название из профиля, «кабинет информатики» или «химия»."""
    text = _normalize(value or "")
    if not text:
        return None
    for name in ROOM_NAMES:
        if _normalize(name) == text:
            return name
    return room_in_title(text) or room_in_title(f"кабинет {text}")


def age_in_title(title: str) -> AgeRange | None:
    text = _normalize(title)
    if match := _GROUP_AGE.search(text):
        return _age(match.group(1), match.group(2))
    if match := _GROUP_UNDER.search(text):
        return _age("0", match.group(1))
    return None


def parse_age(value: str | None) -> AgeRange | None:
    """Возраст из запроса или карточки: «3–4 года», «от 3 до 5», «до 1 года», «3+», «5 лет».

    Названия групп («младшая группа») в годы не переводятся: это соглашение
    программы, а не данные каталога.
    """
    text = _normalize(value or "")
    if match := _AGE_FROM_TO.search(text) or _AGE_SPAN.search(text):
        return _age(match.group(1), match.group(2))
    if match := _AGE_UNDER.search(text):
        return _age("0", match.group(1))
    if match := _AGE_PLUS.search(text):
        return AgeRange(int(match.group(1)))
    if match := _AGE_EXACT.match(text):
        return _age(match.group(1), match.group(1))
    return None


def section_title(title: str) -> str:
    return _NUMBERING.sub("", title, count=1).strip()


def matches_category(placement: Placement, category: str) -> bool:
    """Раздел ниже корня содержит все слова категории: «мячи» → «12.04.1 Мячи игровые»."""
    wanted = set(stems(category))
    if not wanted:
        return False
    return any(wanted <= set(stems(title)) for title in placement.sections)


def _deepest[T](titles: tuple[str, ...], find: Callable[[str], T | None]) -> T | None:
    for title in reversed(titles):
        found = find(title)
        if found is not None:
            return found
    return None


def _age(low: str, high: str) -> AgeRange | None:
    low_years, high_years = int(low), int(high)
    if high_years < low_years:
        return None
    return AgeRange(low_years, high_years)


def _upper(age: AgeRange) -> int:
    return age.max_years if age.max_years is not None else 99


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())
