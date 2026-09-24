"""Профиль разговора: что бот уже знает о задаче.

Отдельно от истории и по белому списку полей. История — это переписка, её
приходится обрезать и маскировать; профиль — короткая выжимка, которая целиком
уходит в системный промпт под заголовком «что уже известно». Именно из-за её
отсутствия бот переспрашивал возраст детей, который ему назвали ходом раньше.

**Персональных данных здесь нет по построению.** Имя, телефон, почта, организация
и адрес в профиль не принимаются: поля перечислены явно, и все они описывают
задачу — учреждение, зону, норматив, возраст, бюджет, срок, — а не человека.
Поэтому профиль можно хранить на диске и целиком показывать модели.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from norms import reference
from norms.extract import document_ids_in_text

MAX_REMEMBERED_SKUS = 20

# --- Распознавание задачи в свободной речи -------------------------------------
#
# Пользователь описывает задачу словами, а не заполняет анкету. Разбор
# детерминированный: лишний вызов модели ради «это детский сад» стоит секунд
# сорок и легко ошибается, а перечисленные обороты покрывают почти всё, чем
# заказчику пишут в первых двух сообщениях.

_INSTITUTIONS: tuple[tuple[str, str], ...] = (
    # «В саду», «садик», «в детсаду» — так говорят чаще, чем «в детском саду»,
    # и без этих форм учреждение оставалось неизвестным. 02.09 из-за этого
    # «чем оснастить спортзал в саду» не прошло гейт карточек, и человек получил
    # три позиции с ценами без единой кнопки «В корзину».
    (
        "детский сад",
        r"детск\w*\s+сад\w*|\bдоу\b|\bдетсад\w*|\bясл\w+|дошкольн\w+|"
        r"\bсадик\w*|\bсад[уеы]\b|\bсадов\w*\s+групп\w*",
    ),
    ("школа", r"\bшкол\w+|\bгимнази\w+|\bлице\w+|\bсош\b|\bмбоу\b|начальн\w+\s+класс"),
    ("колледж", r"\bколледж\w*|\bтехникум\w*|\bспо\b"),
    ("центр развития", r"центр\w*\s+развити\w+|развивающ\w+\s+центр"),
)

_ROOMS: tuple[tuple[str, str], ...] = (
    ("спортивный зал", r"спорт\w*\s*зал\w*|спортивн\w+\s+зал\w*|физкультурн\w+\s+зал\w*"),
    ("музыкальный зал", r"музыкальн\w+\s+зал\w*|актов\w+\s+зал\w*"),
    ("кабинет логопеда", r"логопед\w*"),
    ("кабинет психолога", r"психолог\w*|сенсорн\w+\s+комнат\w*"),
    ("кабинет физики", r"кабинет\w*\s+физик\w*|физик\w*\s+кабинет"),
    ("кабинет химии", r"кабинет\w*\s+хими\w*|хими\w*\s+кабинет"),
    ("кабинет биологии", r"кабинет\w*\s+биологи\w*"),
    ("кабинет технологии", r"кабинет\w*\s+технологи\w*|мастерск\w+"),
    ("групповая комната", r"группов\w+\s+(?:комнат\w*|ячейк\w*)|\bв\s+групп\w+"),
    ("столовая", r"столов\w+|пищеблок\w*"),
    ("медицинский кабинет", r"медицинск\w+\s+кабинет|\bмедкабинет\w*|\bмедблок\w*"),
    ("библиотека", r"библиотек\w+"),
    ("игровая площадка", r"\bплощадк\w+|улич\w+\s+оборудован\w*"),
)

# Кому подбираем — по типу учреждения и по кабинету. Нужно, чтобы бот не
# обосновывал садовскую позицию школьным приказом и наоборот.
_AUDIENCE_BY_INSTITUTION = {
    "детский сад": "preschool",
    "центр развития": "preschool",
    "школа": "school",
    "колледж": "school",
}
_AUDIENCE_BY_ROOM = {
    "групповая комната": "preschool",
    "кабинет физики": "school",
    "кабинет химии": "school",
    "кабинет биологии": "school",
    "кабинет технологии": "school",
}

_AGE_RANGE = re.compile(r"\b(\d)\s*[-–—]\s*(\d{1,2})\s*лет", re.IGNORECASE)
# Одиночный возраст «дети 5 лет»: 23.09 он не парсился (нужен был диапазон), и следующий
# ход переспрашивал возраст, только что названный (сц. 4, 14).
_AGE_SINGLE = re.compile(r"\b(\d{1,2})\s*лет\b", re.IGNORECASE)
_AGE_GROUPS: tuple[tuple[str, str], ...] = (
    ("младшая группа", r"младш\w+\s+групп\w*|ясельн\w+"),
    ("средняя группа", r"средн\w+\s+групп\w*"),
    ("старшая группа", r"старш\w+\s+групп\w*|подготовительн\w+\s+групп\w*"),
    ("начальная школа", r"начальн\w+\s+(?:школ\w*|класс\w*)|1\s*[-–]\s*4\s*класс"),
    ("средняя школа", r"5\s*[-–]\s*9\s*класс|средн\w+\s+звен\w+"),
    ("старшая школа", r"10\s*[-–]\s*11\s*класс|старш\w+\s+класс"),
)

# «бюджет 200 тысяч», «до 500 тыс», «выделили 1,5 млн»
_BUDGET = re.compile(
    r"(?:бюджет\w*|уложить\w*|выделен\w*|выделил\w*|есть|до|не\s+больше|в\s+пределах)"
    r"\D{0,12}?(\d[\d\s.,]*)\s*(млн|миллион\w*|тыс\w*|т\.?\s*р\.?)?\s*(?:руб\w*|₽|р\.)?",
    re.IGNORECASE,
)
_DEADLINES: tuple[tuple[str, str], ...] = (
    ("к 1 сентября", r"к\s*1\s*сентябр\w*|\bк\s+учебн\w+\s+год\w*|\bк\s+сентябр\w+"),
    ("к концу учебного года", r"конц\w+\s+учебн\w+\s+год\w*"),
    ("до конца года", r"до\s+конц\w+\s+год\w*|\bв\s+этом\s+году"),
    ("в этом квартале", r"\bквартал\w*"),
    ("срочно", r"\bсрочн\w*|как\s+можно\s+быстрее|\bгорит\b"),
)
_REGION = re.compile(
    r"(?:город|г\.|регион|область|край|доставк\w+\s+в)\s+([А-ЯЁ][а-яё-]{2,})", re.IGNORECASE
)
# На сколько объектов комплектация: «на 6 групп», «4 кабинета», «12 комплектов».
_COUNT_OF = re.compile(r"\b(?:на\s+|по\s+)?(\d{1,2})\s*(групп\w*|кабинет\w*|комплект\w*|отделени\w*)", re.IGNORECASE)
# Явный отказ от предложенного: «это не подходит», «дорого», «не то».
_REJECTION = re.compile(
    r"не\s+подход\w+|\bне\s+то\b|\bдорог\w+|\bдешевле\b|не\s+нужн\w+", re.IGNORECASE
)
# Объект целиком, а не помещение: «открыли детский сад», «рекомендации по оснащению сада».
_NEW_OBJECT = re.compile(
    r"\bоткрыл\w*|\bоткрыва\w*|\bоснащени\w*|\bоснасти\w*|\bоснащаем\b|\bукомплект\w*|\bрекомендац\w*|"
    r"\bцеликом\b|\bпострои\w*|\bнов(?:ый|ого|ом)\s+(?:детск\w+\s+)?сад",
    re.IGNORECASE,
)
# Названа часть объекта — группа, кабинет, зал, класс, площадка: это уже не «сад целиком».
_PART_OF_OBJECT = re.compile(r"\bгрупп\w*|\bкабинет\w*|\bзал\w*|\bкласс\w*|\bплощадк\w*|\bкомнат\w*", re.IGNORECASE)


# Человеческие названия возражений — для строки «незакрытое возражение» в промпте.
_OBJECTION_NAMES = {
    "price": "цена",
    "norm": "сомнение в нормативном основании",
    "trust": "недоверие к боту или к поставщику",
    "logistics": "сроки, доставка, наличие",
    "docs": "документы для закупки",
}


@dataclass
class DialogProfile:
    """Всё, что бот выяснил о задаче. Ни одного поля о человеке."""

    institution: str | None = None
    room: str | None = None
    age: str | None = None
    # По каким документам человек оснащает. Отсюда берётся аудитория, поэтому
    # сюда попадает только то, что сказано о закупке: «подбери по приказу 1057».
    norm_doc_ids: list[str] = field(default_factory=list)
    # О каких документах он спрашивал. Спросить — не значит закупать: 31.08
    # вопрос «что значит указ 838» переключил разговор в школьный режим, и через
    # минуту детсадовский комплект был обоснован школьным приказом. Список
    # нужен модели для контекста, но на выбор перечня не влияет.
    asked_about_docs: list[str] = field(default_factory=list)
    budget: str | None = None
    deadline: str | None = None
    region: str | None = None
    # На сколько объектов комплектация: «на 6 групп», «4 кабинета». 23.09 кратность
    # не попадала ни в профиль, ни в файл — человек получал состав одной группы
    # и умножал сам (сц. 3, 21). Объектная характеристика, при смене задачи не сбрасывается.
    count: int | None = None
    count_of: str | None = None
    # Коды 1С, уже показанные пользователю, и те, что он отклонил. Нужны, чтобы
    # бот не предлагал по кругу одно и то же.
    offered: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    # Задача закупки этого разговора в Procurement Core (`core/selection.py`): подбор в
    # диалоге идёт через неё, и показанное не повторяется между ходами. Служебное поле,
    # в промпт модели не попадает.
    procurement_task_id: str | None = None

    # --- Ход разговора ---------------------------------------------------------
    #
    # Заполняет маршрутизатор (`agent/routing.py`) перед каждым ответом. Здесь
    # это живёт, а не в агенте, потому что от этих полей зависит гейт карточек, а
    # он должен переживать перезапуск: иначе после рестарта бот снова вываливает
    # позиции человеку, который только что сказал «дорого».
    stage: str = "diagnosis"  # diagnosis | presentation | objection | closing
    objection: str = "none"  # price | norm | trust | logistics | docs | none
    objection_handled: bool = False
    # Клиент готов смотреть позиции: прямо попросил показать либо согласился
    # после снятия возражения.
    ready_to_see: bool = False
    # Кто отвечал прошлым ходом и с каким намерением — состояние оркестратора
    # (`agent/routing.py`, ORCHESTRATOR.md, раздел 12). Прежний агент — только контекст:
    # маршрут решает намерение новой реплики.
    last_agent: str | None = None  # consult | sell
    intent: str | None = None
    # Комплектация, которую составил консультант: документ, раздел, позиции с количеством по
    # перечню — из результата `find_norm_item`, не из текста модели. 14.09 список жил только
    # текстом ответа, и на «сохрани в файл» выгружать было нечего.
    kit: dict[str, Any] | None = None
    # Коды 1С последнего списка «N позиций» — для файла и кнопки «Всё в корзину».
    shortlist: list[str] = field(default_factory=list)
    # Что показано последним — `kit`, `shortlist` или `order`: от этого зависят файл, «ещё» и «N позиций».
    export: str | None = None
    # Присланный заказ: файл и строки с товарами каталога — из проверки заказа, не из текста. 14.09
    # после файла «подбери по этому заказу» и «30 позиций из наличия» ни на что не ссылались.
    order: dict[str, Any] | None = None
    # Коды 1С, подобранные по формулировке приказа без точной привязки: в корзину сами не
    # кладутся, ждут отдельного «Добавить подобранное». Молчаливая замена дала пересортицу
    # в предзаказе 19.09: вместо модульного пола уехал игровой лабиринт.
    review: list[str] = field(default_factory=list)

    @property
    def audience(self) -> str | None:
        """Кому подбираем: `preschool`, `school` или ничего, пока не ясно.

        От этого зависит, каким перечнем бот вправе обосновывать позицию. Один и
        тот же товар лежит у заказчика и в садовской, и в школьной ветке каталога,
        поэтому без аудитории бот цитировал школьный приказ человеку из детского сада.

        Порядок источников — от самого надёжного: прямо названный документ, потом
        тип учреждения, и только затем кабинет. Кабинет физики бывает и в школе,
        и в колледже, а вот групповая комната — только в саду.
        """
        for doc_id in self.norm_doc_ids:
            if doc_id == "order_838":
                return "school"
            if doc_id in {"order_1057", "fgos_do", "fop_do", "func_kits"}:
                return "preschool"
        if self.institution:
            return _AUDIENCE_BY_INSTITUTION.get(self.institution)
        if self.room:
            return _AUDIENCE_BY_ROOM.get(self.room)
        return None

    @property
    def task_known(self) -> bool:
        """Достаточно ли выяснено, чтобы показывать позиции.

        Минимум — учреждение и зона: без них подбор идёт наугад, а основание
        берётся из чужого перечня. Полный набор (возраст, бюджет, срок) точнее,
        но ждать его до первой карточки нельзя: заведующая из первого сценария
        на третьем вопросе подряд отвечает «у меня нет времени заполнять анкету».
        """
        return bool(self.institution and self.room)

    @property
    def facts_known(self) -> int:
        """Сколько полей гейта заполнено — для журнала и отладки."""
        return sum(
            1
            for value in (
                self.institution,
                self.room,
                self.age,
                self.budget,
                self.deadline,
            )
            if value
        )

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.institution,
                self.room,
                self.age,
                self.norm_doc_ids,
                self.budget,
                self.deadline,
                self.region,
                self.offered,
                self.kit,
                self.order,
            )
        )

    def update_from_text(self, text: str) -> list[str]:
        """Разбор реплики пользователя. Возвращает названия изменившихся полей.

        Уже известное не перезаписывается вслепую: пользователь уточняет задачу,
        а не начинает её заново. Тип учреждения фиксируется один раз — он в
        разговоре не меняется, а вот зона, возраст и бюджет уточняются.
        """
        changed: list[str] = []
        low = (text or "").lower()
        if not low:
            return changed

        # Новый объект целиком сбрасывает прежнюю задачу. 14.09 после кабинета логопеда пришло «мы
        # открыли частный детский сад, дай рекомендации по его оснащению»: помещение осталось в
        # профиле, промпт запрещал переспрашивать — и бот снова выдал перечень логопеда.
        if self.room and whole_object(low):
            self.reset_task()
            changed.append("task")

        if self.institution is None:
            self.institution = _first_match(low, _INSTITUTIONS)
            if self.institution:
                changed.append("institution")

        room = _first_match(low, _ROOMS)
        if room and room != self.room:
            self.room = room
            changed.append("room")

        age = _age(low)
        if age and age != self.age:
            self.age = age
            changed.append("age")

        # Спросить про документ и закупать по нему — разные вещи, и путать их
        # дорого: 31.08 вопрос «что значит указ 838» перевёл весь дальнейший
        # разговор в школьный режим, и садовский комплект получил школьное
        # основание. Вопрос идёт в отдельный список, на аудиторию не влияющий.
        asking = reference.question_about_document(text) is not None
        target = self.asked_about_docs if asking else self.norm_doc_ids
        for doc_id in document_ids_in_text(text):
            if doc_id not in target:
                target.append(doc_id)
                changed.append("norm")

        budget = _budget(low)
        if budget and budget != self.budget:
            self.budget = budget
            changed.append("budget")

        deadline = _first_match(low, _DEADLINES)
        if deadline and deadline != self.deadline:
            self.deadline = deadline
            changed.append("deadline")

        region = _REGION.search(text or "")
        if region and region.group(1).capitalize() != self.region:
            self.region = region.group(1).capitalize()
            changed.append("region")

        count = _count_of(low)
        if count and count[0] != self.count:
            self.count, self.count_of = count
            changed.append("count")

        if _REJECTION.search(low) and self.offered:
            # Отклонили то, что показали последним: конкретную позицию пользователь
            # называет редко, а «дорого» почти всегда относится к последней выдаче.
            for sku in self.offered[-3:]:
                if sku not in self.rejected:
                    self.rejected.append(sku)
            changed.append("rejected")
        return changed

    def reset_task(self) -> None:
        """Новая задача: забыть помещение, возраст, показанное, комплектацию и задачу закупки.

        Учреждение, документ, бюджет и срок остаются — они про объект, а не про кабинет.
        """
        self.room = None
        self.age = None
        self.offered = []
        self.rejected = []
        self.procurement_task_id = None
        self.kit = None
        self.shortlist = []
        self.export = None
        self.order = None
        self.review = []

    def remember_kit(self, kit: dict[str, Any]) -> None:
        self.kit = kit
        self.export = "kit"

    def remember_order(self, order: dict[str, Any]) -> None:
        self.order = order
        self.export = "order"

    def remember_offered(self, skus: list[str]) -> None:
        for sku in skus:
            if sku not in self.offered:
                self.offered.append(sku)
        if len(self.offered) > MAX_REMEMBERED_SKUS:
            del self.offered[:-MAX_REMEMBERED_SKUS]

    def as_prompt(self) -> str:
        """Профиль в виде, который читает модель.

        Пустой профиль даёт пустую строку: заголовок «что уже известно» без
        содержимого сбивает модель сильнее, чем его отсутствие.
        """
        if self.is_empty:
            return ""
        lines = ["## Что уже известно о задаче", ""]
        for label, value in (
            ("Учреждение", self.institution),
            ("Зона или кабинет", self.room),
            ("Возраст детей", self.age),
            ("Бюджет", self.budget),
            ("Срок", self.deadline),
            ("Регион", self.region),
            ("Комплектация на сколько объектов", f"{self.count} {self.count_of}" if self.count else None),
        ):
            if value:
                lines.append(f"- {label}: {value}")
        if self.norm_doc_ids:
            lines.append(f"- Оснащает по документу: {', '.join(_doc_names(self.norm_doc_ids))}")
        if self.asked_about_docs:
            lines.append(
                "- Спрашивал про документ (это не значит, что закупает по нему): "
                f"{', '.join(_doc_names(self.asked_about_docs))}"
            )
        if self.offered:
            lines.append(f"- Уже показано позиций: {len(self.offered)}")
        if self.rejected:
            lines.append(
                f"- Отклонено пользователем, повторно не предлагать: {len(self.rejected)}"
            )
        if self.objection != "none" and not self.objection_handled:
            lines.append(f"- Незакрытое возражение: {_OBJECTION_NAMES.get(self.objection, self.objection)}")
        if self.kit:
            count = len(self.kit.get("positions") or [])
            names = _doc_names([self.kit.get("document") or ""])
            lines.append(
                f"- Составлена комплектация: {names[0] + ', ' if names else ''}раздел {self.kit.get('code')} "
                f"«{self.kit.get('title')}», позиций {count}; полный список человек скачивает файлом"
            )
        if self.order:
            positions = self.order.get("positions") or []
            found = sum(1 for position in positions if position.get("sku"))
            lines.append(
                f"- Прислан заказ «{self.order.get('file')}»: строк {len(positions)}, товаров каталога нашлось "
                f"{found}; список по заказу бот выдаёт сам"
            )
        lines += [
            "",
            "Это уже сказано пользователем. Переспрашивать перечисленное не нужно. Если новая реплика меняет "
            "задачу — другое помещение или объект целиком, — отвечай на новую реплику, а не на прежнюю задачу.",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "institution": self.institution,
            "room": self.room,
            "age": self.age,
            "norm_doc_ids": self.norm_doc_ids,
            "asked_about_docs": self.asked_about_docs,
            "budget": self.budget,
            "deadline": self.deadline,
            "region": self.region,
            "count": self.count,
            "count_of": self.count_of,
            "offered": self.offered,
            "rejected": self.rejected,
            "procurement_task_id": self.procurement_task_id,
            "stage": self.stage,
            "objection": self.objection,
            "objection_handled": self.objection_handled,
            "ready_to_see": self.ready_to_see,
            "last_agent": self.last_agent,
            "intent": self.intent,
            "kit": self.kit,
            "shortlist": self.shortlist,
            "export": self.export,
            "order": self.order,
            "review": self.review,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> DialogProfile:
        """Чтение с диска по белому списку.

        Лишние ключи отбрасываются молча: файл состояния переживает обновления
        бота, а неизвестное поле не должно ронять диалог.
        """
        known = set(cls().to_dict())
        return cls(**{key: value for key, value in (raw or {}).items() if key in known})


def whole_object(text: str) -> bool:
    """Объект целиком, без помещения: «мы открыли детский сад, дай рекомендации по оснащению».

    «Открываем новую группу в детском саду» — это группа, а не сад: часть объекта названа, хотя в
    списке помещений такой формы нет.
    """
    low = (text or "").lower()
    return bool(
        _NEW_OBJECT.search(low)
        and _first_match(low, _INSTITUTIONS)
        and not _first_match(low, _ROOMS)
        and not _PART_OF_OBJECT.search(low)
    )


def _first_match(low: str, rules: tuple[tuple[str, str], ...]) -> str | None:
    for label, pattern in rules:
        if re.search(pattern, low, re.IGNORECASE):
            return label
    return None


def _age(low: str) -> str | None:
    match = _AGE_RANGE.search(low)
    if match:
        return f"{match.group(1)}–{match.group(2)} лет"
    single = _AGE_SINGLE.search(low)
    if single and 1 <= int(single.group(1)) <= 18:
        return f"{single.group(1)} лет"
    return _first_match(low, _AGE_GROUPS)


def _count_of(low: str) -> tuple[int, str] | None:
    """Кратность комплектации: «на 6 групп» → (6, «группы»)."""
    match = _COUNT_OF.search(low)
    if not match or int(match.group(1)) < 2:
        return None
    unit = match.group(2).lower()
    for label, stem in (("группы", "групп"), ("кабинеты", "кабинет"), ("комплекты", "комплект"), ("отделения", "отделени")):
        if unit.startswith(stem):
            return int(match.group(1)), label
    return None


def _budget(low: str) -> str | None:
    match = _BUDGET.search(low)
    if match is None:
        return None
    raw = match.group(1).replace(" ", "").replace(",", ".").rstrip(".")
    try:
        amount = float(raw)
    except ValueError:
        return None
    unit = (match.group(2) or "").lower().replace(" ", "")
    if unit.startswith(("млн", "миллион")):
        amount *= 1_000_000
    elif unit.startswith(("тыс", "т.р", "тр")):
        amount *= 1_000
    # Меньше десяти тысяч на оснащение кабинета не бывает — почти наверняка
    # под маску попало число из другого предложения: возраст, класс, количество.
    if amount < 10_000:
        return None
    return f"до {int(amount):,} ₽".replace(",", " ")


def _doc_names(doc_ids: list[str]) -> list[str]:
    from norms import documents as docs

    names = []
    for doc_id in doc_ids:
        if doc_id in docs.DOCUMENTS:
            names.append(docs.get(doc_id).short_name)
    return names
