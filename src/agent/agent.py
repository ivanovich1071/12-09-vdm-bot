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
from agent.routing import CONSULT, GUARD, INTENT_TITLES, SELL, Decision, Orchestrator
from agent.tools import TOOL_SCHEMAS, ToolBox
from agent.verify import (
    describe_refs,
    invented_norm_refs,
    invented_prices,
    prices_in,
    promises_goods,
    without_promises,
)
from core import intent, selection
from core.ui import Button, Keyboard, Message, ProductCard, Response

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_TOOL_ROUNDS = 4
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
# Один вопрос по неснятому возражению, когда от ответа модели ничего не осталось. Порядок —
# первый шаг работы с возражением из промпта продавца: понять, что именно мешает.
_OBJECTION_QUESTIONS = {
    "price": (
        "Понимаю, бюджет важен. Что для вас главное — уложиться в лимит или понять, из чего "
        "складывается цена? От этого зависит, что предложить."
    ),
    "norm": "Понимаю сомнение. По какому пункту перечня нужно подтверждение? Сверю с текстом приказа.",
    "logistics": (
        "Сроки и доставку подтверждает менеджер. Что важнее — успеть к дате или взять всё одной поставкой?"
    ),
    "none": "Подскажите, что именно смущает — цена, соответствие перечню или сроки?",
}

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
    r"[ \t]*[(\[]?[ \t]*(?:\*\*)?(?:код\s*1\s*[СCc]|артикул)[\s*:]*[A-Za-z0-9А-ЯЁа-яё\-]+[ \t]*[)\]]?",
    re.IGNORECASE,
)
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

        answer = self._verified(answer, messages, tools, text, session, tools_for(decision.branch))
        if not answer:
            session.route["discarded_answer"] = True
            return self.engine.offer(session, text)

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
            # Модель бывает права, отказывая: 01.09 она сама выяснила, что код
            # 45892 — игрушечный бронемобиль, честно об этом написала, а карточка
            # бронемобиля всё равно пришла — «упомянут» и «рекомендован» тут не
            # различались. Сомневаемся — карточку не показываем.
            if _rejected(answer, product.name):
                continue
            matched.append(sku)
        return matched

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
            message = client.complete(messages, tools=schemas)
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
        account_usage(tools.session, client, final)
        return (final.get("content") or "").strip()

    # --- Обещание вместо подбора ---------------------------------------------------

    def _insist(self, messages: list[dict], tools: ToolBox, answer: str) -> str:
        log.warning("Продавец пообещал подбор, не вызвав его, — просим подобрать в этом ходе.")
        messages.append({"role": "assistant", "content": answer, "reasoning_content": ""})
        messages.append({"role": "user", "content": _INSIST})
        try:
            return self._ask(messages, tools, tools_for(SELL))
        except LLMError:
            return answer

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
            text = kept or _OBJECTION_QUESTIONS.get(profile.objection, _OBJECTION_QUESTIONS["none"])
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
        if len(kept) < MIN_KEPT or self._complaint(kept, prices, session.norm_refs):
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
    ) -> str:
        """Ответ, в котором каждая сумма и каждый пункт приказа подтверждены данными.

        Одна попытка исправиться: модель почти всегда переписывает ответ честно,
        когда ей называют конкретные лишние числа. Если и второй ответ выдуман,
        возвращаем пустую строку — вызывающая сторона ответит выдачей каталога.
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
        complaint = self._complaint(answer, prices, refs)
        if not complaint:
            return answer

        log.warning("%s Просим переписать ответ.", complaint)
        messages.append({"role": "assistant", "content": answer, "reasoning_content": ""})
        messages.append({"role": "user", "content": complaint + _REWRITE_HINT})
        try:
            # Переписывает та же роль и с теми же инструментами: консультанту на переписывании
            # раньше выдавался весь набор продавца, и он отвечал «уточним через инструменты».
            second = self._ask(messages, tools, schemas if schemas is not None else TOOL_SCHEMAS)
        except LLMError:
            return ""

        prices |= tools.prices
        refs |= tools.norm_refs
        if self._complaint(second, prices, refs):
            log.warning("Ответ выдуман повторно — отвечаем выдачей каталога.")
            return ""
        return second

    def _complaint(
        self, answer: str, prices: set[int], refs: set[tuple[str, str]]
    ) -> str:
        """Что в ответе не подтверждено данными. Пустая строка — всё в порядке."""
        parts: list[str] = []
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

    def _keyboard(self, session, tools: ToolBox, decision: Decision) -> Keyboard | None:  # noqa: ANN001
        """Кнопки под ответом модели.

        «Корзина» и «Оформить» — этап продавца и только при непустой корзине. 14.09 они
        стояли под ответом консультанта на «предложи по 1057 указу»: человек ещё выясняет
        задачу, а ему предлагают оформить пустую корзину.
        """
        keyboard = Keyboard()
        if tools.handoff_reason:
            keyboard.row(Button("Связаться с менеджером", "menu"))
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
            message = {"role": item["role"], "content": item["content"]}
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


def _without_codes(answer: str) -> str:
    return _EMPTY_BULLET.sub("", _CODE_MENTION.sub("", answer))
