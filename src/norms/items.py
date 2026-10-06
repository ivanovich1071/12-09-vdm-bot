"""Пункты перечней из текстов самих приказов.

Раньше бот знал только номер пункта — «позиция 2.4.35». Что за этим номером стоит,
не знал ни он, ни пользователь: чтобы это выяснить, надо было открыть приказ на
147 страницах и найти строку глазами. Теперь формулировка берётся из документа
дословно и показывается рядом с товаром.

Разбор отдельный для каждого приказа: у них разная вёрстка.

**838** — обычный список: «2.4.35. Дидактические пособия и обучающие игры…».
Номер стоит в начале строки, после него точка. Разделы и подразделы идут
заголовками, их запоминаем — по ним видно, что 2.4 это кабинет учителя-логопеда.

**1057** — таблица, из которой pdf вынимает текст построчно и с двумя видами
порчи. Номер иногда склеивается с названием («1.13.4.3.1.2Игровой комплект»),
а иногда, наоборот, разрывается пробелом («1.13.4.3.1.1 0» — это пункт
1.13.4.3.1.10). Из-за второго 482 наших пункта «не находились» в приказе, хотя
были в нём. Обе порчи чиним до разбора, иначе сверка врёт.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_ITEMS = Path("data/kb/norm_items.json")

# Единицы измерения из таблицы 1057. По ним отделяется название пункта от
# количества: название кончается там, где начинается единица.
_UNITS = ("Шт.", "Компл.", "Комплект", "Набор", "Пар", "Пара", "Ед.", "М2", "М.")

# Заголовки разделов в 838: «Раздел 2. Комплекс оснащения предметных кабинетов»,
# «Подраздел 4. Кабинет учителя-логопеда».
_HEADING = re.compile(r"^(Раздел|Подраздел)\s+(\d+)\.\s*(.+)$")

# Пункт перечня в 838: номер, точка, название.
_ITEM_838 = re.compile(r"^(\d+(?:\.\d+){1,4})\.\s+(\S.*)$")

# Пункт перечня в 1057: номер в начале строки, дальше название. Название может
# начаться и со следующей строки — после починки разорванного номера он часто
# остаётся на строке один.
_ITEM_1057 = re.compile(r"^(\d+(?:\.\d+){1,5})(?:\s+(\S.*))?$")

# Разорванный номер: «1.13.4.3.1.1 0 Комплект». Цифру после пробела возвращаем
# на место, но только если дальше начинается название, а не количество.
_SPLIT_CODE = re.compile(r"(\d(?:\.\d+){2,})\s(\d)(?=\s+[А-ЯЁA-Z«\"(])")

# Номер, склеенный с названием: «1.13.4.3.1.2Игровой».
_GLUED_CODE = re.compile(r"(\d(?:\.\d+){2,})(?=[А-ЯЁA-Z«\"(])")

# Общая позиция: «Позиция 2.13 является общей для следующих подразделов
# (предметных кабинетов) и приобретаются в каждый из них:», «Позиции 2.1-2.12
# являются общими…» (диапазон через тире), «Позиции 2.16, 2.17 являются общими…».
# Кабинеты перечислены строками «Подраздел N. Название» после фразы.
_COMMON_PHRASE = re.compile(r"^Позици[ия] (?P<codes>[0-9.\-,– —]+?) явля[ею]тся общ[еи]\w*")


@dataclass(frozen=True)
class NormItem:
    """Пункт перечня так, как он написан в приказе."""

    doc_id: str
    code: str
    title: str
    # Раздел и подраздел, в которых пункт стоит. В 838 по ним видно назначение
    # («Кабинет учителя-логопеда»), в 1057 — направление развития.
    section: str | None = None
    unit: str | None = None
    quantity: str | None = None
    # Кабинеты, в которые приказ велит покупать общую позицию («2.15. Конторка» —
    # в кабинет химии наравне с его пунктами 2.15.1–2.15.127). У обычного пункта
    # список пуст.
    cabinets: tuple[str, ...] = ()

    @property
    def full_title(self) -> str:
        if self.section:
            return f"{self.title} ({self.section})"
        return self.title


def parse_838_meta(text: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Карта подразделов и карта общих позиций из фраз приказа (шаг 3.4).

    «Позиция 2.15 является общей для следующих подразделов…» перечисляет кабинеты
    строками «Подраздел N. Название» — это те же настоящие заголовки подразделов,
    поэтому одна карта обслуживает оба вопроса: чей пункт (подраздел по цифрам)
    и кому позиция общая (кабинеты из перечня). Перечень кабинетов тянется до
    ближайшего пункта перечня: колонтитулы страниц и шум вёрстки ему не помеха.
    """
    subsections: dict[str, str] = {}
    common: dict[str, list[str]] = {}
    pending: list[str] = []
    chapter: str | None = None

    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        phrase = _COMMON_PHRASE.match(line)
        if phrase:
            pending = _expand_codes(phrase.group("codes"))
            # Кабинеты перечня принадлежат главе самой позиции: «Позиции 3.1-3.6…
            # Подраздел 1. Студия» — это 3.1, а не тёзка из главы 2.
            if pending:
                chapter = pending[0].split(".")[0]
            continue
        heading = _HEADING.match(line)
        if heading:
            kind, number, title = heading.groups()
            title = title.strip(" .*")
            if kind == "Раздел":
                chapter = number
                pending = []
            elif chapter is not None:
                subsections.setdefault(f"{chapter}.{number}", title)
                if pending:
                    for code in pending:
                        common.setdefault(code, []).append(f"{chapter}.{number} {title}")
            continue
        if _ITEM_838.match(line):
            pending = []
    return subsections, common


def _expand_codes(codes: str) -> list[str]:
    """«2.1-2.12», «2.16, 2.17» → список кодов одной глубины с общим префиксом."""
    found: list[str] = []
    for part in re.split(r"[,;]", codes):
        part = part.strip().replace("–", "-").replace("—", "-")
        if not part:
            continue
        if "-" not in part:
            found.append(part)
            continue
        left, right = part.split("-", 1)
        lp, rp = left.split("."), right.split(".")
        if len(lp) == len(rp) and lp[:-1] == rp[:-1] and lp[-1].isdigit() and rp[-1].isdigit():
            prefix = ".".join(lp[:-1])
            found.extend(f"{prefix}.{n}" for n in range(int(lp[-1]), int(rp[-1]) + 1))
        else:
            found.append(part)
    return found


def parse_838(text: str) -> list[NormItem]:
    """Раздел пункта определяется его собственным номером, а не позицией строки.

    pypdf вынимает текст страницы не по порядку: на странице 18 заголовки
    «Подраздел 3» и «Подраздел 4» приезжают после «Подраздел 21», а блок пунктов
    2.1–2.17 — раньше своих заголовков. Прежнее «липкое» наследование подписывало
    им чужой подраздел, и почти весь раздел 2 становился «Кабинетом учителя-
    логопеда». Поэтому заголовки собираются в карту «номер → название», и пункт
    2.15.36 получает «Кабинет химии» по своим цифрам — в каком порядке строки ни
    приезжай из выгрузки.

    Позиции второго уровня (2.1–2.17, 3.1–3.6) подразделами не подписываются
    вовсе: у приказа совпадают номера позиции и подраздела, и «2.15. Конторка»
    получала подпись «Кабинет химии» по чужим цифрам. Общая позиция остаётся без
    раздела, а список её кабинетов берётся из фраз «является общей…».
    """
    sections: dict[str, str] = {}

    for raw in text.splitlines():
        line = " ".join(raw.split())
        heading = _HEADING.match(line)
        if heading:
            kind, number, title = heading.groups()
            if kind == "Раздел":
                sections.setdefault(number, title.strip(" .*"))

    subsections, common = parse_838_meta(text)
    items: list[NormItem] = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        match = _ITEM_838.match(line)
        if not match:
            continue
        code, title = match.groups()
        parts = code.split(".")
        if len(parts) == 2:
            items.append(
                NormItem(
                    doc_id="order_838",
                    code=code,
                    title=title.strip(" .*"),
                    section=None,
                    cabinets=tuple(common.get(code, [])),
                )
            )
            continue
        items.append(
            NormItem(
                doc_id="order_838",
                code=code,
                title=title.strip(" .*"),
                section=subsections.get(f"{parts[0]}.{parts[1]}") or sections.get(parts[0]),
            )
        )
    return items


def section_conflicts(known: dict[str, dict[str, NormItem]]) -> list[str]:
    """Признак «липких» разделов: одно название накрыло несколько подразделов.

    При прошлом сбое «Кабинет учителя-логопеда» числился разделом у пунктов
    2.1, 2.12–2.17 сразу. Здоровый справочник держит у одного подраздела —
    первых двух цифр кода — одно название раздела.
    """
    conflicts: list[str] = []
    for doc_id, by_code in known.items():
        owner: dict[str, set[str]] = {}
        for code, item in by_code.items():
            if not item.section:
                continue
            parts = code.split(".")
            if len(parts) < 2:
                continue
            owner.setdefault(item.section, set()).add(".".join(parts[:2]))
        for section, groups in sorted(owner.items()):
            if len(groups) > 1:
                conflicts.append(
                    f"{doc_id}: раздел «{section}» накрывает подразделы {sorted(groups)}"
                )
    return conflicts


def parse_1057(text: str) -> list[NormItem]:
    """Разбор таблицы. Название пункта переносится на несколько строк.

    Поэтому строки накапливаются до следующего номера, а потом из накопленного
    отделяются единица измерения и количество.
    """
    items: list[NormItem] = []
    code: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if code is None:
            return
        title, unit, quantity = _split_tail(" ".join(buffer))
        if title:
            items.append(
                NormItem(
                    doc_id="order_1057",
                    code=code,
                    title=title,
                    unit=unit,
                    quantity=quantity,
                )
            )

    for raw in _repaired(text).splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        match = _ITEM_1057.match(line)
        if match:
            flush()
            code, rest = match.groups()
            buffer = [rest] if rest else []
        elif code is not None:
            buffer.append(line)
    flush()

    # Один и тот же пункт встречается на нескольких страницах (шапка таблицы
    # повторяется). Оставляем первое вхождение — оно полное.
    unique: dict[str, NormItem] = {}
    for item in items:
        unique.setdefault(item.code, item)
    return list(unique.values())


def _repaired(text: str) -> str:
    """Чинит номера, испорченные вёрсткой таблицы."""
    previous = None
    while previous != text:
        previous = text
        text = _SPLIT_CODE.sub(r"\1\2", text)
    return _GLUED_CODE.sub(r"\1 ", text)


def _split_tail(text: str) -> tuple[str, str | None, str | None]:
    """Отделяет от накопленного хвост таблицы: единицу измерения и количество."""
    text = " ".join(text.split()).strip()
    for unit in _UNITS:
        position = text.find(f" {unit}")
        if position <= 0:
            continue
        title = text[:position].strip(" -–—")
        tail = text[position + len(unit) + 1 :].strip(" +")
        return title, unit, " ".join(tail.split()) or None
    return text.strip(" +"), None, None


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def build(sources: dict[str, Path], out: Path = DEFAULT_ITEMS) -> dict[str, int]:
    """Собирает справочник пунктов из PDF приказов.

    Приказы в git не хранятся (как и любые PDF заказчика), поэтому команда
    запускается вручную у того, у кого файлы лежат рядом с проектом. Рядом с
    пунктами пишутся карты подразделов и общих позиций — в 838 без них
    комплектация кабинета подписана позицией-тёзкой (шаг 3.4).
    """
    parsers = {"order_838": parse_838, "order_1057": parse_1057}
    meta_parsers = {"order_838": parse_838_meta}
    collected: dict[str, list[dict]] = {}
    subsections: dict[str, dict[str, str]] = {}
    common: dict[str, dict[str, list[str]]] = {}

    for doc_id, path in sources.items():
        if doc_id not in parsers or not path.exists():
            continue
        text = read_pdf(path)
        items = parsers[doc_id](text)
        collected[doc_id] = [asdict(item) for item in items]
        if doc_id in meta_parsers:
            doc_subsections, doc_common = meta_parsers[doc_id](text)
            if doc_subsections:
                subsections[doc_id] = doc_subsections
            if doc_common:
                common[doc_id] = doc_common

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {**collected, "subsections": subsections, "common_positions": common},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return {doc_id: len(items) for doc_id, items in collected.items()}


class ItemIndex:
    """Справочник пунктов с поиском по словам.

    Появился из-за того, что модель не могла найти пункт по смыслу. Она знала
    номера из разговора и достраивала остальное сама: «Спортивный комплекс
    соответствует пункту 2.20.63 приказа 1057», хотя 2.20.63 — это фрезерный
    станок из приказа 838, а спортивное оборудование в 1057 стоит под 1.5.1.
    Тексты приказов у нас разобраны давно, не хватало только способа спросить
    их словами.

    Поиск нарочно простой: совпадение основ слов, длинные пункты чуть ниже
    коротких. Это не полнотекстовый движок, это замена выдумыванию.
    """

    def __init__(
        self,
        items: dict[str, dict[str, NormItem]],
        meta: dict[str, dict[str, dict]] | None = None,
    ) -> None:
        self.items = items
        meta = meta or {}
        self.subsections: dict[str, dict[str, str]] = {
            doc_id: dict(m.get("subsections", {})) for doc_id, m in meta.items()
        }
        self.common: dict[str, dict[str, list[str]]] = {
            doc_id: dict(m.get("common", {})) for doc_id, m in meta.items()
        }
        self._tokens: dict[tuple[str, str], set[str]] = {}
        for doc_id, by_code in items.items():
            for code, item in by_code.items():
                extra = " ".join(item.cabinets)
                self._tokens[(doc_id, code)] = _stems(f"{item.title} {item.section or ''} {extra}")

    @property
    def loaded(self) -> bool:
        return bool(self.items)

    def get(self, doc_id: str, code: str) -> NormItem | None:
        return self.items.get(doc_id, {}).get(code)

    def subsection(self, doc_id: str, code: str) -> str | None:
        """Имя подраздела приказа по коду: «2.15.36» и «2.15» → «Кабинет химии».

        Совпадение номеров позиции и подраздела — устройство приказа 838: позиция
        2.15 «Конторка» — общая для кабинетов, а пункты 2.15.1–2.15.127 — сам
        кабинет химии. Хранятся они раздельно (шаг 3.4), чтобы комплектация
        кабинета подписывалась кабинетом, а не позицией-тёзкой.
        """
        maps = self.subsections.get(doc_id)
        if not maps:
            return None
        parts = code.split(".")
        for size in range(len(parts), 0, -1):
            name = maps.get(".".join(parts[:size]))
            if name:
                return name
        return None

    def documents_with(self, code: str) -> list[str]:
        """В каких приказах есть пункт с таким номером."""
        return sorted(doc_id for doc_id, by_code in self.items.items() if code in by_code)

    def count(self, doc_id: str) -> int:
        return len(self.items.get(doc_id, {}))

    def parents(self, doc_id: str, code: str) -> list[NormItem]:
        """Разделы, в которых стоит пункт, — от верхнего к ближайшему.

        Без них «1.14.2.7.2 Спортивный инвентарь» выглядит как пункт про спортзал, хотя
        это групповые помещения для детей до года. 14.09 консультант выдал такие пункты
        на «оснастить спортзал», и подбор по ним в спортзале ничего не нашёл.
        """
        parts = code.split(".")
        found = (self.get(doc_id, ".".join(parts[:size])) for size in range(1, len(parts)))
        return [item for item in found if item is not None]

    def children(self, doc_id: str, code: str) -> list[NormItem]:
        """Пункты раздела в порядке номеров, со вложенными подразделами.

        Комплектация кабинета (шаг 3.4) — не только его собственные пункты:
        приказ относит к кабинету и общие позиции. В кабинет химии входят и
        2.15.1–2.15.127, и общая «2.15. Конторка», и доска со столами из блока
        2.1–2.17, которых раньше в «полном комплекте» не было.
        """
        prefix = f"{code}."
        found = [item for key, item in self.items.get(doc_id, {}).items() if key.startswith(prefix)]
        found += self._common_positions(doc_id, code)
        return sorted(found, key=lambda item: [int(part) for part in item.code.split(".") if part.isdigit()])

    def _common_positions(self, doc_id: str, code: str) -> list[NormItem]:
        """Общие позиции, которые приказ относит к этому подразделу.

        Кабинеты в карте общих позиций хранятся строкой «2.15 Кабинет химии» —
        матчится код кабинета, а не название: названия подразделов могут
        повторяться или сокращаться.
        """
        if code not in (self.subsections.get(doc_id) or {}):
            return []
        return [
            item
            for pos_code, cabinets in self.common.get(doc_id, {}).items()
            if any(cabinet.split(" ", 1)[0] == code for cabinet in cabinets)
            and (item := self.get(doc_id, pos_code)) is not None
        ]

    def search(self, text: str, doc_id: str | None = None, limit: int = 5) -> list[NormItem]:
        from catalog.text import expand

        wanted = _stems(text)
        if not wanted:
            return []
        # «Спортзал» в приказе называется «спортивным оборудованием», «мастерская» —
        # «кабинетом технологии». Раскрываем запрос теми же синонимами, что и в
        # каталоге, иначе поиск по смыслу молчит ровно там, где он нужен.
        wanted |= {token for token in expand(sorted(wanted)) if len(token) > 2}
        scored: list[tuple[float, NormItem]] = []
        for (item_doc, code), tokens in self._tokens.items():
            if doc_id and item_doc != doc_id:
                continue
            common = wanted & tokens
            if not common:
                continue
            # Доля запроса, которую пункт покрыл, минус наказание за многословие:
            # иначе абзац на сорок слов обгоняет точную формулировку из трёх.
            score = len(common) / len(wanted) - 0.01 * len(tokens)
            scored.append((score, self.items[item_doc][code]))
        scored.sort(key=lambda pair: (-pair[0], pair[1].code))
        return [item for _, item in scored[:limit]]


def _stems(text: str) -> set[str]:
    from catalog.text import stems

    return {token for token in stems(text) if len(token) > 2}


def load(path: Path = DEFAULT_ITEMS) -> dict[str, dict[str, NormItem]]:
    """Справочник в память: документ → номер пункта → пункт.

    Файла может не быть — приказы лежат не у всех. Тогда бот работает как раньше,
    называя номер пункта без формулировки.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    result: dict[str, dict[str, NormItem]] = {}
    for doc_id, items in raw.items():
        # Карты подразделов и общих позиций лежат в том же файле — это словари,
        # а не списки пунктов (шаг 3.4).
        if not isinstance(items, list):
            continue
        result[doc_id] = {
            item["code"]: NormItem(
                doc_id=doc_id,
                code=item["code"],
                title=item.get("title", ""),
                section=item.get("section"),
                unit=item.get("unit"),
                quantity=item.get("quantity"),
                cabinets=tuple(item.get("cabinets") or ()),
            )
            for item in items
        }
    return result


def load_meta(path: Path = DEFAULT_ITEMS) -> dict[str, dict[str, dict]]:
    """Карты подразделов и общих позиций из того же файла, по документу.

    {"order_838": {"subsections": {"2.15": "Кабинет химии", …},
                   "common": {"2.15": ["2.1 Кабинет начальных классов", …]}}}
    Файла может не быть — тогда подразделов нет и бот подписывает пункты,
    как раньше.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    meta: dict[str, dict[str, dict]] = {}
    for key, field in (("subsections", "subsections"), ("common_positions", "common")):
        for doc_id, payload in (raw.get(key) or {}).items():
            if isinstance(payload, dict):
                meta.setdefault(doc_id, {})[field] = payload
    return meta
