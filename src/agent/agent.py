"""Два агента в одном диалоге: консультант и продавец.

Роль на каждый ход выбирает оркестратор (`agent/routing.py`), и промпт
собирается под неё. Раньше все четыре файла промптов склеивались в одну простыню
на четыре с лишним тысячи токенов и уходили одному вызову — агенту приходилось
быть сразу справочной, продавцом и охраной, и он выбирал самое простое: показать
товар. Отсюда жалоба заказчика «пропал режим диалога».

Ключевое свойство — деградация без обрыва. Если провайдер не ответил, пробуем
следующего; если легли все — диалог продолжается предложением из каталога.
Бот, который молчит из-за недоступности внешнего сервиса, хуже бота без модели.

Персональные данные до модели не доходят: история приходит сюда уже маскированной
(`core/dialog.Session.remember`), а ответ восстанавливается перед показом.

Три вещи агент делает поверх обычного tool-calling.

**Показывает модели профиль разговора.** Короткая выжимка «что уже известно»
подставляется в системный промпт. Без неё бот переспрашивал возраст детей, который
ему назвали ходом раньше, — это видно в журнале диалогов.

**Проверяет цены и нормативные основания в ответе.** Всё, что похоже на сумму или
на ссылку «пункт такой-то приказа такого-то», должно встречаться среди результатов
инструментов. Не совпало — просим переписать, а если не помогло, отвечаем выдачей
каталога. Выдуманная цена дороже молчания, а выдуманное основание — дороже цены:
по нему принимают закупку.

**Не верит обещанию подбора.** «Сейчас подберу» без вызова подбора — ложное
обещание. Модель один раз просят подобрать в этом же ходе; не подобрала — ответ
заменяется подбором Procurement Core по реплике человека или одним вопросом о том,
чего не хватает. Прежняя страховка дописывала к обещанию выдачу по помещению из
профиля — и на «массажные мячи» приходили тележка и ребристая доска (13.09).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from agent.client import ChatClient, LLMError
from agent.providers import LLMRouter
from agent.routing import (
    CONSULT,
    EXPORT_REQUEST,
    GUARD,
    INTENT_TITLES,
    SELL,
    Decision,
    Orchestrator,
)
from agent.tools import TOOL_SCHEMAS, ToolBox
from agent.verify import (
    claims_handoff,
    client_ages,
    describe_refs,
    foreign_script,
    invented_norm_refs,
    invented_prices,
    listed_codes,
    prices_in,
    promises_goods,
    section_ages,
    title_matches,
    without_meta,
    without_promises,
    without_service_marks,
    without_tool_calls,
    without_unverified,
)
from core import exports, intent, selection
from core.profile import whole_object
from core.ui import Button, Keyboard, Message, ProductCard, Response, price_text, stock_text

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_TOOL_ROUNDS = 4
# Сколько раз за ход обращаемся к модели. Ночью 15.09 ход доходил до десяти вызовов: 79 секунд
# ожидания и 6.25 ₽ за диалог против 1.35 ₽ прежних. После лимита отвечаем тем, что уже собрано
# и проверено: молчание превращалось в «консультант временно недоступен» на самых важных ходах.
TURN_CALLS = 4
HISTORY_LIMIT = 12
# Сколько карточек прикладываем к ответу модели. Заказчик отдельно попросил
# не больше трёх: пять карточек подряд читаются как выгрузка, а не как подбор.
CARDS_SHOWN = 3

# Чего мы ждём от переписанного ответа. Отдельной строкой, чтобы просьба звучала
# одинаково и для выдуманной цены, и для выдуманного пункта приказа.
_REWRITE_HINT = (
    " Названия, цены и пункты перечней бери только из вызовов инструментов. "
    "Номер пункта всегда называй вместе с приказом, а формулировку — только ту, "
    "что вернул инструмент. Перепиши ответ: оставь лишь подтверждённые позиции, "
    "а чего в каталоге нет — так и скажи."
)

# Короче этого остаток ответа после вырезания обещания — вежливость, а не ответ.
MIN_KEPT = 40
# Сколько знаков отвергнутого ответа пишем в журнал хода — хватает, чтобы увидеть, что выдумано.
DISCARDED_KEPT = 1500
# Длинные ответы ассистента в истории для модели: полная комплектация уже лежит в профиле.
HISTORY_CHARS = 1500
_UNVERIFIED_NOTE = (
    "Часть пунктов по тексту приказа не подтвердилась — их я не привожу. Назовите номер раздела "
    "перечня, и я сверю состав по нему."
)
# Срок до счёта, график поставок и дату отгрузки бот не знает: этих данных нет ни в каталоге,
# ни в перечнях. Ночью 16.09 такой вопрос остался без ответа в семи диалогах.
_DEADLINE_ANSWER = (
    "Срок от оформления до счёта и график поставок по этапам называет менеджер: "
    "у бота таких данных нет — ни в каталоге, ни в перечнях приказов."
)
_DEADLINE_WITH_CART = (
    "\n\nНажмите «Оформить» — соберу спецификацию и предзаказ, а после имени и телефона "
    "менеджер увидит заявку со всеми позициями и основаниями и назовёт срок."
)
_DEADLINE_WITHOUT_CART = (
    "\n\nНажмите «Связаться с менеджером» и оставьте имя и телефон — он ответит по срокам, "
    "этапам поставки и оплате. Могу пока подобрать позиции, чтобы в заявке был состав."
)


def _about_goods(text: str) -> bool:
    """Просят ли в той же реплике товары: тогда срок — не единственное, о чём спросили."""
    return bool(intent.asks_to_show(text) or intent.names_goods(text) or intent.asks_for_goods(text))


# Вопрос консультанта, когда подтвердить не удалось ничего. Выдачу каталога вместо консультации
# не показываем: 14.09 на «дай консультацию… кабинет логопеда» пришли фитбол и мячики.
_CONSULT_RETRY = (
    "Пункты перечня по этому вопросу подтвердить по тексту приказа не получилось, а называть их по "
    "памяти я не буду. Назовите помещение или раздел перечня — например, «1.13.3», — и я соберу "
    "комплектацию по нему."
)
# Один вопрос по неснятому возражению, когда от ответа модели ничего не осталось. Порядок —
# первый шаг работы с возражением из промпта продавца: понять, что именно мешает.
_OBJECTION_QUESTIONS = {
    "price": (
        "Понимаю, бюджет важен. Что для вас главное — уложиться в лимит или понять, из чего "
        "складывается цена? От этого зависит, что предложить."
    ),
    "norm": "Понимаю сомнение. По какому пункту перечня нужно подтверждение? Сверю с текстом приказа.",
    # Стоимость и срок доставки бот не называет: тариф зависит от региона и от того,
    # частное лицо это или учреждение, а сроки заказчик просил не обещать. Отдаём
    # условия ссылкой и передаём разговор менеджеру.
    "logistics": (
        "Сроки и доставку подтверждает менеджер, условия — на странице {delivery_url}. "
        "Что важнее — успеть к дате или взять всё одной поставкой?"
    ),
    "none": "Подскажите, что именно смущает — цена, соответствие перечню или сроки?",
}


def _objection_question(objection: str, delivery_url: str) -> str:
    text = _OBJECTION_QUESTIONS.get(objection, _OBJECTION_QUESTIONS["none"])
    return text.format(delivery_url=delivery_url)

# Просьба к продавцу, который пообещал подбор и не вызвал его.
_INSIST = (
    "Ты пообещал подобрать товары, но не вызвал подбор. Не обещай: вызови "
    "search_products (для пункта перечня — find_by_norm_code) сейчас и назови только "
    "то, что он вернёт, либо задай клиенту один уточняющий вопрос. Не извиняйся и не "
    "упоминай эту просьбу: клиент её не видел. Цену уже показанного товара можно назвать "
    "из переписки."
)

# Код 1С в ответе модели: она обязана его называть, чтобы карточки сошлись с
# текстом, а перед показом человеку код вырезается — он служебный.
_CODE_MENTION = re.compile(
    # «Артикул» — только целым словом: 14.09 «Артикуляционная моторика» превратилась в «, мимика».
    # Слово после «код 1С» вырезается, только если это и правда код: с цифрой внутри.
    # 16.09 «или код 1С товара — отвечу» превратилось в «или— отвечу».
    r"[ \t]*[(\[]?[ \t]*(?:\*\*)?(?:код\s*1\s*[СCc]|артикул(?![а-яё]))[\s*:]*(?=[A-Za-z0-9А-ЯЁа-яё\-]*\d)[A-Za-z0-9А-ЯЁа-яё\-]+[ \t]*[)\]]?",
    re.IGNORECASE,
)
# Запятая от вырезанного кода: «Мат детский, артикул Д-214, 8 164 ₽» → «Мат детский,, 8 164 ₽».
_DOUBLE_COMMA = re.compile(r",(?:\s*,)+")
# Пункт списка, от которого после вырезания кода ничего не осталось: «- **Код 1С:** 42639» → «-».
_EMPTY_BULLET = re.compile(r"^[ \t]*[-•][ \t]*[*:]*[ \t]*(?:\n|$)", re.MULTILINE)

# Из чего собирается промпт роли. Границы идут первыми, чтобы не тонуть в
# середине длинного текста, дальше общая часть, дальше сама роль.
ROLE_PARTS: dict[str, tuple[str, ...]] = {
    CONSULT: ("guard", "common", "consultant"),
    SELL: ("guard", "common", "salesman"),
    GUARD: ("guard",),
}

# Какие инструменты доступны роли. Консультанту каталог не нужен: он объясняет
# документы, а не подбирает. Охране не нужно ничего.
ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    # Справочник пунктов приказа консультанту нужен не меньше, чем справка по
    # самому документу: «что такое пункт 2.1.14» — это его вопрос, и отвечать
    # на него по памяти он не должен.
    CONSULT: ("explain_norm", "find_norm_item", "get_cart", "handoff_to_manager"),
    SELL: (),  # пустой кортеж — значит все
    GUARD: ("__none__",),
}


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def tools_for(branch: str) -> list[dict] | None:
    allowed = ROLE_TOOLS.get(branch, ())
    if not allowed:
        return TOOL_SCHEMAS
    chosen = [
        schema
        for schema in TOOL_SCHEMAS
        if schema.get("function", {}).get("name") in allowed
    ]
    return chosen or None


def may_show_cards(
    profile,  # noqa: ANN001
    decision: Decision,
    named_positions: bool = False,
) -> tuple[bool, str]:
    """Можно ли приложить к ответу карточки товаров — и почему.

    Заказчик сформулировал правило так: «только после отработки возражений
    выводить карточку с кнопкой подробнее или в корзину, бот должен быть живым,
    а не просто связывать карточки и корзину». Причина решения возвращается
    наружу и пишется в журнал — иначе на прогоне не понять, почему бот промолчал.

    `named_positions` — модель уже назвала конкретные позиции из каталога.
    Тогда требование «сначала выясни учреждение и зону» снимается: 02.09 на
    реплику «чем оснастить спортзал в саду» бот перечислил три позиции с ценами
    и пунктами приказа, а карточек не дал — и человеку было нечем положить их в
    корзину. Товар с ценой в тексте и без кнопки хуже, чем товар с кнопкой.
    Гейт по возражению при этом остаётся: он и есть суть правила заказчика.
    """
    if decision.branch != SELL:
        return False, "ветка консультирования"
    if profile.objection != "none" and not profile.objection_handled:
        return False, f"возражение не снято ({profile.objection})"
    if not profile.ready_to_see and not named_positions:
        return False, "клиент не просил показывать"
    if not profile.task_known and not decision.precise and not named_positions:
        return False, "не выяснены учреждение и зона"
    if named_positions and not (profile.task_known or profile.ready_to_see):
        return True, "позиции уже названы в ответе"
    return True, "задача ясна, клиент готов смотреть"


class SalesAgent:
    def __init__(self, engine, router: LLMRouter, routing: Orchestrator | None = None) -> None:  # noqa: ANN001
        self.engine = engine
        self.router = router
        self.routing = routing if routing is not None else Orchestrator(router)
        self.prompts = {
            branch: "\n\n".join(
                part for part in (load_prompt(name) for name in names) if part
            )
            for branch, names in ROLE_PARTS.items()
        }

    @property
    def available(self) -> bool:
        return self.router.available

    def reply(self, session, text: str) -> list[Response]:  # noqa: ANN001
        if not self.available:
            return self.engine.offer(session, text)

        decision = self.routing.decide(session, text)
        show_cards, reason = may_show_cards(session.profile, decision)
        session.route = {
            "role": decision.branch,
            "intent": decision.intent,
            "previous_agent": decision.previous,
            "reason": decision.reason,
            "stage": decision.stage,
            "objection": session.profile.objection,
            "objection_handled": session.profile.objection_handled,
            "routed_by": decision.source,
            "cards": {"allowed": show_cards, "reason": reason},
        }

        # Файл, следующая страница и список из N позиций — действия, а не разговор: отвечает ядро,
        # без модели. 14.09 на «сохрани в файл» модель ответила «не могу», на «а ещё что есть» —
        # таблицей, а «подбери из наличия 30 позиций» свела к трём карточкам.
        service = self._service_reply(session, text, decision)
        if service is not None:
            return service

        tools = ToolBox(self.engine, session)
        messages = [
            {"role": "system", "content": self._system_prompt(session, decision)},
            *self._history(session),
        ]

        try:
            answer = self._ask(messages, tools, tools_for(decision.branch))
        except LLMError:
            # Провайдеры уже помечены нерабочими и записаны в лог — здесь остаётся
            # только доиграть ход предложением из каталога.
            return self.engine.offer(session, text)

        # Продавец пообещал подбор и не сделал его: просим один раз, в этом же ходе.
        if decision.sells and not tools.selected and promises_goods(answer, show_cards):
            answer = self._insist(messages, tools, answer)
        session.prices |= tools.prices
        session.norm_refs |= tools.norm_refs
        if decision.sells and not tools.selected and promises_goods(answer, show_cards):
            session.route["false_promise"] = True
            return self._instead_of_promise(session, tools, text, decision, answer)
        if decision.branch == CONSULT and promises_goods(answer):
            # Подбора у консультанта нет, а на OpenRouter 14.09 он его обещал: «сейчас
            # проверю, какие позиции есть… одну минуту». Обещание убираем, ведём вопросом.
            session.route["false_promise"] = True
            session.route["fallback"] = "consult_question"
            answer = self._without_selection_promise(session, answer)

        # Гейт пересчитываем, когда ход уже сыгран: до вызова модели неизвестно,
        # назовёт ли она конкретные позиции, а от этого зависит, будет ли человеку
        # чем воспользоваться.
        show_cards, reason = may_show_cards(session.profile, decision, bool(tools.shown_skus))
        session.route["cards"] = {"allowed": show_cards, "reason": reason}
        if tools.norm_lookups:
            session.route["norm_lookups"] = tools.norm_lookups

        # Ночью 16.09 клиенту ушёл голый JSON вызова handoff_to_manager (сц. 50) и «Функция
        # вызывается:» с тремя блоками (сц. 47). Проверка цен такой текст пропускает — она
        # ищет выдуманные числа, а не пересказ работы, которой не было.
        answer = without_tool_calls(
            without_service_marks(
                self._verified(
                    answer, messages, tools, text, session, tools_for(decision.branch), decision.branch
                )
            )
        )
        if decision.branch == CONSULT:
            # В файл — раздел, о котором ответ, а не последний разобранный: ночью 14.09 (сц. 24) текст был
            # про технопарк, а «Скачать Excel» прислал ученические стулья из последнего поиска «раздел 2.14».
            tools.kit = tools.kit_for(answer)
            if tools.kit:
                session.profile.remember_kit(tools.kit)
                answer = _short_kit_answer(answer)
        if not answer:
            session.route["discarded_answer"] = True
            if decision.branch == CONSULT:
                # Консультацию выдачей каталога не заменяем: 14.09 вместо неё пришли фитбол и мячики.
                return self._consult_question(session, tools, decision)
            return self.engine.offer(session, text)

        if show_cards:
            answer = self._with_catalog_positions(tools, answer)
        answer = session.masker.unmask(answer)
        session.remember("assistant", answer)
        session.profile.remember_offered(_unique(tools.shown_skus))
        # Продавцу по фразе консультанта ход не передаётся: переход решает новое намерение
        # человека (ORCHESTRATOR.md, разделы 14 и 17).
        return self._render(session, tools, answer, text, decision, show_cards)

    # --- Сведение текста ответа с карточками ---------------------------------

    def _mentioned_skus(self, tools: ToolBox, answer: str) -> list[str]:
        """Товары, которые модель действительно назвала в ответе.

        Раньше искались только коды 1С. Модель их не пишет — она пишет названия, —
        и совпадений не было ни разу, а на их месте молча подставлялись первые
        позиции из поиска. Так и вышло, что текст обещал интерактивное зеркало и
        песочницу, а карточками приходили парта логопеда и карточки «Овощи».

        Теперь совпадение ищется двумя способами: по коду, если модель его всё же
        назвала, и по названию. Если не нашлось ничего — карточек не будет:
        показать наугад хуже, чем не показать.
        """
        by_code = [sku for sku in tools.shown_skus if sku in answer]
        if by_code:
            return _unique([sku for sku in by_code if not _rejected(answer, sku)])

        words = _significant(answer)
        pairs = {(words[i], words[i + 1]) for i in range(len(words) - 1)}
        matched = []
        for sku in _unique(tools.shown_skus):
            product = self.engine.index.get(sku)
            if product is None or not _named_in(product.name, words, pairs):
                continue
            # Название в каталоге начинается с кода перечня: «2.14.106 Установка для изучения
            # фотоэффекта». Ночью 15.09 (сц. 11) модель перечислила пункты 2.14.1–2.14.3, которых
            # в каталоге нет, а карточки пришли от 2.14.106 и 2.14.100: совпали слова «лабораторный
            # демонстрационный». Пункт не назван в ответе — товар не он.
            code = _name_code(product.name)
            if code and not names_code(answer, code):
                continue
            # Модель бывает права, отказывая: 01.09 она сама выяснила, что код
            # 45892 — игрушечный бронемобиль, честно об этом написала, а карточка
            # бронемобиля всё равно пришла — «упомянут» и «рекомендован» тут не
            # различались. Сомневаемся — карточку не показываем.
            if _rejected(answer, product.name):
                continue
            # 15.09 строка «Звонкий-глухой (Д-214)» получила карточку «Логопедическое лото (Д-222)»: совпали
            # общие слова «лото», «настольно-печатная игра». Строка с чужим артикулом — не этот товар.
            if not _marks_agree(product.name, answer):
                continue
            matched.append(sku)
        return matched

    def _with_catalog_positions(self, tools: ToolBox, answer: str) -> str:
        """Пункты перечня в ответе — не товары: дописываем позиции каталога тех же разделов.

        Ночью 15.09 на «покажите первые три позиции из раздела» бот перечислил пункты приказа
        2.14.1–2.14.3 («столов в каталоге нет»), а карточками прислал 2.14.106, 2.14.100 и 2.14.30 —
        человек видел один список, а под ним другие товары. Теперь список и карточки собираются из
        одних и тех же позиций: текст дописывается кодом, поэтому названия и цены — из каталога.
        """
        if self._mentioned_skus(tools, answer):
            return answer
        products = self._section_products(tools, answer)
        if not products:
            return answer
        lines = [
            f"{number}. {product.name} — {price_text(product.price)}, {stock_text(product)}"
            for number, product in enumerate(products, 1)
        ]
        return answer + "\n\n" + _CATALOG_BLOCK + "\n" + "\n".join(lines)

    def _section_products(self, tools: ToolBox, answer: str) -> list:
        """Позиции каталога из тех же разделов перечня, что названы в ответе."""
        sections = set()
        for code, _ in listed_codes(answer):
            parts = code.split(".")
            section = ".".join(parts[:-1]) if len(parts) > 2 else code
            if section.count(".") >= 1:
                sections.add(section)
        if not sections:
            return []
        products = []
        for sku in _unique(tools.shown_skus):
            product = self.engine.index.get(sku)
            code = _name_code(product.name) if product is not None else ""
            if code and not names_code(answer, code) and code.rsplit(".", 1)[0] in sections:
                products.append(product)
        return products[:CARDS_SHOWN]

    # --- Цикл вызова инструментов -------------------------------------------

    def _ask(self, messages: list[dict], tools: ToolBox, schemas: list[dict] | None) -> str:
        """Ход разговора: пробуем провайдеров по очереди, пока кто-то не ответит.

        Каждому даём свою копию сообщений. Цикл вызова инструментов дописывает
        в них ответы модели, и остатки неудачной попытки не должны утекать
        следующему провайдеру: служебные поля у них разные.
        """
        last: LLMError | None = None
        for client in self.router.ready():
            try:
                answer = self._run(client, list(messages), tools, schemas)
            except LLMError as exc:
                self.router.mark_down(client, exc)
                last = exc
                continue
            self.router.mark_up(client)
            return answer
        raise last or LLMError("нет настроенных провайдеров модели")

    def _run(
        self,
        client: ChatClient,
        messages: list[dict],
        tools: ToolBox,
        schemas: list[dict] | None,
    ) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            if tools.calls >= TURN_CALLS:
                # Лимит хода исчерпан: дальше только просьба ответить по собранному.
                tools.session.route["call_limit"] = tools.calls
                break
            message = client.complete(messages, tools=schemas)
            tools.calls += 1
            account_usage(tools.session, client, message)
            calls = message.get("tool_calls") or []
            if not calls:
                return (message.get("content") or "").strip()

            # Ответ модели возвращаем в историю как есть. Пересобирать его из
            # content и tool_calls нельзя: рассуждающие модели отдают ещё и
            # reasoning_content, а Cloud.ru требует это поле обратно — без него
            # следующий запрос падает с «Missing reasoning_content field».
            messages.append(_assistant_message(message))
            for call in calls:
                function = call.get("function", {})
                arguments = _parse_arguments(function.get("arguments"))
                result = tools.run(function.get("name", ""), arguments)
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
                )

        # Инструменты вызывались снова и снова без итогового ответа — просим завершить.
        messages.append(
            {
                "role": "user",
                "content": "Ответь пользователю по уже собранным данным, без новых вызовов.",
            }
        )
        final = client.complete(messages)
        tools.calls += 1
        account_usage(tools.session, client, final)
        return (final.get("content") or "").strip()

    # --- Обещание вместо подбора ---------------------------------------------------

    def _insist(self, messages: list[dict], tools: ToolBox, answer: str) -> str:
        log.warning("Продавец пообещал подбор, не вызвав его, — просим подобрать в этом ходе.")
        messages.append({"role": "assistant", "content": answer, "reasoning_content": ""})
        messages.append({"role": "user", "content": _INSIST})
        try:
            second = without_meta(self._ask(messages, tools, tools_for(SELL)))
        except LLMError:
            return answer
        # Осталась одна вежливость — показываем прежний ответ, а не «Понял, спасибо за замечание».
        return second or answer

    def _instead_of_promise(  # noqa: ANN001
        self, session, tools: ToolBox, question: str, decision: Decision, answer: str
    ) -> list[Response]:
        """Подбора так и не было — текст с обещанием человеку не уходит.

        Можно показывать — подбирает Procurement Core по реплике человека. Нельзя —
        остаётся то, что в ответе было кроме обещания, или один вопрос о недостающем.
        Выдачи «по помещению из профиля» здесь нет и быть не должно: на «массажные мячи»
        она приносила тележку и ребристую доску.
        """
        log.warning("Подбор так и не выполнен — ответ модели заменён.")
        profile = session.profile
        # Вопрос о показанном — «чем эти мячи полезны?», «сколько стоит первый?» — это
        # презентация, а не новый подбор: слова вопроса ядро приняло бы за товар.
        about_shown = bool(profile.offered) and "?" in question and not intent.asks_to_show(question)
        allowed, _ = may_show_cards(profile, decision)
        if allowed and not about_shown:
            session.route["fallback"] = "procurement_select"
            return self.engine.select_offer(session, question, "Подобрал в каталоге")

        kept = self._kept(session, question, answer)
        if about_shown:
            text = kept or (
                "Расскажу по конкретной позиции: нажмите «Подробнее» на её карточке или "
                "назовите её — характеристики, цену и наличие возьму из каталога."
            )
        elif profile.objection != "none" and not profile.objection_handled:
            text = kept or _objection_question(profile.objection, self.engine.settings.delivery_url)
        elif not profile.task_known and not decision.precise:
            text = selection.question(_missing(profile))
        else:
            text = kept or "Показать подходящие варианты из каталога?"
        session.route["fallback"] = "question"
        session.remember("assistant", text)
        return [Message(text, keyboard=self._keyboard(session, tools, decision))]

    def _kept(self, session, question: str, answer: str) -> str:  # noqa: ANN001
        """Что из ответа модели можно показать без обещания.

        Остаток проходит ту же проверку цен и оснований, что и обычный ответ: обходить её
        он не должен. Пустая вежливость — «извините за задержку» — ответом не считается.
        """
        kept = without_promises(answer)
        prices = session.prices | prices_in(question) | prices_in(session.profile.budget or "")
        if len(kept) < MIN_KEPT or self._complaint(kept, prices, session.norm_refs, session):
            return ""
        return kept

    def _without_selection_promise(self, session, answer: str) -> str:  # noqa: ANN001
        """Ответ консультанта без обещания подбора — и с одним вопросом, ведущим дальше.

        Задача ясна — предлагаем перейти к подбору: на согласие ответит продавец настоящими
        позициями. Не ясна — спрашиваем недостающее. Вопрос в ответе уже есть — второй не
        добавляем: консультант задаёт один вопрос за сообщение.
        """
        kept = without_promises(answer)
        if len(kept) < MIN_KEPT:
            kept = ""
        if "?" in kept:
            return kept
        profile = session.profile
        ask = "Подобрать варианты из каталога?" if profile.task_known else selection.question(_missing(profile))
        return f"{kept}\n\n{ask}" if kept else ask

    # --- Проверка ответа -------------------------------------------------------

    def _verified(  # noqa: ANN001
        self,
        answer: str,
        messages: list[dict],
        tools: ToolBox,
        question: str,
        session,
        schemas: list[dict] | None = None,
        branch: str = SELL,
    ) -> str:
        """Ответ, в котором каждая сумма и каждый пункт приказа подтверждены данными.

        Одна попытка исправиться: модель почти всегда переписывает ответ честно,
        когда ей называют конкретные лишние числа. Если и второй ответ выдуман, у
        консультанта из него убираются строки с неподтверждённым, у продавца
        возвращается пустая строка, и вызывающая сторона отвечает подбором.
        Отвергнутый текст пишется в журнал хода: 14.09 без него было не понять,
        выдумала модель «пункт 33.1.2» или ошиблась проверка.
        """
        # Цены и основания за весь разговор, а не только за этот ход. Отвечая на
        # «дорого», модель ссылается на уже показанные позиции и в инструменты не
        # ходит — проверка по одному ходу отвергала такой ответ дважды подряд и
        # роняла его в выдачу каталога. Поймано на живом прогоне 01.09.
        prices = (
            tools.prices
            | session.prices
            | prices_in(question)
            | prices_in(session.profile.budget or "")
        )
        refs = tools.norm_refs | session.norm_refs
        complaint = self._complaint(answer, prices, refs, session)
        if not complaint:
            return answer

        log.warning("%s Просим переписать ответ.", complaint)
        session.route["rewritten"] = {"complaint": complaint, "answer": answer[:DISCARDED_KEPT]}
        second = ""
        if tools.calls < TURN_CALLS:
            messages.append({"role": "assistant", "content": answer, "reasoning_content": ""})
            messages.append({"role": "user", "content": complaint + _REWRITE_HINT})
            try:
                # Переписывает та же роль и с теми же инструментами: консультанту на переписывании
                # раньше выдавался весь набор продавца, и он отвечал «уточним через инструменты».
                second = self._ask(messages, tools, schemas if schemas is not None else TOOL_SCHEMAS)
            except LLMError:
                second = ""
            # Просьбу переписать модель принимает за реплику человека и отвечает на неё: «Спасибо,
            # что поправили», «Переписываю строго по данным из инструментов». Ночью 15.09 это ушло
            # клиенту 26 раз в 17 диалогах из 25, а в двух ходах кроме извинения не было ничего.
            second = without_meta(second)
        else:
            session.route["call_limit"] = tools.calls

        prices |= tools.prices
        refs |= tools.norm_refs
        if second:
            second_complaint = self._complaint(second, prices, refs, session)
            if not second_complaint:
                return second
            session.route["discarded"] = {"complaint": second_complaint, "answer": second[:DISCARDED_KEPT]}
        # Молчать не из чего: оставляем подтверждённые строки — сначала переписанного ответа, потом
        # первого. Раньше так делал только консультант, а пустой ответ продавца уходил в выдачу
        # каталога и на возражении оборачивался «консультант временно недоступен» (ночь 15.09).
        for text in (second, answer):
            kept = without_unverified(text, prices, refs, self._registry_problems(text, session)[1])
            if len(kept) >= MIN_KEPT:
                log.warning("Ответ выдуман повторно — строки с неподтверждённым убраны.")
                session.route["fallback"] = "unverified_lines_removed"
                return f"{kept}\n\n{_UNVERIFIED_NOTE}"
        log.warning("Ответ выдуман повторно, подтверждённых строк не осталось — текст не показываем.")
        return ""

    def _complaint(
        self, answer: str, prices: set[int], refs: set[tuple[str, str]], session=None  # noqa: ANN001
    ) -> str:
        """Что в ответе не подтверждено данными. Пустая строка — всё в порядке."""
        parts: list[str] = []
        foreign = foreign_script(answer)
        if foreign:
            parts.append(
                "слова не на русском: " + ", ".join(f"«{word}»" for word in sorted(foreign)) + " — замени их русскими"
            )
        if claims_handoff(answer):
            # Ночью 16.09 шесть диалогов из пятидесяти кончились словами «всё передал
            # менеджеру» — при том, что заявка уходит только с контактом человека.
            parts.append(
                "слова о том, что заявка менеджеру уже передана, — бот сам ничего не передаёт: "
                "напиши, что менеджер получит заявку, когда человек оставит имя и телефон"
            )
        parts.extend(self._registry_problems(answer, session)[0])
        invented = invented_prices(answer, prices)
        if invented:
            parts.append(
                "суммы, которых нет в результатах инструментов: "
                + ", ".join(f"{price} ₽" for price in sorted(invented))
            )
        wrong_refs = invented_norm_refs(answer, refs)
        if wrong_refs:
            parts.append(
                "нормативные основания, которых инструменты не возвращали: "
                + describe_refs(wrong_refs)
            )
        return f"В твоём ответе есть {' и '.join(parts)}." if parts else ""

    def _registry_problems(self, answer: str, session=None) -> tuple[list[str], set[str]]:  # noqa: ANN001
        """Коды из строк списка и «раздел X» — против текста приказов, раздел группы — против возраста клиента.

        Ночью 14.09 проверка оснований смотрела только «пункт X»: прошли пункты 1.14.5.7.1.39–48, которых в
        приказе нет (сц. 11), раздел 2.12 приказа 838 «Словари» под видом кабинета ИЗО (сц. 53) и группа
        1–2 лет для детей 5–6 лет (сц. 2). Возвращает жалобы и коды, чьи строки можно вырезать.
        """
        index = getattr(self.engine, "norm_texts", None)
        if index is None or not index.loaded:
            return [], set()
        client = client_ages(session.profile.age) if session is not None else None
        # Пункты ФГОС и ФОП в реестре не разобраны — их коды «неизвестными» не считаем.
        strict = not re.search(r"ФГОС|ФОП", answer or "")
        unknown: list[str] = []
        renamed: list[str] = []
        other_age: list[str] = []
        bad: set[str] = set()
        for code, claimed in dict(listed_codes(answer)).items():
            docs = index.documents_with(code)
            if not docs:
                if strict:
                    unknown.append(code)
                    bad.add(code)
                continue
            titles = [index.get(doc, code).title for doc in docs]
            if claimed and not title_matches(claimed, titles):
                renamed.append(f"{code} — в приказе «{titles[0]}»")
                bad.add(code)
                continue
            if client is None:
                continue
            for doc in docs:
                chain = [*index.parents(doc, code), index.get(doc, code)]
                group = next((ages for item in reversed(chain) if (ages := section_ages(item.title))), None)
                if group and not (group[0] <= client[1] and client[0] <= group[1]):
                    other_age.append(f"{code} (для детей {group[0]}–{group[1]} лет)")
                    bad.add(code)
                    break
        problems = []
        if unknown:
            problems.append("пункты, которых нет ни в одном приказе: " + ", ".join(unknown))
        if renamed:
            problems.append("пункты, названные не так, как в приказе: " + "; ".join(renamed))
        if other_age:
            problems.append(f"разделы для другого возраста, а у клиента {session.profile.age}: " + ", ".join(other_age))
        return problems, bad

    # --- Сборка ответа --------------------------------------------------------

    def _render(  # noqa: ANN001
        self,
        session,
        tools: ToolBox,
        answer: str,
        question: str,
        decision: Decision,
        show_cards: bool,
    ) -> list[Response]:
        # Карточки показываем по товарам, которые агент действительно назвал: так
        # текст ответа и карточки не расходятся.
        mentioned = self._mentioned_skus(tools, answer) if show_cards else []

        responses: list[Response] = []
        if answer:
            # Коды 1С нужны нам для сведения текста с карточками, но человеку в
            # ответе они ни к чему — это внутренний артикул, а не характеристика.
            responses.append(Message(_without_codes(answer), keyboard=self._keyboard(session, tools, decision)))

        for sku in mentioned[:CARDS_SHOWN]:
            product = self.engine.index.get(sku)
            if product is None:
                continue
            responses.append(
                ProductCard(
                    product=product,
                    citation=self._citation(session, tools, product),
                    keyboard=Keyboard().row(
                        Button("В корзину", f"add:{sku}"),
                        Button("Подробнее", f"card:{sku}"),
                    ),
                    # Без этих полей карточки от модели приходили без снимка в
                    # обоих каналах, даже когда файл лежал у нас на диске.
                    image=self.engine._image(product),
                    image_path=self.engine.photo_path(product),
                )
            )

        if not responses:
            # Модель промолчала — отвечаем предложением по исходному вопросу.
            return self.engine.offer(session, question)

        # Страховки, которая дописывала к ответу выдачу поиска по помещению из профиля,
        # здесь больше нет (NEXT-4.1): товары в ответе — только из подбора ядра, а
        # обещание подбора без самого подбора разбирается до сборки ответа.
        return responses

    def _citation(self, session, tools: ToolBox, product) -> str | None:  # noqa: ANN001
        """Основание для карточки — то же, что бот назвал в тексте.

        Раньше текст брал основание из результата поиска, а карточка считала его
        заново по аудитории профиля, и в одном сообщении оказывались два разных
        приказа: «привязана к приказу 838» в тексте и «позиция 1.13.4.3.1.6 —
        приказ 1057» на карточке под ним. Теперь основание — из подбора ядра.
        """
        if product.sku_1c in tools.citations:
            return tools.citations[product.sku_1c]
        norm = product.norm_for(session.profile.audience, session.profile.room or "")
        return norm.citation if norm else None

    # --- Действия без модели -------------------------------------------------------

    def _service_reply(self, session, text: str, decision: Decision) -> list[Response] | None:  # noqa: ANN001
        """Ответ ядра без модели: файл, список из N позиций, следующая страница подбора."""
        profile = session.profile
        # Срока от оформления до счёта и графика поставок нет ни в каталоге, ни в приказах —
        # их называет менеджер. Ночью 16.09 этот вопрос остался без ответа в семи диалогах,
        # а в сц. 3 трижды подряд получил «Пришлю комплектацию файлом. В каком виде?».
        deadline = intent.asks_deadline(text)
        if decision.intent == EXPORT_REQUEST:
            offer = exports.offer(self.engine, session)
            if offer is not None:
                session.route["fallback"] = "export"
                # Файл просили вместе со сроком — отвечаем и на то, и на другое.
                return [*self._deadline_note(session), *offer] if deadline else offer
            # Выгружать нечего: «Сохранять пока нечего: сначала соберём комплектацию» было ответом
            # на «нужна спецификация в Excel и счёт» в 11 диалогах из 25 (ночь 15.09) — и разговор
            # на этом кончался. Пусть отвечает агент: он и соберёт то, что потом уйдёт файлом.
        if deadline and not _about_goods(text):
            session.route["fallback"] = "deadline"
            return self._deadline_note(session, self._manager_keyboard(session))
        size = intent.list_size(text)
        # Оформление по присланному файлу — ядро, а не модель: 15.09 на «сформируй предзаказ» и «все найденные
        # по 1 шт.» модель трижды пересобрала строки файла по-разному (14 из 15, потом 4 из 15).
        if profile.order and profile.export == "order" and intent.asks_order_checkout(text):
            session.route["fallback"] = "order_cart"
            return self.engine.order_cart(session, override=intent.each_quantity(text))
        # «Оформить», «выставьте счёт» без присланного файла: корзина и предзаказ — кодом. Ночью
        # 15.09 на «Оформить. Согласен. Организация, контакт…» модель присылала анкету «1. Название
        # организации…» или советовала нажать кнопку, которой под сообщением не было: за 25 диалогов
        # ни одного предзаказа и ни одной непустой корзины.
        if not profile.order and intent.asks_checkout(text):
            checkout = self.engine.checkout_by_intent(session, text)
            if checkout is not None:
                session.route["fallback"] = "checkout"
                return checkout
        # Присланный заказ: «подбери по этому заказу», «из наличия 30 позиций» и «а ещё» — по его строкам.
        if profile.order and (intent.mentions_order(text) or (size and profile.export == "order")):
            session.route["fallback"] = "order_list"
            return self.engine.order_list(session, text, size)
        if profile.order and profile.export == "order" and profile.order.get("shown") and intent.asks_more(text):
            session.route["fallback"] = "order_more"
            return self.engine.order_list(session, text, None, more=True)
        if size and (decision.sells or "налич" in text.lower()):
            session.route["fallback"] = "shortlist"
            return self.engine.shortlist(session, text, size)
        if decision.sells and intent.asks_more(text) and profile.procurement_task_id and profile.offered:
            session.route["fallback"] = "select_more"
            return self.engine.more_selection(session)
        # Детский сад целиком, без помещения — разделы приказа 1057 по помещениям и вопрос, с какого начать.
        if whole_object(text) and profile.audience == "preschool" and not profile.room and not intent.names_goods(text):
            rooms = self.engine.object_rooms(session)
            if rooms is not None:
                session.route["fallback"] = "object_rooms"
                return rooms
        return None

    def _deadline_note(self, session, keyboard: Keyboard | None = None) -> list[Response]:  # noqa: ANN001
        """Честный ответ о сроке: данных нет, называет менеджер по заявке."""
        cart = self.engine.storage.load_cart(session.user_id).count
        text = _DEADLINE_ANSWER + (_DEADLINE_WITH_CART if cart else _DEADLINE_WITHOUT_CART)
        session.remember("assistant", text)
        return [Message(text, keyboard=keyboard)]

    def _manager_keyboard(self, session) -> Keyboard | None:  # noqa: ANN001
        keyboard = Keyboard().row(Button("Связаться с менеджером", "manager"))
        if self.engine.storage.load_cart(session.user_id).count:
            keyboard.row(Button("Корзина", "cart"), Button("Оформить", "checkout"))
        return keyboard

    def _consult_question(self, session, tools: ToolBox, decision: Decision) -> list[Response]:  # noqa: ANN001
        session.route["fallback"] = "consult_question"
        session.remember("assistant", _CONSULT_RETRY)
        return [Message(_CONSULT_RETRY, keyboard=self._keyboard(session, tools, decision))]

    def _keyboard(self, session, tools: ToolBox, decision: Decision) -> Keyboard | None:  # noqa: ANN001
        """Кнопки под ответом модели.

        «Корзина» и «Оформить» — этап продавца и только при непустой корзине. 14.09 они
        стояли под ответом консультанта на «предложи по 1057 указу»: человек ещё выясняет
        задачу, а ему предлагают оформить пустую корзину.
        """
        keyboard = Keyboard()
        if decision.branch == CONSULT and tools.kit:
            # Комплектация раздела — файлом (решение заказчика 14.09: кратко в чате, полностью в файле).
            exports.buttons(keyboard)
        if tools.handoff_reason:
            # Ночью 14.09 кнопка вела на "menu": человек просил менеджера и получал «Чем помочь?».
            keyboard.row(Button("Связаться с менеджером", "manager"))
        if decision.sells and self.engine.storage.load_cart(session.user_id).count:
            keyboard.row(Button("Корзина", "cart"), Button("Оформить", "checkout"))
        return keyboard if keyboard.rows else None

    def _system_prompt(self, session, decision: Decision) -> str:  # noqa: ANN001
        """Промпт выбранной роли, текущий запрос и то, что уже известно об этом разговоре.

        Строка о запросе — контекст перехода (ORCHESTRATOR.md, раздел 18): консультант видит,
        что комплектацию надо довести до списка, продавец — что консультация уже была.
        """
        parts = [self.prompts.get(decision.branch) or self.prompts[SELL]]
        title = INTENT_TITLES.get(decision.intent)
        if title and decision.branch in (CONSULT, SELL):
            line = f"## Текущий запрос\n\n{title[0].upper()}{title[1:]}."
            if decision.branch == SELL and decision.previous == CONSULT:
                line += " До этого разговор вёл консультант: задача и комплектация — в переписке, заново не расспрашивай."
            parts.append(line)
        profile = session.profile.as_prompt()
        if profile:
            parts.append(profile)
        return "\n\n".join(parts)

    def _history(self, session) -> list[dict]:  # noqa: ANN001
        """Переписка для модели.

        Маскировать здесь нечего: в сессию реплики попадают уже с метками вместо
        персональных данных, и на диске лежат в том же виде.
        """
        history = []
        for item in session.history[-HISTORY_LIMIT:]:
            content = item["content"]
            if item["role"] == "assistant" and len(content) > HISTORY_CHARS:
                # Комплектация на 88 позиций раздувала каждый следующий ход (14.09: 52 тыс. токенов на входе),
                # а полностью она всё равно лежит в профиле.
                content = content[:HISTORY_CHARS].rstrip() + "\n…(сокращено)"
            message = {"role": item["role"], "content": content}
            if item["role"] == "assistant":
                # Само рассуждение не храним, но поле должно присутствовать:
                # валидатор Cloud.ru требует его у каждого ответа ассистента.
                message["reasoning_content"] = ""
            history.append(message)
        return history


def account_usage(session, client: ChatClient, message: dict) -> None:  # noqa: ANN001
    """Складывает расход модели за ход в сессию.

    Ход почти никогда не равен одному обращению: сначала вызовы инструментов,
    потом ответ, иногда ещё и переписывание из-за выдуманной цены. Стоимость
    сценария — это сумма всех, поэтому считаем накопительно.
    """
    usage = message.get("_usage")
    if not usage:
        return
    tokens_in = int(usage.get("tokens_in") or 0)
    tokens_out = int(usage.get("tokens_out") or 0)
    cost = (tokens_in * client.price_in + tokens_out * client.price_out) / 1_000_000

    box = session.usage
    box["provider"] = client.name
    box["model"] = usage.get("model") or client.model
    box["calls"] = int(box.get("calls", 0)) + 1
    box["tokens_in"] = int(box.get("tokens_in", 0)) + tokens_in
    box["tokens_out"] = int(box.get("tokens_out", 0)) + tokens_out
    box["cost_rub"] = round(float(box.get("cost_rub", 0.0)) + cost, 4)


def _missing(profile) -> list[str]:  # noqa: ANN001 — core.profile.DialogProfile
    """Чего не хватает для подбора: учреждение и помещение."""
    return [
        name
        for name, known in (("institution_type", profile.institution), ("room", profile.room))
        if not known
    ]


def _assistant_message(message: dict) -> dict:
    """Ответ модели в том виде, в каком его примут обратно.

    Провайдеры расходятся в служебных полях, поэтому ничего не выбрасываем и
    ничего не придумываем: берём пришедшее и добавляем только то, чего нет.
    Наши собственные пометки (они начинаются с подчёркивания) провайдеру,
    разумеется, не возвращаем — он их не поймёт.
    """
    kept = {
        key: value
        for key, value in message.items()
        if value is not None and not key.startswith("_")
    }
    kept.setdefault("role", "assistant")
    kept.setdefault("content", "")
    kept.setdefault("reasoning_content", "")
    return kept


def _parse_arguments(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _unique(items) -> list[str]:  # noqa: ANN001
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _significant(text: str) -> list[str]:
    """Основы слов длиннее двух букв — по ним сверяются названия товаров."""
    from catalog.text import stems

    return [word for word in stems(text) if len(word) > 2 and not word.isdigit()]


# Модель отговаривает от позиции, а не предлагает её.
_REJECTION = re.compile(
    r"не\s+подойд\w+|не\s+подход\w+|не\s+соответству\w+|не\s+рекоменд\w+|"
    r"не\s+относ\w+|не\s+числ\w+|не\s+для\s+|вряд\s+ли|ошибочн\w+|"
    r"это\s+не\s+то|исключ\w+\s+из|игрушечн\w+",
    re.IGNORECASE,
)
# Предложения делим по точке с большой буквы, переводу строки и точке с запятой.
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")


def _rejected(answer: str, needle: str) -> bool:
    """Названо ли это только затем, чтобы отказать.

    Смотрим предложения, где позиция упомянута: если все они содержат отказ,
    карточке под ответом взяться неоткуда.
    """
    words = _significant(needle)
    if not words:
        return False
    mentions = []
    for sentence in _SENTENCE.split(answer or ""):
        low = sentence.lower()
        if needle.lower() in low or all(word in low for word in words[:2]):
            mentions.append(sentence)
    return bool(mentions) and all(_REJECTION.search(sentence) for sentence in mentions)


# Код перечня в начале названия товара: «2.14.106 Установка для изучения фотоэффекта».
_NAME_CODE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3}){1,5})\s")
_CATALOG_BLOCK = "Что по этим пунктам есть в каталоге:"


def _name_code(name: str) -> str:
    match = _NAME_CODE.match(name or "")
    return match.group(1) if match else ""


def names_code(answer: str, code: str) -> bool:
    """Назван ли в ответе именно этот пункт: 2.14.10 — не 2.14.106."""
    return bool(re.search(rf"(?<![\d.]){re.escape(code)}(?![\d.])", answer or ""))


def _named_in(name: str, words: list[str], pairs: set[tuple[str, str]]) -> bool:
    """Названо ли это в ответе.

    Названия у заказчика начинаются с кода поставщика — «ВТ ПЛ Парта логопеда», —
    а модель пишет «парта логопеда». Поэтому сверяются не строки, а пары соседних
    значимых слов: одно общее слово («набор», «комплект») есть у половины каталога
    и совпадением не является.
    """
    own = _significant(name)
    if not own:
        return False
    if len(own) == 1:
        return own[0] in words
    return any((own[i], own[i + 1]) in pairs for i in range(len(own) - 1))


# Артикул поставщика в названии: «Д-214», «С-913», «У1076», «KF0015». Цифры отдельно — не артикул.
_MARK = re.compile(r"(?<![0-9A-Za-zА-Яа-яЁё])[A-Za-zА-Яа-яЁё]{1,4}-?\d{2,6}(?![0-9A-Za-zА-Яа-яЁё])")


def _marks(text: str) -> set[str]:
    return {mark.replace("-", "").lower() for mark in _MARK.findall(text or "")}


def _marks_agree(name: str, answer: str) -> bool:
    """Не стоит ли название в строке, где назван другой артикул.

    Строка без артикулов не мешает: модель часто пишет «Азбука, настольная игра» без кода поставщика.
    """
    own = _marks(name)
    if not own:
        return True
    naming = []
    for line in (answer or "").splitlines():
        words = _significant(line)
        if _named_in(name, words, {(words[i], words[i + 1]) for i in range(len(words) - 1)}):
            naming.append(_marks(line))
    return not naming or any(not marks or marks & own for marks in naming)


# Комплектация раздела в чате — кратко, полный список в файле (решение заказчика 14.09). Ночью консультант
# всё равно писал раздел целиком, до 4 тыс. знаков, при кнопках «Скачать» под тем же сообщением.
KIT_ANSWER_CHARS = 2500
_KIT_TAIL = "Полный список — в файле, кнопки под сообщением."


def _short_kit_answer(answer: str) -> str:
    if len(answer) <= KIT_ANSWER_CHARS:
        return answer
    last = answer.rstrip().rsplit("\n\n", 1)[-1]
    question = last if last.rstrip().endswith("?") and len(last) <= 300 else ""
    budget = KIT_ANSWER_CHARS - len(_KIT_TAIL) - len(question) - 4
    cut = answer.rfind("\n", 0, budget)
    body = answer[: cut if cut > 0 else budget].rstrip()
    return "\n\n".join(part for part in (body, _KIT_TAIL, question) if part)


def _without_codes(answer: str) -> str:
    return _EMPTY_BULLET.sub("", _DOUBLE_COMMA.sub(",", _CODE_MENTION.sub("", answer)))
