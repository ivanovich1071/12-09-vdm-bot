"""Оркестратор диалога: кто отвечает на реплику — консультант или продавец.

Устроен по ORCHESTRATOR.md заказчика (14.09). Сам он не консультирует и не продаёт:
определяет намерение текущей реплики и выбирает агента. Главное правило — маршрут
решает НОВОЕ намерение человека, а не прошлый режим и не последняя фраза агента:

- комплектация объекта, требования, нормы, «что нужно», «подберите оборудование для
  зала» — консультант, и он доводит задачу до итогового списка;
- конкретный товар, цена, наличие, артикул, каталог, покупка, заказ — продавец;
- из продавца назад к консультанту — по такому же новому вопросу («а что ещё нужно
  для полноценного зала?»);
- сомнение между ними при вопросе о выборе, комплектации или требованиях — консультант.

Прежние механизмы, которые этому противоречили, убраны: передача продавцу по фразе
консультанта «передам специалисту» с подбором в том же ходе и счётчик ответов
консультанта, после которого продавец получал ход сам. Оба переводили человека в
продажу без его нового намерения — ровно то, что заказчик запретил (разделы 14 и 17).

**Сначала правила.** Приветствие, цена, «покажите шведские стенки», «составьте полный
список» разбираются регулярками за микросекунды и не стоят ничего.

**Модель — только на неоднозначном.** Короткий промпт `prompts/orchestrator.md` без
инструментов, ответ строго JSON с намерением и причиной. Контекст — последние реплики и
состояние разговора: «покажи ещё» без него не понять. Сломанный JSON или отказ провайдера
ход не роняют: отвечает прежний агент, а в начале разговора — консультант.

Решение оседает в профиле разговора (`core/profile.py`): прежний агент и намерение
переживают перезапуск, от стадии и возражения зависит гейт карточек.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.client import LLMError
from core import intent

log = logging.getLogger(__name__)

CONSULT = "consult"
SELL = "sell"
GUARD = "guard"

BRANCHES = {CONSULT, SELL, GUARD}
STAGES = {"diagnosis", "presentation", "objection", "closing"}
OBJECTIONS = {"price", "norm", "trust", "logistics", "docs", "none"}
AGENT_NAMES = {CONSULT: "CONSULTANT", SELL: "SALES", GUARD: "GUARD"}

# --- Намерения (ORCHESTRATOR.md, раздел 13) ------------------------------------------

GENERAL_CONSULTATION = "GENERAL_CONSULTATION"
EQUIPMENT_SELECTION = "EQUIPMENT_SELECTION"
ROOM_CONFIGURATION = "ROOM_CONFIGURATION"
FULL_EQUIPMENT_SET = "FULL_EQUIPMENT_SET"
RECOMMENDATION = "RECOMMENDATION"
REQUIREMENTS = "REQUIREMENTS"
REGULATIONS = "REGULATIONS"
SAFETY = "SAFETY"
COMPARISON_CATEGORIES = "COMPARISON_CATEGORIES"
QUANTITY_RECOMMENDATION = "QUANTITY_RECOMMENDATION"
OBJECT_CONFIGURATION = "OBJECT_CONFIGURATION"

PRODUCT_SEARCH = "PRODUCT_SEARCH"
PRODUCT_SELECTION = "PRODUCT_SELECTION"
PRODUCT_DETAILS = "PRODUCT_DETAILS"
PRICE_REQUEST = "PRICE_REQUEST"
AVAILABILITY_REQUEST = "AVAILABILITY_REQUEST"
ARTICLE_REQUEST = "ARTICLE_REQUEST"
PURCHASE_INTENT = "PURCHASE_INTENT"
ORDER_INTENT = "ORDER_INTENT"
PREORDER_INTENT = "PREORDER_INTENT"
CATALOG_REQUEST = "CATALOG_REQUEST"

GREETING = "GREETING"
THANKS = "THANKS"
CLARIFICATION = "CLARIFICATION"
OFF_TOPIC = "OFF_TOPIC"
UNSUPPORTED = "UNSUPPORTED"
OTHER = "OTHER"

CONSULTATION_INTENTS = frozenset(
    {
        GENERAL_CONSULTATION,
        EQUIPMENT_SELECTION,
        ROOM_CONFIGURATION,
        FULL_EQUIPMENT_SET,
        RECOMMENDATION,
        REQUIREMENTS,
        REGULATIONS,
        SAFETY,
        COMPARISON_CATEGORIES,
        QUANTITY_RECOMMENDATION,
        OBJECT_CONFIGURATION,
    }
)
SALES_INTENTS = frozenset(
    {
        PRODUCT_SEARCH,
        PRODUCT_SELECTION,
        PRODUCT_DETAILS,
        PRICE_REQUEST,
        AVAILABILITY_REQUEST,
        ARTICLE_REQUEST,
        PURCHASE_INTENT,
        ORDER_INTENT,
        PREORDER_INTENT,
        CATALOG_REQUEST,
    }
)
# Реплики, у которых своей темы нет: отвечает тот, кто вёл разговор.
CONTINUATION_INTENTS = frozenset({GREETING, THANKS, CLARIFICATION, OTHER})
INTENTS = CONSULTATION_INTENTS | SALES_INTENTS | CONTINUATION_INTENTS | {OFF_TOPIC, UNSUPPORTED}

# Что агенту стоит знать о текущем запросе. Уходит в его системный промпт одной строкой:
# консультанту — что задачу надо довести до итогового списка, продавцу — что консультация
# уже была и расспрашивать заново не нужно.
INTENT_TITLES = {
    GENERAL_CONSULTATION: "общий вопрос",
    EQUIPMENT_SELECTION: "подбор оборудования под задачу — это консультация, не выдача товаров",
    ROOM_CONFIGURATION: "комплектация помещения — доведи до предварительной комплектации",
    FULL_EQUIPMENT_SET: "полная комплектация объекта — доведи до итогового списка",
    RECOMMENDATION: "рекомендация",
    REQUIREMENTS: "требования к оборудованию",
    REGULATIONS: "нормативные документы",
    SAFETY: "безопасность",
    COMPARISON_CATEGORIES: "сравнение категорий оборудования",
    QUANTITY_RECOMMENDATION: "сколько оборудования нужно",
    OBJECT_CONFIGURATION: "комплектация объекта — доведи до итогового списка",
    PRODUCT_SEARCH: "показать конкретные товары из каталога",
    PRODUCT_SELECTION: "подобрать конкретную товарную позицию",
    PRODUCT_DETAILS: "вопрос о конкретном товаре",
    PRICE_REQUEST: "цена",
    AVAILABILITY_REQUEST: "наличие и поставка",
    ARTICLE_REQUEST: "конкретная модель или артикул",
    PURCHASE_INTENT: "хочет купить",
    ORDER_INTENT: "заказ",
    PREORDER_INTENT: "предзаказ",
    CATALOG_REQUEST: "товары из каталога",
}

# Сколько последних реплик показываем модели маршрутизации. Ей нужен контекст («ладно,
# показывайте» — согласие на что?), но не весь разговор: это её цена.
HISTORY_LIMIT = 6
MAX_TOKENS = 250

# Попытка сменить роль или вытащить инструкцию. Ловится правилом, а не моделью:
# просить модель решить, атакуют ли её, — сомнительная затея.
_INJECTION = re.compile(
    r"систем\w+\s+промпт|system\s+prompt|покажи\s+(?:свои\s+)?(?:инструкц|промпт|правил)|"
    r"игнорируй\s+(?:все\s+)?предыдущ|забудь\s+(?:все\s+)?(?:предыдущ|инструкц|указан)|"
    r"представь,?\s+что\s+ты|притворись|веди\s+себя\s+как|ты\s+теперь\b|"
    r"выведи\s+(?:свой|своё|весь)\s+(?:промпт|текст\s+инструкц)|jailbreak|DAN\b",
    re.IGNORECASE,
)

# --- Признаки продажи: конкретный товар, цена, наличие, покупка ----------------------
#
# Эти признаки сильнее консультации: «составьте список с ценами» — уже вопрос продавцу.
# Порядок — приоритеты раздела 14: заказ, цена, наличие, артикул, каталог.
_SALES_SIGNALS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            r"\bпредзаказ\w*|\bзаказ(?:ать|ывать|ываю|ываем|ем|ал)\b|\bоформ\w*\s+(?:заказ|заявк|покупк)|"
            r"\bв\s+корзин\w*|\bсч[её]т\s+на\b|\bвыстав\w*\s+сч[её]т",
            re.IGNORECASE,
        ),
        ORDER_INTENT,
        "хочет заказать или оформить",
    ),
    (
        re.compile(
            r"сколько\s+сто(?:ит|ят|ить)|\bцен(?:а|ы|у|е|ой|ам|ами|ах)?\b|\bстоимост\w*|\bпоч[её]м\b|\bпрайс\w*",
            re.IGNORECASE,
        ),
        PRICE_REQUEST,
        "спрашивает цену",
    ),
    (
        re.compile(
            r"в\s+наличии|\bналичи[еяю]\b|\bна\s+склад\w*|\bостат(?:ок|ки|ков)\b|\bесть\s+ли\s+у\s+вас|"
            r"\bсрок\w*\s+(?:поставк|доставк)\w*|\bдостав(?:ка|ку|ите|ят|ляете)\b",
            re.IGNORECASE,
        ),
        AVAILABILITY_REQUEST,
        "спрашивает наличие или поставку",
    ),
    (
        re.compile(r"\bартикул\w*|\bкод\w*\s*1\s*[сc]\b|\bмодел(?:ь|и|ей|ью)\b", re.IGNORECASE),
        ARTICLE_REQUEST,
        "называет модель или артикул",
    ),
    (
        re.compile(
            r"\bкаталог\w*|\bассортимент\w*|\bу\s+вас\s+(?:есть|бывают|имеются|продаются)|\bесть\s+у\s+вас|"
            r"\bконкретно\b|\bконкретн\w+\s+(?:товар|позици|модел|вариант|производител)\w*|"
            r"\bваш(?:и|их|ими|е|его)?\s+(?:товар|позици|модел|вариант)\w*",
            re.IGNORECASE,
        ),
        CATALOG_REQUEST,
        "просит конкретные товары из каталога",
    ),
)

# Покупка — продажа, только когда покупают что-то определённое: «хочу купить шведскую
# стенку». «Что нужно купить для спортзала» — вопрос комплектации.
_PURCHASE = re.compile(
    r"\bкуп(?:ить|лю|им|ят)\b|\bприобрести\b|\bприобрет(?:аем|аю|ём|ем)\b|\bвозьм(?:у|ём|ем)\b|\bбер(?:у|ём|ем)\b",
    re.IGNORECASE,
)
# Ссылка на показанное: «этот мяч», «первый», «из этих».
_SHOWN_REF = re.compile(
    r"\bэт(?:от|а|у|и|ой|ого|им|ими|их)\s+(?!задач|помещени|зал|кабинет|групп|вопрос)\w{3,}|"
    r"\b(?:перв|втор|трет)(?:ый|ой|ий|ая|ья|ую|ью|ое|ье)\b(?!\s+очеред)|\bиз\s+(?:этих|них|показанн\w+)|"
    r"\bпоказанн\w+",
    re.IGNORECASE,
)
_SHOW = re.compile(
    r"покаж\w+|показать|\bнайди\w*|\bнайти\b|\bищу\b|\bчто\s+есть\b|\bкакие\s+есть\b|\bварианты\b",
    re.IGNORECASE,
)
_MORE = re.compile(
    r"\bещ[её]\s+(?:вариант|позици|товар|что-нибудь|такие)\w*|\bдруги[ехм]?\s+(?:вариант|позици|модел|товар)\w*|"
    r"\bподешевле\b|\bдешевле\b|\bпокажи\w*\s+ещ[её]",
    re.IGNORECASE,
)
_SELECT = re.compile(r"подбер\w+|подобра\w+|\bподбор\w*", re.IGNORECASE)
# Возражение по показанному: его стадию и снятие размечает модель, правилом не решить.
_OBJECTION = re.compile(
    r"\bдорог\w*|\bскидк\w*|\bторг\w*|\bне\s+укладыва\w*|\bне\s+влеза\w*|\bпереплат\w*|\bу\s+других\b",
    re.IGNORECASE,
)

# --- Признаки консультации -------------------------------------------------------------
_CONSULT_SIGNALS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            r"полн\w+\s+(?:список|перечень|комплект\w*|оснащени\w*|комплектаци\w*)|\bдля\s+полноценн\w+|"
            r"\bвесь\s+(?:список|комплект|перечень)|\bсписок\s+всего\b|\bкомплектаци\w*|\bукомплект\w*|"
            r"(?:дай|дайте|пришли\w*|напиши\w*|состав\w*|сформиру\w*|подготов\w*|приведи\w*)\s+(?:\w+\s+){0,2}"
            r"(?:список|перечень)|\bсписок\s+оборудовани\w*|"
            r"\bчто\s+(?:ещ[её]\s+)?(?:нужн|необходим|надо|потребу|должн|входит)\w*|"
            r"\bкак(?:ой|ая|ое|ие|их)\s+(?:ещ[её]\s+)?(?:\w+\s+){0,3}(?:нужн|необходим|требу|потребу|долж)\w*|"
            r"\bвернёмся\s+к|\bвернемся\s+к",
            re.IGNORECASE,
        ),
        FULL_EQUIPMENT_SET,
        "комплексный запрос комплектации",
    ),
    (
        re.compile(
            r"\bкак\s+(?:\w+\s+)?(?:оборудовать|оснастить|организовать|расставить|разместить|укомплектовать)|"
            r"\bс\s+чего\s+начать|\bоборудова(?:ть|ли)\b|\bоснасти\w*|\bоснащени\w*|\bпереоснащ\w*|"
            r"\bоборудовани\w*\s+(?:для|в)\b|\bкакое\s+оборудование",
            re.IGNORECASE,
        ),
        ROOM_CONFIGURATION,
        "комплектация помещения",
    ),
    (re.compile(r"\bтребовани\w*|\bпредъявля\w*|\bтребуется\s+ли", re.IGNORECASE), REQUIREMENTS, "вопрос о требованиях"),
    (re.compile(r"\bбезопасн\w*|\bтравм\w*", re.IGNORECASE), SAFETY, "вопрос о безопасности"),
    (
        re.compile(
            r"сколько\s+(?:\w+\s+)?(?:нужно|надо|требуется|необходимо|рекомендуется|должно)|"
            r"\bкакое\s+количество|\bв\s+каком\s+количестве",
            re.IGNORECASE,
        ),
        QUANTITY_RECOMMENDATION,
        "вопрос о количестве",
    ),
    (
        re.compile(r"\bчем\s+отлича\w*|\bв\s+ч[её]м\s+разниц\w*|\bразниц\w*\s+между", re.IGNORECASE),
        COMPARISON_CATEGORIES,
        "сравнение вариантов",
    ),
    (
        re.compile(
            r"\bрекоменд\w*|\bпосовету\w*|\bсоветуете\b|\bлучше\b|\bнужн\w*\s+ли\b|\bзачем\b|"
            r"\bобязательн\w*\s+ли\b|\bстоит\s+ли\b|\bчто\s+выбрать|\bкак\s+выбрать",
            re.IGNORECASE,
        ),
        RECOMMENDATION,
        "консультационный вопрос о выборе",
    ),
)

# Согласие одним словом — ответ на вопрос бота.
_AGREEMENT = re.compile(r"^\s*(?:да|ага|угу|ок|окей|хорошо|ладно|давайте)\b[\s!.,)]*$", re.IGNORECASE)
_THANKS = re.compile(r"^\s*(?:спасибо\w*|благодар\w+)", re.IGNORECASE)
_ABOUT_BOT = re.compile(r"\b(?:ты|вы)\s+(?:кто|бот)\b", re.IGNORECASE)
# Вопрос бота, согласие на который — просьба показать товары: «Подобрать варианты из каталога?»
_OFFERS_CATALOG = re.compile(
    r"(?:подобрать|показать|посмотреть)\s+(?:\w+\s+){0,3}(?:вариант|товар|позици)\w*|из\s+каталога",
    re.IGNORECASE,
)


@dataclass
class Decision:
    """Что решено по текущей реплике."""

    branch: str = CONSULT
    intent: str = OTHER
    # Почему — одной фразой. Идёт в журнал: по нему видно, отчего ответил этот агент.
    reason: str = ""
    stage: str = "diagnosis"
    objection: str = "none"
    objection_handled: bool = False
    ready_to_see: bool = False
    facts: dict[str, str] = field(default_factory=dict)
    # Назван номер пункта перечня. Такой запрос точен сам по себе: показывать по
    # нему можно, не выясняя учреждение и зону.
    precise: bool = False
    # Чем принято решение: правилом, моделью или запасным путём.
    source: str = "правило"
    # Кто отвечал ходом раньше — для журнала.
    previous: str | None = None

    @property
    def sells(self) -> bool:
        return self.branch == SELL


def by_rules(
    text: str,
    profile,  # noqa: ANN001 — core.profile.DialogProfile
    last_reply: str | None = None,
) -> Decision | None:
    """Решение, которое видно без модели. `None` — значит, нужно спрашивать.

    `last_reply` — предыдущий ответ бота: «да» на его вопрос — ответ, а не вежливость.
    """
    text = (text or "").strip()
    last = _last_agent(profile)
    if not text:
        return _continue(profile, CLARIFICATION, "пустая реплика")

    if _INJECTION.search(text):
        return Decision(branch=GUARD, intent=UNSUPPORTED, reason="попытка сменить роль или вытащить инструкцию")

    kind = intent.classify(text)
    if kind == intent.GREETING:
        return _continue(profile, GREETING, "приветствие")
    if kind == intent.SMALL_TALK:
        if _AGREEMENT.match(text) and (last_reply or "").rstrip().endswith("?"):
            if _OFFERS_CATALOG.search(_last_sentence(last_reply)):
                return _sales(CATALOG_REQUEST, "согласился посмотреть товары из каталога")
            # «Да» на «вам для сада или для школы?» — ответ, а не переход. Решает модель в контексте.
            return None
        if _THANKS.match(text):
            return _continue(profile, THANKS, "благодарность")
        if _ABOUT_BOT.search(text):
            return _consult(GENERAL_CONSULTATION, "вопрос о боте и компании")
        return _continue(profile, CLARIFICATION, "короткая реплика продолжает тему")

    goods = intent.names_goods(text)
    shown = bool(profile.offered) or last == SELL

    for pattern, name, reason in _SALES_SIGNALS:
        if pattern.search(text):
            return _sales(name, reason)
    if _PURCHASE.search(text) and (goods or shown):
        return _sales(PURCHASE_INTENT, "хочет купить определённый товар")
    if shown and _SHOWN_REF.search(text):
        return _sales(PRODUCT_DETAILS, "вопрос о показанном товаре", ready=False)
    if goods and _SHOW.search(text):
        return _sales(PRODUCT_SEARCH, "просит показать названный товар")
    if shown and _OBJECTION.search(text):
        # Стадию, возражение и его снятие размечает модель: правилом «дорого» от «ладно,
        # убедили» не отличить.
        return None

    for pattern, name, reason in _CONSULT_SIGNALS:
        if pattern.search(text):
            return _consult(name, reason)

    if kind == intent.NORM_QUESTION:
        return _consult(REGULATIONS, "вопрос о нормативном документе или пункте")
    if kind == intent.NORM_CODE:
        # Назван пункт перечня — человек знает, какую позицию ищет. Показываем.
        return _sales(CATALOG_REQUEST, "назван пункт перечня", precise=True)
    if _MORE.search(text) and shown:
        return _sales(PRODUCT_SEARCH, "просит ещё варианты")
    if _SELECT.search(text):
        if goods:
            return _sales(PRODUCT_SELECTION, "просит подобрать названный товар")
        if intent.describes_task(text):
            # «Подберите оборудование для спортзала детского сада» — не поиск товара, а задача
            # комплектации (ORCHESTRATOR.md, раздел 8 ТЗ на перестройку).
            return _consult(EQUIPMENT_SELECTION, "подбор оборудования под задачу")
        if shown or re.search(r"\bвариант|\bтовар|\bпозици", text, re.IGNORECASE):
            return _sales(CATALOG_REQUEST, "просит подобрать товары")
        return _consult(EQUIPMENT_SELECTION, "подбор оборудования под задачу")
    if _SHOW.search(text):
        return _sales(CATALOG_REQUEST, "просит показать, что есть в каталоге")
    if goods and (intent.describes_task(text) or intent.is_short(text)):
        # «Нужен мяч для группы», «массажные мячи» — названа товарная позиция.
        return _sales(PRODUCT_SELECTION, "назван конкретный товар")
    if kind == intent.TASK:
        if shown:
            # Уточнение посреди подбора («у нас младшая группа») — ответ продавцу или новая
            # задача консультанту. Видно только в контексте: решает модель.
            return None
        return _consult(OBJECT_CONFIGURATION, "описание задачи: учреждение, помещение, возраст")

    # Осталось неоднозначное: возражения, сомнения, согласия, ответы на вопросы.
    return None


def _consult(name: str, reason: str) -> Decision:
    return Decision(branch=CONSULT, intent=name, reason=reason, stage="diagnosis")


def _sales(name: str, reason: str, *, ready: bool = True, precise: bool = False) -> Decision:
    return Decision(
        branch=SELL, intent=name, reason=reason, stage="presentation", ready_to_see=ready, precise=precise
    )


def _continue(profile, name: str, reason: str) -> Decision:  # noqa: ANN001 — core.profile.DialogProfile
    """Реплика без своей темы: отвечает прежний агент, в начале разговора — консультант."""
    last = _last_agent(profile)
    return Decision(branch=last or CONSULT, intent=name, reason=reason, stage=profile.stage if last else "diagnosis")


def _last_agent(profile) -> str | None:  # noqa: ANN001 — core.profile.DialogProfile
    last = getattr(profile, "last_agent", None)
    return last if last in (CONSULT, SELL) else None


def _last_sentence(text: str | None) -> str:
    parts = re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip())
    return parts[-1] if parts else ""


class Orchestrator:
    """Оркестратор: правила плюс дешёвый вызов модели на остатке."""

    def __init__(self, llm, prompt: str | None = None) -> None:  # noqa: ANN001 — LLMRouter
        self.llm = llm
        self.prompt = prompt if prompt is not None else _load_prompt()

    def decide(self, session, text: str) -> Decision:  # noqa: ANN001 — core.dialog.Session
        profile = session.profile
        previous = _last_agent(profile)
        last_reply = next(
            (item["content"] for item in reversed(session.history) if item.get("role") == "assistant"),
            None,
        )
        decision = by_rules(text, profile, last_reply)
        if decision is None:
            decision = self._ask(session, text) or _fallback(profile)
        decision.previous = previous
        self._apply(session, decision)
        _log_route(session, text, decision)
        return decision

    # --- Обращение к модели ---------------------------------------------------

    def _ask(self, session, text: str) -> Decision | None:  # noqa: ANN001
        if self.llm is None or not self.llm.available:
            return None
        system = f"{self.prompt}\n\n{_state(session.profile)}" if self.prompt else _state(session.profile)
        messages = [{"role": "system", "content": system}, *_history(session, text)]
        for client in self.llm.ready():
            try:
                message = client.complete(messages, temperature=0.0, max_tokens=MAX_TOKENS)
            except LLMError as exc:
                self.llm.mark_down(client, exc)
                continue
            _account(session, client, message)
            parsed = parse(message.get("content") or "", _last_agent(session.profile))
            if parsed is not None:
                return parsed
            log.warning("Оркестратор не разобрал ответ модели — отвечает прежний агент.")
            return None
        return None

    # --- Запись решения в профиль ---------------------------------------------

    def _apply(self, session, decision: Decision) -> None:  # noqa: ANN001
        profile = session.profile
        for key, value in decision.facts.items():
            _remember_fact(profile, key, value)

        if decision.branch in (CONSULT, SELL):
            profile.last_agent = decision.branch
        profile.intent = decision.intent

        profile.stage = decision.stage
        if decision.objection != "none":
            if decision.objection != profile.objection:
                # Возражение новое — считаем его неснятым, что бы ни сказала
                # модель: снять то, что человек только что высказал, нельзя.
                profile.objection = decision.objection
                profile.objection_handled = False
            elif decision.objection_handled:
                profile.objection_handled = True
        elif decision.objection_handled:
            profile.objection_handled = True

        pending = profile.objection != "none" and not profile.objection_handled
        if decision.ready_to_see:
            profile.ready_to_see = True
            if decision.objection == "none":
                # «Ладно, показывайте» — это и есть снятое возражение. Без этой
                # строки прошлое «дорого» держало карточки закрытыми до конца
                # разговора, даже когда человек прямо просил их показать.
                profile.objection_handled = True
        elif pending:
            # Пока возражение висит, карточки не показываем, даже если человек
            # хотел их посмотреть ходом раньше.
            profile.ready_to_see = False


def _fallback(profile) -> Decision:  # noqa: ANN001 — core.profile.DialogProfile
    """Модель не спросить: продолжает прежний агент, а в начале разговора — консультант.

    Раздел 24: при сомнении между консультантом и продавцом — консультант. Продавец
    получает ход по умолчанию, только если он и вёл разговор.
    """
    decision = _continue(profile, CLARIFICATION, "неоднозначная реплика, модель маршрутизации недоступна")
    decision.source = "запасной"
    return decision


def parse(raw: str, last_agent: str | None = None) -> Decision | None:
    """Решение из ответа модели. Мусор превращается в `None`, а не в исключение.

    Агента задаёт намерение (раздел 23): товарное — продавец, консультационное —
    консультант. Поле агента нужно для реплик без своей темы и для охраны.
    """
    data = _json_object(raw)
    if data is None:
        return None

    name = str(data.get("intent") or "").strip().upper()
    name = name if name in INTENTS else OTHER
    agent = _AGENTS.get(str(data.get("agent") or data.get("branch") or "").strip().lower())
    if name in SALES_INTENTS:
        branch = SELL
    elif name in CONSULTATION_INTENTS:
        branch = CONSULT
    elif agent is not None:
        branch = agent
    elif name in (OFF_TOPIC, UNSUPPORTED):
        branch = GUARD
    else:
        branch = last_agent if last_agent in (CONSULT, SELL) else CONSULT

    stage = str(data.get("stage") or "").strip().lower()
    objection = str(data.get("objection") or "none").strip().lower()
    facts = data.get("facts")
    reason = data.get("reason")

    return Decision(
        branch=branch,
        intent=name,
        reason=reason.strip()[:200] if isinstance(reason, str) else "",
        stage=stage if stage in STAGES else "diagnosis",
        objection=objection if objection in OBJECTIONS else "none",
        objection_handled=bool(data.get("objection_handled")),
        ready_to_see=bool(data.get("ready_to_see")),
        facts=_clean_facts(facts if isinstance(facts, dict) else {}),
        source="модель",
    )


_AGENTS = {
    "consultant": CONSULT,
    "consult": CONSULT,
    "sales": SELL,
    "sell": SELL,
    "salesman": SELL,
    "guard": GUARD,
}


# --- Мелочи -------------------------------------------------------------------

# Поля профиля, которые модели маршрутизации позволено заполнять. Персональных данных
# среди них нет и быть не должно: профиль хранится на диске и целиком уходит в
# промпт финального агента.
_ALLOWED_FACTS = ("institution", "room", "age", "budget", "deadline")
_MAX_FACT_LENGTH = 60


def _clean_facts(raw: dict) -> dict[str, str]:
    facts = {}
    for key in _ALLOWED_FACTS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            facts[key] = value.strip()[:_MAX_FACT_LENGTH]
    return facts


def _remember_fact(profile, key: str, value: str) -> None:  # noqa: ANN001
    """Дописывает факт, не затирая уже разобранное регулярками.

    Тип учреждения фиксируется один раз — в разговоре он не меняется. Остальное
    уточняется: человек начал со «спортзала», потом перешёл на «музыкальный зал».
    """
    if key == "institution" and profile.institution:
        return
    if getattr(profile, key, None) != value:
        setattr(profile, key, value)


def _state(profile) -> str:  # noqa: ANN001 — core.profile.DialogProfile
    """Состояние разговора для модели маршрутизации (разделы 12 и 19)."""
    last = _last_agent(profile)
    known = ", ".join(
        f"{label}: {value}"
        for label, value in (("учреждение", profile.institution), ("зона", profile.room), ("возраст", profile.age))
        if value
    )
    return "\n".join(
        [
            "## Состояние разговора",
            f"- Прежний агент: {AGENT_NAMES[last] if last else 'нет, разговор только начался'}",
            f"- Прежнее намерение: {getattr(profile, 'intent', None) or 'нет'}",
            f"- Показано товаров: {len(profile.offered)}",
            f"- Известно о задаче: {known or 'ничего'}",
        ]
    )


def _history(session, text: str) -> list[dict[str, str]]:  # noqa: ANN001
    """Переписка для модели маршрутизации — обязательно маскированная.

    В сессию реплики попадают уже с метками вместо телефонов и почты
    (`Session.remember`), поэтому история берётся как есть. А вот текущую реплику
    приходится маскировать здесь: ядро зовёт оркестратор после записи в
    историю, но тесты и другие вызовы могут этого не делать, и немаскированный
    телефон ушёл бы наружу. Проверено — уходил.
    """
    history = [
        {"role": item["role"], "content": item["content"]}
        for item in session.history[-HISTORY_LIMIT:]
    ]
    masked = session.masker.mask(text or "")
    if not history or history[-1]["role"] != "user" or history[-1]["content"] != masked:
        history.append({"role": "user", "content": masked})
    return history


def _log_route(session, text: str, decision: Decision) -> None:  # noqa: ANN001
    """Строка журнала на каждый ход (раздел 22). Реплика — только маскированная."""
    masker = getattr(session, "masker", None)
    message = masker.mask(text or "")[:120] if masker is not None else "—"
    log.info(
        "Оркестратор: «%s» → намерение %s, прежний агент %s, отвечает %s: %s (%s)",
        message,
        decision.intent,
        AGENT_NAMES.get(decision.previous or "", "NONE"),
        AGENT_NAMES.get(decision.branch, decision.branch),
        decision.reason or "—",
        decision.source,
    )


def _json_object(raw: str) -> dict | None:
    """Первый объект JSON в тексте.

    Модель то и дело оборачивает ответ в ```json … ``` или предваряет фразой,
    хотя промпт этого не просит. Вырезаем от первой фигурной скобки до последней.
    """
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _account(session, client, message: dict) -> None:  # noqa: ANN001
    """Расход оркестратора идёт в тот же счётчик хода, что и расход агента."""
    from agent.agent import account_usage

    account_usage(session, client, message)


def _load_prompt() -> str:
    path = Path(__file__).parent / "prompts" / "orchestrator.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


__all__ = [
    "CONSULT",
    "CONSULTATION_INTENTS",
    "GUARD",
    "INTENT_TITLES",
    "SALES_INTENTS",
    "SELL",
    "Decision",
    "Orchestrator",
    "by_rules",
    "parse",
]
