"""Ядро диалога: одна логика продажи на все каналы.

Адаптер отдаёт сюда текст или действие пользователя и получает готовые примитивы
ответа. Ни Telegram, ни виджет не знают ни про корзину, ни про согласие, ни про
нормативные основания — иначе правила разъедутся между каналами и починить их
в одном месте станет невозможно.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from catalog.models import Availability, Product
from catalog.points import PointFinder
from catalog.runtime import CatalogRuntime, CatalogRuntimeState
from catalog.search import CatalogIndex, SearchHit, SearchQuery
from catalog.service import CatalogService
from core import exports, intent, selection
from core.config import Settings
from core.models import Cart, CartItem, Customer
from core.profile import DialogProfile
from core.storage import Storage
from core.ui import (
    Button,
    Keyboard,
    Message,
    OrderLine,
    OrderSummary,
    ProductCard,
    ProductList,
    Response,
    delivery_note,
    lead_accepted,
    order_accepted,
    plural,
    price_text,
    stock_text,
)
from norms import documents as norm_docs
from norms import items as norm_items
from norms import reference as norm_reference
from norms.check import check_point, nearest_section
from norms.extract import document_ids_in_text
from orders.service import OrderService
from privacy.consent import CONSENT_TEXT, CONSENT_VERSION
from privacy.masking import PHONE, Masker
from procurement import discovery
from procurement.models import SelectionResult, SelectionStatus

log = logging.getLogger(__name__)

# Помещения детского сада — разделы приказа 1057 в том порядке, в каком их оснащают чаще.
OBJECT_SECTIONS = ("1.14", "1.5", "1.6", "1.13", "1.7", "1.8", "1.10", "1.12", "3.2", "3.3")
_GROUP_AGE = re.compile(r"для\s+детей\s+(.+?)\s*$", re.IGNORECASE)

# Приветствие открытое: кнопки — подсказка, а не единственный путь. Четыре
# жёстких сценария на старте отсекали тех, кто пришёл с вопросом, а не с заявкой.
GREETING = (
    "Здравствуйте! Я консультант ЭЛТИ-КУДИЦ — помогу с оборудованием для детского "
    "сада или школы.\n\n"
    "Напишите своими словами, что нужно. Например:\n"
    "• «чем оснастить спортзал в саду, дети 3–6 лет»;\n"
    "• «что значит приказ 838» — объясню документ;\n"
    # Пример намеренно из приказа 1057: пункт 2.1.14, стоявший здесь раньше,
    # есть только в школьном приказе 838 — бот сам приглашал детский сад
    # ввести чужой номер, а потом объяснял, почему по нему ничего нет.
    "• «1.5.1» — покажу позиции по пункту перечня.\n\n"
    "Можно просто описать задачу — разберёмся вместе.\n"
    "Можете воспользоваться кнопками, но это не обязательно."
)

# Что бот умеет — по команде /help. Приветствие держим коротким, а перечень
# возможностей выносим сюда: он нужен тому, кто специально его спросил.
HELP = (
    "Что я умею:\n\n"
    "• подобрать оборудование по описанию задачи — «кабинет логопеда в детском саду»;\n"
    "• найти позиции по пункту перечня — «1.5.1» или «1.13.3»;\n"
    "• объяснить документ — «что значит приказ 838»;\n"
    "• собрать корзину и передать заявку менеджеру.\n\n"
    "Команды:\n"
    "/start — начать заново\n"
    "/cart — корзина\n"
    "/order — оформить заказ\n"
    "/manager — связаться с менеджером\n"
    "/my_data — что о вас хранится\n"
    "/delete_data — удалить данные и отозвать согласие"
)

# Сколько реплик разговора храним. Дальше модель всё равно не смотрит, а профиль
# помнит суть без переписки.
HISTORY_KEPT = 24

# Поля, которые бот спрашивает при оформлении. Больше не собираем: каждое лишнее
# поле — это лишние персональные данные, которые придётся защищать и удалять.
CHECKOUT_FIELDS: tuple[tuple[str, str], ...] = (
    ("organization", "Название организации (или «-», если заказ для себя):"),
    ("name", "Контактное лицо — как к вам обращаться:"),
    ("phone", "Телефон для связи:"),
    ("email", "E-mail (можно «-»):"),
    ("region", "Город или регион доставки:"),
    ("comment", "Комментарий к заказу (можно «-»):"),
)

# Ответы, которые данными заказа не становятся: названия кнопок и меню. «Оформить»,
# вписанное в организацию 17.09, уехало менеджеру в заявке.
_CONTACT_NOISE = re.compile(
    r"^(оформ\w*|корзин\w*|меню|менеджер\w*|каталог\w*|назад|отмен\w*|начать заново|да|ок)$",
    re.IGNORECASE,
)
# Телефон: цифры с разделителями, от десяти знаков — чтобы «Иванова» в поле телефона
# не проходила, а «+7 (900) 123-45-67» проходила.
_CONTACT_PHONE = re.compile(r"\+?\d[\d\s()\-]{9,}\d")

# Сколько позиций показываем за раз. Пять оказалось много: заказчик отдельно
# отметил, что выдача из пяти карточек «излишня». Три помещаются на экран
# целиком, и каждую можно показать со снимком, не превращая чат в ленту.
PAGE_SIZE = 3
# Верхний предел выборки: больше пользователю всё равно не показать,
# а отдавать в агент сотни позиций дорого и бессмысленно.
SEARCH_CAP = 50
# Сколько наименований перечисляем текстом, когда отвечаем без модели. Список
# читается одним взглядом и даёт понять, что вообще есть, а подробности —
# в трёх карточках под ним.
OFFER_NAMES = 10

# Постоянная клавиатура Telegram присылает нажатие обычным текстом — сопоставляем
# его с действием. Ключи в нижнем регистре, сравнение тоже.
KEYBOARD_ACTIONS: dict[str, str] = {
    "каталог": "catalog",
    "моя корзина": "cart",
    "корзина": "cart",
    "менеджер": "manager",
    "связаться с менеджером": "manager",
    # Сначала подтверждение: «Начать заново» стирает разговор и корзину (решение заказчика 14.09).
    "начать заново": "restart",
}


def _not_listed(audience: str | None) -> str | None:
    """Честная строка вместо пустого основания.

    Почти половина каталога к перечням не привязана — реестра «пункт 838 → код
    1С» у заказчика нет вовсе. Молчать об этом хуже, чем сказать: закупщик по
    пустому месту решит, что основание есть и просто не показано.
    """
    if audience == "preschool":
        return "в перечнях для дошкольных организаций эта позиция не числится — уточнит менеджер"
    if audience == "school":
        return "в перечнях для школ эта позиция не числится — уточнит менеджер"
    return "в перечнях приказов эта позиция не числится — уточнит менеджер"


@dataclass
class Session:
    """Состояние диалога одного пользователя.

    Разделено намеренно. Переписка и профиль переживают перезапуск — иначе бот
    забывает разговор посреди подбора. А незаконченный ввод контактов не хранится
    нигде: это чистые персональные данные, и спросить их заново дешевле, чем
    защищать на диске.

    **История хранится маскированной с момента записи.** Не «замаскируем перед
    отправкой модели», а именно так: телефон, попавший в переписку, не должен
    оказаться в базе даже на время.
    """

    user_id: str
    channel: str
    last_hits: list[SearchHit] = field(default_factory=list)
    checkout_step: int | None = None
    customer: Customer = field(default_factory=Customer)
    pending_checkout: bool = False
    # Лид без корзины (шаг 4.4): человек просил счёт/КП/реквизиты/звонок, и мы
    # ждём его контакты, прежде чем передать заявку менеджеру.
    pending_lead: bool = False
    lead_essence: str = ""
    # Шаг пошагового сбора данных лида: «name» → «city» → «phone». Пусто, пока
    # вопрос не задан; телефон в первой же реплике перескакивает шаги.
    lead_stage: str = ""
    history: list[dict[str, str]] = field(default_factory=list)
    profile: DialogProfile = field(default_factory=DialogProfile)
    # Один маскер на весь разговор: метки должны совпадать между сообщениями,
    # иначе модель видит [ИМЯ_1] в истории и [ИМЯ_2] в текущей реплике для
    # одного и того же человека.
    masker: Masker = field(default_factory=Masker)
    # Расход модели за текущий ход: токены, обращения, рубли. Живёт в сессии, а не
    # в агенте, потому что ходы разных людей считаются одновременно, в разных
    # потоках. В журнал уходит по завершении хода и обнуляется перед следующим.
    usage: dict[str, object] = field(default_factory=dict)
    # Кто отвечал и почему: роль, этап, возражение, показаны ли карточки и по
    # какой причине. Заполняет агент, читает журнал. На ручных прогонах без этого
    # не понять, почему бот промолчал карточками, — а прогоны заказчик делает сам.
    route: dict[str, object] = field(default_factory=dict)
    # Все цены, которые инструменты вернули за этот разговор. Проверка на
    # выдуманные суммы смотрела только текущий ход — и отвергала ответ, где
    # модель ссылается на позицию, показанную ходом раньше. Именно так теряется
    # ответ на возражение «дорого»: там инструменты не вызываются вовсе.
    prices: set[int] = field(default_factory=set)
    # То же для нормативных оснований: пары «приказ, пункт», которые инструменты
    # подтвердили за разговор. Модель ссылается на пункт из прошлого хода так же
    # свободно, как на цену, и проверка по одному ходу отвергала бы честный ответ.
    norm_refs: set[tuple[str, str]] = field(default_factory=set)
    # Какое сохранённое состояние разговора видел этот процесс. Разговор одного человека
    # ведут два процесса с общим хранилищем — бот и сервер Mini App, — и по отметке
    # процесс замечает, что разговор продолжился у соседа.
    state_stamp: tuple | None = None
    # Подряд идущие заглушки «консультант недоступен». Второй раз подряд заглушки
    # не будет: клиенту нужны менеджер и телефон, а не повтор про «временно».
    degradation_streak: int = 0

    def remember(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": self.masker.mask(content)})
        del self.history[:-HISTORY_KEPT]

    def forget(self) -> None:
        self.history.clear()
        self.profile = DialogProfile()
        self.masker = Masker()
        self.prices.clear()
        self.norm_refs.clear()


# Сколько последних показанных позиций кладём в корзину на «оформить», когда списка не было:
# человек видел три карточки — их и оформляем.
OFFERED_TO_CART = 3
# Сколько пунктов перечня разбираем за одно «оформить»: комплектация спортзала — два десятка.
POINTS_TO_CART = 40
# Сколько кандидатов-замен показываем в сообщении: полный список длиннее пары строк
# превращает ответ в простыню (21.09: у комплектации логопеда 6+ кандидатов на 88 пунктов).
REVIEW_TO_SHOW = 5
# Строк корзины в превью: сообщения Telegram длиннее 4096 знаков не уходят.
CART_PREVIEW_LINES = 20
# Строк корзины на странице кнопок: 8 строк × 4 кнопки — предел Telegram не грозит (шаг 7.3).
CART_PAGE_LINES = 8


# Вопрос «кто ты такой» не доходит до модели, если её нет, — отвечаем сами (ТЗ BUG-07).
_IDENTITY_QUESTION = re.compile(
    r"(?:вы|ты)\s+(?:бот|робот)\b|(?:вы|ты)\s+кто|это\s+(?:бот|робот)|робот\s*\?|человек\s*\?",
    re.IGNORECASE,
)


def _doc_short(doc_id: str | None) -> str:
    return norm_docs.get(doc_id).short_name if doc_id in norm_docs.DOCUMENTS else (doc_id or "")


def _positions_word(n: int) -> str:
    """позиция / позиции / позиций — для заголовка «По пункту N: M …» (ТЗ BUG-10)."""
    if n % 10 == 1 and n % 100 != 11:
        return "позиция"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "позиции"
    return "позиций"


class DialogEngine:
    def __init__(
        self,
        index: CatalogIndex | CatalogRuntime,
        storage: Storage,
        orders: OrderService,
        settings: Settings,
        agent=None,  # noqa: ANN001 — необязательная зависимость, см. agent/
        dialog_log=None,  # noqa: ANN001 — observability/dialog_log.py
        media=None,  # noqa: ANN001 — media/service.py, фото добираются с сайта
    ) -> None:
        # Каталог — одно состояние на процесс (`catalog/runtime.py`). Индекс,
        # переданный напрямую, становится состоянием без версии: так работают тесты.
        self.runtime = (
            index
            if isinstance(index, CatalogRuntime)
            else CatalogRuntime(CatalogRuntimeState.from_index(index))
        )
        self.storage = storage
        self.orders = orders
        self.settings = settings
        self.agent = agent
        self.dialog_log = dialog_log
        self.media = media
        # Procurement Core ставит сборка ядра (`core_api/composition.py`): подбор товаров в
        # диалоге идёт только через него. Процесс виджета собирает ядро лениво —
        # `procurement_provider` соберёт его при первом подборе.
        self.procurement = None
        self.procurement_provider = None
        # Предзаказный сервис ставит сборка ядра (`core_api.composition`): /preorders
        # в ядре отвечает и там, где шлюза нет — виджет, Mini App (шаг 5.2).
        self.preorders = None
        self._sessions: dict[str, Session] = {}
        # Пункты приказов с формулировками и поиском по словам. Файла может не
        # быть — тогда бот называет номер пункта без текста, как и раньше.
        # Справочник приказов — по настройке (`norm_items_path`), а не по
        # относительному пути: виджет и бот в одном процессе видят один файл (шаг 3.5).
        items_path = Path(self.settings.norm_items_path)
        self.norm_texts = norm_items.ItemIndex(
            norm_items.load(items_path), norm_items.load_meta(items_path)
        )

    @property
    def index(self) -> CatalogIndex:
        """Индекс версии, закреплённой за текущим ходом, а вне хода — текущей."""
        return self.runtime.state.index

    @index.setter
    def index(self, value: CatalogIndex) -> None:
        self.runtime.replace(CatalogRuntimeState.from_index(value))

    @property
    def catalog(self) -> CatalogService:
        """Каталог через доменный слой (EPIC 1) — из того же состояния, что `index`.

        Прежние вызовы `self.index` пока не переведены: бот и агент переходят на
        сервис в следующих EPIC, поведение диалога здесь не меняется.
        """
        return self.runtime.state.catalog

    @property
    def catalog_version(self) -> str | None:
        return self.runtime.state.version

    def procurement_service(self):  # noqa: ANN201 — procurement.service.ProcurementService | None
        if self.procurement is None and self.procurement_provider is not None:
            self.procurement_provider()
        return self.procurement

    def session(self, user_id: str, channel: str) -> Session:
        key = f"{channel}:{user_id}"
        if key not in self._sessions:
            self._sessions[key] = self._restore(user_id, channel)
        return self._sessions[key]

    def _restore(self, user_id: str, channel: str) -> Session:
        """Разговор, начатый до перезапуска бота.

        Метки маскирования после перезапуска раскрыть нечем — соответствие жило
        в памяти процесса. Это осознанный размен: ПДн не хранятся, а нераскрытая
        метка заменяется нейтральным словом при показе (см. `Masker.unmask`).
        """
        session = Session(user_id=user_id, channel=channel)
        try:
            saved = self.storage.load_dialog_state(user_id, channel)
        except Exception as exc:  # состояние не должно мешать начать разговор
            log.warning("Состояние диалога %s не прочитано: %s", user_id, exc)
            return session
        if saved:
            session.history = saved["history"]
            session.profile = DialogProfile.from_dict(saved["profile"])
            session.state_stamp = self._stamp(session)
        return session

    def _remember(self, session: Session) -> None:
        try:
            self.storage.save_dialog_state(
                session.user_id, session.channel, session.history, session.profile.to_dict()
            )
        except Exception as exc:  # запись состояния не стоит ответа пользователю
            log.warning("Состояние диалога %s не сохранено: %s", session.user_id, exc)
            return
        session.state_stamp = self._stamp(session)

    def _stamp(self, session: Session) -> tuple | None:
        try:
            return self.storage.dialog_state_stamp(session.user_id, session.channel)
        except Exception as exc:  # сверка не стоит ответа пользователю
            log.warning("Отметка разговора %s не прочитана: %s", session.user_id, exc)
            return session.state_stamp

    def _sync(self, session: Session) -> None:
        """Подхватить разговор, продолженный другим процессом.

        Бот и сервер Mini App — разные процессы с общим хранилищем, а сессия диалога
        живёт в памяти каждого. Без сверки бот продолжал бы по своей копии и при записи
        затёр бы то, что человек обсудил в Mini App, а переписка, удалённая там по
        /delete_data, вернулась бы на диск с первым же ответом бота.
        """
        stamp = self._stamp(session)
        if stamp == session.state_stamp:
            return
        saved = None
        if stamp is not None:
            try:
                saved = self.storage.load_dialog_state(session.user_id, session.channel)
            except Exception as exc:  # как при восстановлении: не мешаем ответить
                log.warning("Состояние диалога %s не прочитано: %s", session.user_id, exc)
                return
        if saved:
            session.history = saved["history"]
            session.profile = DialogProfile.from_dict(saved["profile"])
        else:
            # Удалено снаружи — по сроку хранения или по требованию субъекта.
            session.forget()
        session.state_stamp = stamp if saved else None

    # --- Точки входа ---------------------------------------------------------

    # Каждая точка входа закрепляет одну версию каталога на весь ход: поиск, пункт
    # приказа, карточка и цена внутри одного ответа берутся из одного снимка.

    def start(self, user_id: str, channel: str) -> list[Response]:
        with self.runtime.turn():
            self.session(user_id, channel)
            return [Message(GREETING, keyboard=self._main_menu())]

    def handle_text(self, user_id: str, channel: str, text: str) -> list[Response]:
        with self.runtime.turn():
            started = time.monotonic()
            # Шаги сбора контактов — это чистые персональные данные и ничего не дают
            # для настройки промптов. В журнал вместо них идёт отметка о шаге.
            session = self.session(user_id, channel)
            self._sync(session)
            collecting = session.checkout_step is not None
            session.usage = {}
            session.route = {}
            responses = self._handle_text(user_id, channel, text)
            logged = "<контактные данные при оформлении>" if collecting else text
            self._log(user_id, channel, "text", logged, responses, started)
            return responses

    def handle_action(self, user_id: str, channel: str, action: str) -> list[Response]:
        with self.runtime.turn():
            started = time.monotonic()
            # Расход и роль сбрасываются так же, как в текстовом ходе. Без этого на
            # нажатие «Корзина» в журнал уходили токены и рубли предыдущего ответа
            # модели — 02.09 один и тот же ход оказался посчитан трижды.
            session = self.session(user_id, channel)
            self._sync(session)
            session.usage = {}
            session.route = {}
            responses = self._handle_action(user_id, channel, action)
            self._log(user_id, channel, "action", action, responses, started)
            return responses

    def _log(
        self,
        user_id: str,
        channel: str,
        kind: str,
        incoming: str,
        responses: list[Response],
        started: float,
    ) -> None:
        if self.dialog_log is None:
            return
        agent = self.agent
        mode = "search" if agent is None or not getattr(agent, "available", True) else "agent"
        session = self.session(user_id, channel)
        self.dialog_log.turn(
            channel=channel,
            user_id=user_id,
            kind=kind,
            incoming=incoming,
            responses=responses,
            mode=mode,
            latency_ms=int((time.monotonic() - started) * 1000),
            cart_count=self.storage.load_cart(user_id).count,
            usage=session.usage or None,
            route=session.route or None,
        )

    def _handle_text(self, user_id: str, channel: str, text: str) -> list[Response]:
        session = self.session(user_id, channel)
        text = text.strip()
        if not text:
            return [Message("Напишите, что ищете.", keyboard=self._main_menu())]

        if text.startswith("/"):
            return self._handle_command(session, text)
        if session.checkout_step is not None:
            return self._collect_contact(session, text)

        # Нажатие постоянной клавиатуры приходит обычным текстом. Разбираем его
        # до обновления профиля: иначе слово «Каталог» уйдёт в разбор задачи.
        action = KEYBOARD_ACTIONS.get(text.strip().lower())
        if action is not None:
            return self._handle_action(user_id, channel, action)

        # Корзина словами (шаги 4.2–4.3): «возьму X 4 шт.» и «по этому списку
        # по 1 шт.» — действия ядра, они работают и без модели (BUG-03/04).
        # Они раньше карточки менеджера: прогон 05.10 (СЦ5/СЦ7) — «возьму
        # Лесенку 4 шт. … и счёт от менеджера» глоталась карточкой, и корзина
        # не пополнялась. Чистая просьба («позовите человека») слов добавления
        # не содержит и ниже всё равно дойдёт до менеджера.
        has_procurement = self.procurement_service() is not None
        if has_procurement and intent.asks_add(text):
            handled = self.add_by_text(session, text)
            if handled is not None:
                self._remember(session)
                return handled
        if has_procurement and intent.asks_list_to_cart(text):
            self._remember(session)
            return self._collect_list(session, text)

        # Лид без корзины (шаг 4.4): «нужен счёт / КП / реквизиты / перезвоните» —
        # заявка менеджеру и без позиций. Корзина словами важнее: «возьму X и
        # счёт» — это корзина, оформление само доведёт до менеджера.
        lead = self._lead_flow(session, text)
        if lead is not None:
            self._remember(session)
            return lead

        # Запрос менеджера (шаг 4.1): ни анкета, ни подбор, ни модель его не
        # глотают. «Позовите человека» — это просьба, а не имя контакта.
        if intent.asks_manager(text):
            return self._manager(session)
        # Вопросы о показанном — кодом (шаги 4.5–4.8), но только без модели:
        # с агентом возражение «дорого» ведёт модель (цена из показанного,
        # подсказка «разбить сейчас/потом»), а код — её запасной путь.
        if (
            self.agent is None
            and has_procurement
            and intent.classify(text) not in (intent.GREETING, intent.SMALL_TALK, intent.NORM_QUESTION)
        ):
            handled = self._shown_actions(session, text)
            if handled is not None:
                self._remember(session)
                return handled

        # Профиль обновляем до ответа: то, что человек сказал сейчас, должно
        # попасть в промпт этого же хода, а не следующего.
        session.profile.update_from_text(text)
        session.remember("user", text)

        doc_id = norm_reference.question_about_document(text)
        if doc_id is not None:
            responses = self._explain_norm(session, doc_id)
        elif self.agent is not None:
            responses = self.agent.reply(session, text)
        else:
            responses = self.offer(session, text)

        self._remember(session)
        return responses

    def _handle_action(self, user_id: str, channel: str, action: str) -> list[Response]:
        session = self.session(user_id, channel)
        verb, _, arg = action.partition(":")

        match verb:
            case "menu":
                return [Message("Чем помочь?", keyboard=self._main_menu())]
            case "catalog":
                return self._sections()
            case "root":
                return self._by_root(session, arg)
            case "norms":
                return self._norm_help()
            case "norm_doc":
                return self._explain_norm(session, arg)
            case "norm_items":
                return self._norm_items(session, arg)
            case "card":
                return self._card(session, arg)
            case "similar":
                return self._similar(session, arg)
            case "add":
                return self._add(session, arg, 1)
            case "inc":
                return self._change(session, arg, +1)
            case "dec":
                return self._change(session, arg, -1)
            case "del":
                return self._change(session, arg, 0, remove=True)
            case "card_inc":
                return self._change_on_card(session, arg, +1)
            case "card_dec":
                return self._change_on_card(session, arg, -1)
            case "cart":
                return self._show_cart(session)
            case "cart_page":
                return self._show_cart(session, page=int(arg) if arg.isdigit() else 1)
            case "clear":
                return self._clear_cart(session)
            case "repeat_last_spec":
                return self._repeat_last_spec(session)
            case "restart":
                return self._confirm_restart()
            case "add_all":
                return self._add_all(session)
            case "order_cart":
                return self.order_cart(session, default=int(arg) if arg.isdigit() else None)
            case "order_more":
                return self.order_list(session, "", None, more=True)
            case "export":
                # Файл отдаёт адаптер канала через Core API (`TelegramGateway._export`); сюда нажатие
                # доходит только из каналов, где файлов нет.
                return [Message("Файл со списком пришлю в Telegram-боте.", keyboard=self._main_menu())]
            case "restart_yes":
                return self._restart(session)
            case "manager":
                return self._manager(session)
            case "noop":
                # Надпись с количеством — не кнопка. Telegram всё равно требует
                # у неё действие, поэтому действие есть, а ответа на него нет.
                return []
            case "checkout":
                return self._start_checkout(session)
            case "consent_yes":
                return self._grant_consent(session)
            case "consent_no":
                return [
                    Message(
                        "Без согласия заказ передать менеджеру нельзя. "
                        f"Можно позвонить напрямую: {self.settings.manager_contact}.",
                        keyboard=self._main_menu(),
                    )
                ]
            case "confirm_order":
                return self._submit(session)
            case "order_shown":
                return self._add_shown(session)
            case "order_review":
                return self._add_review(session)
            case "cancel":
                session.checkout_step = None
                session.pending_checkout = False
                return [Message("Оформление отменено, корзина сохранена.", keyboard=self._main_menu())]
            case "more":
                return self._more(session, int(arg or 0))
            case "select_more":
                return self.more_selection(session)
        return [Message("Не понял действие.", keyboard=self._main_menu())]

    # --- Команды -------------------------------------------------------------

    def _handle_command(self, session: Session, text: str) -> list[Response]:
        command = text.split()[0].lower()
        match command:
            case "/start":
                # Разговор уже идёт — сначала подтверждение: /start стирает разговор и корзину, а
                # нажимают его и те, кто просто вернулся в чат (решение заказчика 14.09).
                if session.history:
                    return self._confirm_restart()
                return self._restart(session)
            case "/restart":
                return self._confirm_restart()
            case "/menu":
                return [Message("Чем помочь?", keyboard=self._main_menu())]
            case "/cart":
                return self._show_cart(session)
            case "/order":
                return self._start_checkout(session)
            case "/manager":
                return self._manager()
            case "/my_data":
                return self._export_data(session)
            case "/delete_data":
                return self._delete_data(session)
            case "/help":
                return [Message(HELP, keyboard=self._main_menu())]
            case "/spec":
                return self._spec_history(session)
            case "/preorders":
                return self._preorder_history(session)
        return [Message("Такой команды нет. /help — что умеет бот.", keyboard=self._main_menu())]

    def _spec_history(self, session: Session) -> list[Response]:
        """Спецификации разговора — команда ядра, работает в любом канале (шаг 5.2)."""
        specs = []
        if self.procurement is not None:
            specs = self.procurement.repository.specifications_of(session.user_id)
        if not specs:
            return [Message("Спецификаций пока нет. Соберите корзину и нажмите «Оформить».", keyboard=self._main_menu())]
        lines = ["Ваши спецификации:"]
        lines += [f"• {s.id} — {price_text(s.totals.amount)}, каталог {s.catalog_version}" for s in specs[:10]]
        return [Message("\n".join(lines), keyboard=self._main_menu())]

    def _preorder_history(self, session: Session) -> list[Response]:
        """Предзаказы разговора — команда ядра, работает в любом канале (шаг 5.2)."""
        preorders = self.preorders.of_owner(session.user_id) if self.preorders is not None else []
        if not preorders:
            return [Message("Предзаказов пока нет. Соберите корзину и нажмите «Оформить».", keyboard=self._main_menu())]
        lines = ["Ваши предзаказы:"]
        lines += [f"• {p.id} — {p.status.value}, {price_text(p.totals.amount)}" for p in preorders[:10]]
        return [Message("\n".join(lines), keyboard=self._main_menu())]

    # --- Поиск и карточки -----------------------------------------------------

    def search(
        self, session: Session, text: str, limit: int = PAGE_SIZE, title: str | None = None
    ) -> list[Response]:
        hits = self.index.search(
            SearchQuery(text=text, limit=SEARCH_CAP, audience=session.profile.audience)
        )
        session.last_hits = hits
        if not hits:
            return [
                Message(
                    "Ничего не нашёл по этому запросу. Попробуйте назвать товар иначе "
                    "или указать пункт приказа — например, «1.5.1».\n"
                    f"Если нужно, подключим менеджера: {self.settings.manager_contact}.",
                    keyboard=self._main_menu(),
                )
            ]

        return [
            self._list(
                hits[:limit],
                title or self._result_title(hits, text),
                len(hits),
                offset=0,
                show_norms=self._show_norms(session),
            )
        ]

    def offer(self, session: Session, text: str) -> list[Response]:
        """Ответ, когда модели нет: сеть упала, кончились деньги, ключ не вписан.

        Раньше здесь стоял прямой вызов `search`, и любой текст уходил в каталог.
        1 сентября бот ответил на «привет» списком из пятидесяти товаров, а на
        «а почему на мой привет ты мне товарами отвечаешь?» — ещё пятьюдесятью.
        Поиск перестал быть ответом по умолчанию.

        Что осталось без модели, то и предлагаем честно: список наименований и
        три карточки под ним — по согласованию с заказчиком.
        """
        kind = intent.classify(text)

        # Вопрос о документе отвечается из наших данных и без модели тоже.
        # 01.09 на «по какому приказу оснащается детский сад» бот выдал полсотни
        # случайных товаров: вопрос попал в товарную ветку, и справка, которая
        # лежала рядом, не пригодилась.
        if kind is intent.NORM_QUESTION:
            session.degradation_streak = 0
            # Вопрос о ПУНКТЕ («что за п. 1.13.2.3.9?») — формулировка из справочника
            # без модели (шаг 3.2, ТЗ BUG-29/34): раньше такой вопрос уводил в
            # «по какому документу подбираем?».
            code = intent.norm_code(text)
            if code:
                return self._point_explain(session, text, code)
            doc_id = norm_reference.question_about_document(text)
            if doc_id is None:
                return self._norm_help()
            return self._explain_norm(session, doc_id)

        if kind in (intent.GREETING, intent.SMALL_TALK) or _IDENTITY_QUESTION.search(text):
            session.degradation_streak = 0
            return [self._small_talk(text)]

        # Описание задачи без товара — тоже повод для подбора: консультанта нет, а ядро либо
        # подберёт по учреждению и помещению, либо спросит одно недостающее.
        if kind is intent.TASK and self.procurement_service() is not None:
            return self.select_offer(session, text, "Могу предложить товары из каталога")
        if kind not in (intent.PRODUCT, intent.NORM_CODE):
            # Классификатор класс не узнал — но это не повод для заглушки, если
            # в реплике назван предмет из каталога: «тактильные дорожки 3 шт.
            # срок 4 недели» (прогон 05.10, СЦ2) уводило в «проще обычного»,
            # хотя искать было что. Решает словарь каталога.
            service = self.procurement_service()
            if service is not None and service.names_catalog_item(text):
                return self.select_offer(session, text, "Могу предложить товары из каталога")
            return self._degraded(session)

        if self.procurement_service() is not None:
            return self.select_offer(session, text, "Могу предложить товары из каталога")

        # Без Procurement Core движок живёт только в тестах диалога: там — поиск по индексу.
        session.degradation_streak = 0
        hits = self.index.search(
            SearchQuery(text=text, limit=SEARCH_CAP, audience=session.profile.audience)
        )
        session.last_hits = hits
        if not hits:
            return [
                Message(
                    "Ничего не нашёл по этому запросу. Попробуйте назвать товар иначе "
                    "или указать пункт приказа — например, «1.5.1».\n"
                    f"Если нужно, подключим менеджера: {self.settings.manager_contact}.",
                    keyboard=self._offer_menu(),
                )
            ]

        shown = hits[:OFFER_NAMES]
        names = "\n".join(f"• {hit.product.name}" for hit in shown)
        header = "Могу предложить товары из каталога"
        if len(hits) > len(shown):
            header += f" — вот {len(shown)} из {self._found(hits)}"
        return [
            Message(f"{header}:\n\n{names}", keyboard=self._offer_menu()),
            self._list(
                hits[:PAGE_SIZE],
                "Первые три — подробнее",
                len(hits),
                offset=0,
                show_norms=self._show_norms(session),
            ),
        ]

    def _point_explain(self, session: Session, text: str, code: str) -> list[Response]:
        """Формулировка пункта, путь разделов и счёт позиций — кодом, без модели."""
        from norms.check import check_point, nearest_section

        named = document_ids_in_text(text) or session.profile.norm_doc_ids[:1]
        check = check_point(self.norm_texts, code, named[0] if named else None)
        if not check.exists:
            hint = nearest_section(self.norm_texts, text)
            return [Message(f"{check.note} {hint}".strip(), keyboard=self._offer_menu())]
        if check.in_other_document:
            return [Message(check.note, keyboard=self._offer_menu())]
        item = self.norm_texts.get(check.found_in, code)
        lines = [f"Пункт {code} {_doc_short(check.found_in)}: «{item.title}»." if item is not None else f"Пункт {code} {_doc_short(check.found_in)}."]
        path = self.norm_texts.parents(check.found_in, code)
        if path:
            lines.append("Разделы: " + " → ".join(f"{p.code} {p.title}" for p in path) + ".")
        counted = len(self.norm_texts.children(check.found_in, code))
        if counted:
            lines.append(f"В него входит пунктов: {counted}.")
        doc_name = _doc_short(check.found_in)
        return [
            Message(
                "\n".join(lines)
                + "Показать товары по пункту — нажмите кнопку или назовите предмет.",
                keyboard=Keyboard().row(Button(f"Показать по {doc_name}", "noop")),
            )
        ]

    def _small_talk(self, text: str) -> Message:
        """Приветствие, благодарность и «вы бот?» — разные ответы, а не одна фраза
        на всё (проверка ТЗ BUG-07: «спасибо» → «Здравствуйте!» читалось как баг)."""
        low = (text or "").lower()
        if re.search(r"\bспасибо|благодар", low):
            return Message(
                "Пожалуйста! Если понадобятся ещё позиции — я здесь. "
                f"По счётам и срокам поможет менеджер: {self.settings.manager_contact}.",
                keyboard=self._main_menu(),
            )
        if re.search(r"вы\s+(?:бот|робот)|ты\s+(?:бот|робот)|вы\s+кто|ты\s+кто|человек\s*\?", low):
            return Message(
                "Я Элтик, помощник ЭЛТИ-КУДИЦ: подбираю оборудование из каталога "
                "и комплектации по приказам 1057 и 838. Счёт, скидки и сроки "
                f"называет менеджер: {self.settings.manager_contact}.",
                keyboard=self._main_menu(),
            )
        return Message(
            "Здравствуйте! Я консультант ЭЛТИ-КУДИЦ. Подбираете для "
            "детского сада или для школы?",
            keyboard=self._main_menu(),
        )

    def _degraded(self, session: Session) -> list[Message]:
        """Заглушка без модели — но не дважды подряд (прогон 04.10, BUG-02: клиент
        получал её и на содержательные реплики, из деградации не было выхода)."""
        session.degradation_streak += 1
        if session.degradation_streak >= 2:
            return [
                Message(
                    "Свяжу вас с менеджером: "
                    f"{self.settings.manager_contact}. Оставьте телефон — и менеджер "
                    "перезвонит; пока могу показать каталог или найти позиции по "
                    "пункту перечня, например «1.5.1».",
                    keyboard=self._offer_menu(),
                )
            ]
        return [
            Message(
                "Сейчас я отвечаю проще обычного — консультант временно "
                "недоступен. Могу показать каталог или найти позиции по "
                "пункту перечня, например «1.5.1».\n"
                f"По остальным вопросам — менеджер: {self.settings.manager_contact}.",
                keyboard=self._offer_menu(),
            )
        ]

    def _cart_keyboard(self) -> Keyboard:
        return Keyboard().row(Button("Корзина", "cart"), Button("Оформить", "checkout"))

    def _offered_products(self, session: Session) -> list[Product]:
        """Показанные в разговоре товары в порядке показа, без повторов."""
        products = []
        for sku in dict.fromkeys(session.profile.offered):
            product = self.index.get(sku)
            if product is not None:
                products.append(product)
        return products

    def add_by_text(self, session: Session, text: str) -> list[Response] | None:
        """«Возьму X 4 шт. / добавьте коврики» — корзина словами (шаг 4.2, BUG-04/05).

        Сопоставляем с показанным: назвали однозначно — кладём и отчитываемся;
        несколько кандидатов — карточки с кнопкой «В корзину». Ничего узнаваемого —
        None: реплика уходит в обычный подбор.
        """
        low = (text or "").lower()
        matches = []
        for product in self._offered_products(session):
            words = [w for w in product.name.lower().split() if len(w) >= 5]
            if any(w in low for w in words):
                matches.append(product)
        if not matches:
            return None
        numbers = [int(n) for n in re.findall(r"(\d{1,3})\s*(?:шт|штук|комплект)", low)]
        if not numbers:
            single = re.search(r"\b(\d{1,3})\b", low)
            numbers = [int(single.group(1))] if single and int(single.group(1)) <= 500 else []
        responses: list[Response] = []
        for index, product in enumerate(matches):
            if index < len(numbers):
                quantity = numbers[index]
            elif len(numbers) == 1 and len(matches) == 1:
                quantity = numbers[0]
            else:
                quantity = 1
            responses += self._add(session, product.sku_1c, quantity)
        cart = self.storage.load_cart(session.user_id)
        responses.append(
            Message(f"В корзине {cart.count} шт. на {cart.total} ₽.", keyboard=self._cart_keyboard())
        )
        return responses

    def _collect_list(self, session: Session, text: str) -> list[Response]:
        """«По этому списку подбери все по 1 шт.» — корзина и сводка (шаг 4.3, BUG-03).

        Главный кейс заказчика: раньше фраза без слова «оформить» уходила в отказ
        «это следующий этап». Теперь показанный список кладётся в корзину кодом.
        """
        each = intent.each_quantity(text) or 1
        skus = list(dict.fromkeys(session.profile.offered))
        if not skus:
            return [
                Message(
                    "Список уже ушёл из разговора. Покажите его ещё раз — и я соберу корзину.",
                    keyboard=self._offer_menu(),
                )
            ]
        for sku in skus:
            self._add(session, sku, each)
        cart = self.storage.load_cart(session.user_id)
        return [
            Message(
                f"Собрал по списку: {len(skus)} поз. по {each} шт. "
                f"В корзине {cart.count} шт. на {cart.total} ₽.",
                keyboard=self._cart_keyboard(),
            )
        ]

    def _shown_actions(self, session: Session, text: str) -> list[Response] | None:
        """Вопросы о показанном — ответ кодом, а не новый подбор (шаги 4.5–4.8)."""
        low = (text or "").lower().replace("ё", "е")
        if re.search(r"подробнее|какой код|какая цена|размеры и код", low):
            sku = self._sku_of_reference(session, text, low)
            if sku:
                return self._card(session, sku)
        if re.search(r"сколько стоит (весь |целый |весь\s+)?(комплект|список|заказ|набор)|сколько всего", low):
            return self._total_reply(session)
        if re.search(r"(вы\s+)?добав(ил|или|ите)|где мои позиции|что в корзине|коврики добавили", low):
            return self._show_cart(session)
        if discovery.is_rejection(text) and discovery.objection_of(text) == "price":
            return self._objection_reply(session)
        return None

    def _sku_of_reference(self, session: Session, text: str, low: str) -> str | None:
        """Какой показанный товар назван: «по первой» — первый, иначе по словам названия."""
        offered = list(dict.fromkeys(session.profile.offered))
        if not offered:
            return None
        if re.search(r"по перв|первого|первую|первый", low):
            return offered[0]
        if re.search(r"по втор|второго", low) and len(offered) > 1:
            return offered[1]
        if re.search(r"по трет|третьего", low) and len(offered) > 2:
            return offered[2]
        for sku in offered:
            product = self.index.get(sku)
            if product is None:
                continue
            words = [w for w in product.name.lower().split() if len(w) >= 5]
            if any(w in low for w in words):
                return sku
        return offered[0] if re.search(r"подробнее", low) else None

    def _total_reply(self, session: Session) -> list[Response]:
        """«Сколько стоит весь комплект?» — сумма показанного (ТЗ BUG-09)."""
        priced = [p for p in self._offered_products(session) if p.price is not None]
        if not priced:
            return [
                Message(
                    "Я ничего ещё не показывал — назовите товар или пункт, и я посчитаю.",
                    keyboard=self._offer_menu(),
                )
            ]
        lines = [
            OrderLine(name=p.name, quantity=1, price=p.price, sku_1c=p.sku_1c)
            for p in priced
        ]
        total = sum(p.price for p in priced)
        return [
            OrderSummary(
                lines=lines,
                total=total,
                note="Сумма по показанному; счёт и скидку назовёт менеджер.",
                keyboard=self._offer_menu(),
            )
        ]

    def _objection_reply(self, session: Session) -> list[Response]:
        """Возражение «дорого / у других дешевле» — удержание, а не заглушка (шаг 4.5)."""
        contact = self.settings.manager_contact
        priced = [p for p in self._offered_products(session) if p.price]
        cheaper = sorted(priced, key=lambda p: p.price or 0)[:3]
        head = f"Понимаю. Скидку назовёт менеджер: {contact}."
        if cheaper:
            lines = "\n".join(f"• {p.name} — {p.price} ₽" for p in cheaper)
            body = f"{head}\n\nИз этой же задачи есть дешевле:\n{lines}\n\nМогу разбить на «обязательно сейчас / можно потом» — скажите."
        else:
            body = (
                f"{head}\n\nМогу подобрать дешевле из той же задачи или разбить "
                "на «обязательно сейчас / можно потом» — скажите."
            )
        return [Message(body, keyboard=self._offer_menu())]

    def select_offer(self, session: Session, text: str, header: str = "Подобрал в каталоге") -> list[Response]:
        """Подбор по реплике через Procurement Core.

        Нужен, когда модели нет или когда она пообещала подбор и не сделала его. Слова
        запроса — из самой реплики, остальное ядро берёт из задачи разговора. Реплика не о
        товаре («хорошо, что дальше?») прежний запрос не сбивает.
        """
        about = selection.about_task(text)
        code = intent.norm_code(text) if intent.classify(text) is intent.NORM_CODE else None
        # Содержательный ответ ядра выводит разговор из деградации.
        session.degradation_streak = 0
        profile = session.profile
        # Вопросы о показанном — кодом, до подбора: «подробнее по первой» не должен
        # искать по своим же словам (шаги 4.5–4.8).
        if about and code is None:
            handled = self._shown_actions(session, text)
            if handled is not None:
                return handled
        if code is None and discovery.is_rejection(text):
            # Два «не то» подряд — стоп (прогон 04.10, сц. 5 и 9: логопед 11 раз
            # получил следующую тройку мусора). Дальше — менеджер, а не листание.
            profile.rejection_streak += 1
            if profile.rejection_streak >= 2:
                return [
                    Message(
                        "Похоже, подходящего в каталоге нет. Поможет менеджер: "
                        f"{self.settings.manager_contact}."
                    )
                ]
        else:
            profile.rejection_streak = 0
        # Проверка пункта до выдачи (шаг 3.3): пункта нет нигде — честный отказ,
        # пункт в другом документе — называем где. Никаких товаров вместо ответа.
        if code:
            named = document_ids_in_text(text) or session.profile.norm_doc_ids[:1]
            check = check_point(self.norm_texts, code, named[0] if named else None)
            if not check.exists:
                hint = nearest_section(self.norm_texts, text)
                note = f"{check.note} {hint}".strip()
                return [Message(note, keyboard=self._offer_menu())]
            if check.in_other_document:
                return [Message(check.note, keyboard=self._offer_menu())]
        result = selection.select(
            self,
            session,
            text=text if about else None,
            # Названный код и слова товара работают вместе: «ростомер по разделу 1.12»
            # ищет и по словам, и по разделу — слова больше не стираются (К2.4).
            query=None,
            norm_item=code,
        )
        if code and result is not None and result.items:
            total = result.matched or len(result.items)
            header = f"По пункту {code}: {total} {_positions_word(total)}"
        reply = self._selection_reply(session, result, header)
        # Позиций нет и назван пункт — объясняем про пункт, а не молчаливое «не нашлось»:
        # REVIEW_REQUIRED («пункта нет в документе») сюда тоже попадает.
        if code and result is not None and not result.items:
            if note := self._section_note(result.norm, selection.query_of(text)):
                reply = [*reply, Message(note)]
        return reply

    def _section_note(self, norm, request: str = "") -> str | None:  # noqa: ANN001 — norms.selector NormRequirement
        """Честный ответ о разделе вместо молчаливого «не нашлось» (сц. 9, 12)."""
        code = norm.point
        if not code:
            return None
        asked = f" по запросу «{request}»" if request else ""
        if norm.document:
            item = self.norm_texts.get(norm.document, code)
            if item is not None:
                return (
                    f"В разделе {code} «{item.title}»{asked} товаров нет. "
                    "Назовите другой раздел или товар — или позовём менеджера."
                )
        homes = self.norm_texts.documents_with(code)
        if homes:
            item = self.norm_texts.get(homes[0], code)
            title = f" — «{item.title}»" if item is not None else ""
            name = norm_docs.get(homes[0]).short_name if homes[0] in norm_docs.DOCUMENTS else homes[0]
            return f"Пункта {code} в названном перечне нет; в {name} это{title}."
        return f"Пункта {code} нет ни в одном из разобранных перечней."

    def _selection_reply(
        self, session: Session, result: SelectionResult | None, header: str
    ) -> list[Response]:
        if result is None:
            text = f"Подбор сейчас недоступен. Поможет менеджер: {self.settings.manager_contact}."
        elif result.status is SelectionStatus.NEEDS_DETAILS:
            text = selection.question(result.questions)
        elif not result.items:
            text = (
                "По этой задаче в каталоге ничего не нашлось. Назовите товар иначе или пункт "
                "перечня — например, «1.5.1».\n"
                f"Если нужно, подключим менеджера: {self.settings.manager_contact}."
            )
        else:
            # reason — служебная строка с фильтрами подбора («совпадает с запросом…»),
            # человеку от неё шум; показываем только основание по перечню.
            lines = [
                f"• {item.name}" + (f" — {citation}" if (citation := selection.citation(item)) else "")
                for item in result.items
            ]
            text = "\n".join([f"{header}:", "", *lines, *(["", *selection.notes(result)] if selection.notes(result) else [])])
            session.remember("assistant", text)
            session.profile.remember_offered([item.product_id for item in result.items])
            return [Message(text, keyboard=self._offer_menu()), self._selection_list(session, result)]
        session.remember("assistant", text)
        return [Message(text, keyboard=self._offer_menu())]

    def _selection_list(self, session: Session, result: SelectionResult) -> ProductList:
        """Карточки ровно тех позиций, которые вернул Procurement Core."""
        cards = []
        for item in result.items:
            product = self.index.get(item.product_id)
            if product is None:
                continue
            cards.append(
                ProductCard(
                    product=product,
                    citation=selection.citation(item) or _not_listed(session.profile.audience),
                    keyboard=Keyboard().row(
                        Button("В корзину", f"add:{product.sku_1c}"),
                        Button("Подробнее", f"card:{product.sku_1c}"),
                    ),
                    image=self._image(product),
                    image_path=self.photo_path(product),
                )
            )
        keyboard = Keyboard()
        if result.has_more:
            keyboard.row(Button("Показать ещё", "select_more"))
        keyboard.row(Button("Моя корзина", "cart"), Button("Меню", "menu"))
        return ProductList(title="Подбор из каталога", cards=cards, total_found=result.matched, keyboard=keyboard)

    def shortlist(self, session: Session, text: str, size: int) -> list[Response]:
        """Список из N позиций одним сообщением — строками, с файлом и «Всё в корзину».

        14.09: «подбери из наличия 30 позиций и дай списком» — просьба о перечне, а не о трёх
        карточках (решение заказчика). Позиции — из Procurement Core; если консультант уже
        составил комплектацию, — по её разделу перечня.
        """
        profile = session.profile
        kit = profile.kit or {}
        low = text.lower()
        in_stock = "налич" in low or "со склада" in low
        result = selection.select(
            self,
            session,
            query="",
            norm_item=kit.get("code") or "",
            norm_document=kit.get("document"),
            available_only=in_stock,
            limit=size,
        )
        if result is None or result.status is SelectionStatus.NEEDS_DETAILS or not result.items:
            return self._selection_reply(session, result, "Подобрал в каталоге")

        lines = []
        for number, item in enumerate(result.items, 1):
            product = self.index.get(item.product_id)
            stock = stock_text(product) if product is not None else str(item.availability)
            point = next((mapping.item_code for mapping in item.norm_mappings if mapping.item_code), None)
            lines.append(f"{number}. {item.name} — {price_text(item.price)} — {stock}" + (f" — п. {point}" if point else ""))
        found = len(result.items)
        where = f" по разделу {kit.get('code')} «{kit.get('title')}»" if kit else ""
        head = f"{'В наличии' if in_stock else 'Подобрал'}{where}: {found} {plural(found, 'позиция', 'позиции', 'позиций')}"
        if found < size:
            head += f" из {size} запрошенных — больше {'в наличии ' if in_stock else ''}не нашлось"

        skus = [item.product_id for item in result.items]
        profile.shortlist = skus
        profile.export = "shortlist"
        profile.remember_offered(skus)
        message = "\n".join([f"{head}:", "", *lines])
        session.remember("assistant", message)
        keyboard = exports.buttons()
        keyboard.row(Button("Всё в корзину", "add_all"), Button("Меню", "menu"))
        return [Message(message, keyboard=keyboard)]

    def more_selection(self, session: Session) -> list[Response]:
        """Следующая страница того же подбора — на «а ещё что есть» и на кнопку «Показать ещё»."""
        result = selection.more(self, session)
        if result is None:
            # «Показать ещё» после исчерпания — не тупик и не повтор дословно (сц. 10
            # прогона 04.10): варианты продолжения под рукой (шаги 5.5/7.2).
            return [
                Message(
                    "Других позиций по этой задаче нет. Назовите товар иначе, откройте каталог или позовём менеджера.",
                    keyboard=Keyboard().row(Button("Каталог", "catalog"), Button("Менеджер", "manager")),
                )
            ]
        return self._selection_reply(session, result, "Ещё варианты из каталога")

    def order_list(self, session: Session, text: str, size: int | None, more: bool = False) -> list[Response]:
        """Список по присланному заказу — одним сообщением, с файлом и «Всё в корзину».

        14.09 после файла «подбери из наличия 30 позиций» и «подбери по этому заказу, выведи списком
        то, что есть» бот спрашивал помещение: итог проверки жил только текстом, и «этот заказ» ни на
        что не ссылался. Товары — из проверки заказа (по коду, названию или пункту перечня), цены и
        наличие — из каталога на сейчас.
        """
        profile = session.profile
        order = profile.order or {}
        positions = order.get("positions") or []
        in_stock = order.get("in_stock", False) if more else "налич" in text.lower() or "со склада" in text.lower()
        rows: list[tuple[Product, str | None]] = []
        seen: set[str] = set()
        for position in positions:
            product = self.index.get(position.get("sku") or "")
            if product is None or product.id in seen:
                continue
            if in_stock and product.availability is not Availability.AVAILABLE:
                continue
            seen.add(product.id)
            rows.append((product, position.get("point")))

        start = int(order.get("shown", 0)) if more else 0
        limit = min(size or intent.MAX_LIST, intent.MAX_LIST)
        page = rows[start : start + limit]
        name, where = order.get("file") or "заказ", "в наличии" if in_stock else "в каталоге"
        if not page:
            reply = (
                f"По заказу «{name}» больше позиций {where} нет."
                if more
                else f"По заказу «{name}» {where} не нашлось ни одной из {len(positions)} строк. "
                "Могу передать заказ менеджеру — он подберёт замены."
            )
            session.remember("assistant", reply)
            keyboard = Keyboard().row(Button("Связаться с менеджером", "manager"), Button("Меню", "menu"))
            return [Message(reply, keyboard=keyboard)]

        order.update(shown=start + len(page), in_stock=in_stock)
        head = (
            f"По заказу «{name}» {where}: {len(rows)} {plural(len(rows), 'позиция', 'позиции', 'позиций')} "
            f"из {len(positions)} строк"
        )
        if start:
            head += f", показываю {start + 1}–{start + len(page)}"
        elif size and len(page) < size:
            head += f" — {size} не набралось"
        lines = [
            f"{number}. {product.name} — {price_text(product.price)} — {stock_text(product)}"
            + (f" — п. {point}" if point else "")
            for number, (product, point) in enumerate(page, start + 1)
        ]
        skus = [product.id for product, _ in page]
        profile.shortlist = skus
        profile.remember_offered(skus)
        profile.export = "order"
        message = "\n".join([f"{head}:", "", *lines])
        session.remember("assistant", message)
        keyboard = exports.buttons()
        if start + len(page) < len(rows):
            keyboard.row(Button("Показать ещё", "order_more"))
        keyboard.row(Button("Всё в корзину", "add_all"), Button("Меню", "menu"))
        return [Message(message, keyboard=keyboard)]

    def object_rooms(self, session: Session) -> list[Response] | None:
        """Детский сад целиком: помещения по разделам приказа 1057 и вопрос, с какого начать.

        14.09 на «мы открыли частный детский сад, дай рекомендации по оснащению» консультант выдал
        общий текст про мебель и игрушки без единого пункта перечня. Разделы приказа по помещениям
        известны заранее — их называет код, число товаров — каталог. `None` — перечня 1057 нет.
        """
        doc_id = norm_docs.ORDER_1057.id
        sections = [(code, self.norm_texts.get(doc_id, code)) for code in OBJECT_SECTIONS]
        sections = [(code, item) for code, item in sections if item is not None]
        if not sections:
            return None
        counts: dict[str, int] = {}
        for product in self.index.products:
            if not product.is_active:
                continue
            codes = {ref.item_code for ref in product.norms if ref.doc_id == doc_id and ref.item_code}
            for code, _ in sections:
                if any(point.startswith(f"{code}.") for point in codes):
                    counts[code] = counts.get(code, 0) + 1

        lines = []
        for code, item in sections:
            children = [
                child for child in self.norm_texts.children(doc_id, code) if child.code.count(".") == code.count(".") + 1
            ]
            title = item.title
            ages = [
                re.sub(r"\s*-\s*", "–", match.group(1)) for child in children if (match := _GROUP_AGE.search(child.title))
            ]
            if ages:
                title += f" — отдельно по возрастам: {ages[0]} … {ages[-1]}"
            elif code == "1.13" and children:
                title += ": " + ", ".join(re.sub(r"^Кабинет\s+", "", child.title) for child in children)
            count = counts.get(code, 0)
            suffix = f" (в каталоге {count} {plural(count, 'товар', 'товара', 'товаров')})" if count else ""
            lines.append(f"• {code} {title}{suffix}")
        text = "\n".join(
            [
                "Детский сад по приказу № 1057 оснащают по помещениям — у каждого в перечне свой раздел:",
                "",
                *lines,
                "",
                "С какого помещения начнём? Напишите его — например, «спортзал» или «группа 3–4 лет», — "
                "и я разберу раздел и соберу комплектацию.",
            ]
        )
        session.remember("assistant", text)
        return [Message(text)]

    def _add_all(self, session: Session) -> list[Response]:
        """«Всё в корзину» под списком N позиций: по одной штуке каждой."""
        added, cart = self._add_skus(session, session.profile.shortlist, 1)
        if not added:
            return [Message("Список пуст — сначала подберём позиции.", keyboard=self._main_menu())]
        return [
            Message(
                f"Добавил в корзину {added} {plural(added, 'позицию', 'позиции', 'позиций')}. "
                f"В корзине {cart.count} шт. на {price_text(cart.total)}.",
                keyboard=Keyboard().row(Button("Моя корзина", "cart"), Button("Оформить", "checkout")),
            )
        ]

    def _add_skus(self, session: Session, skus: list[str], quantity: int) -> tuple[int, Cart]:
        """Позиции в корзину по кодам: общая часть «всё в корзину» и «оформить» словами."""
        chosen: list[tuple[Product, int]] = []
        for sku in skus:
            product = self.index.get(sku)
            if product is not None:
                chosen.append((product, quantity))
        return self._add_products(session, chosen)

    def _add_products(
        self, session: Session, chosen: list[tuple[Product, int]],
        norm_codes: dict[str, str] | None = None,
    ) -> tuple[int, Cart]:
        """Товары в корзину, у каждого своё количество.

        Что уже лежит в корзине, не удваивается: «оформить» человек пишет и после того, как
        сам добавил позицию кнопкой.

        `norm_codes` — запрошенный пункт для каждого товара: в основании позиции пишем именно
        его, а не выбор `product.norm_for()` по релевантности. 21.09 песочница, взятая по
        пункту 1.13.3.2.3, уехала в заявку с чужой подписью 1.13.2.3.6 — у товара несколько
        привязок, и релевантность выбрала не ту.
        """
        cart = self.storage.load_cart(session.user_id)
        added = 0
        for product, quantity in chosen:
            if cart.find(product.sku_1c) is not None:
                continue
            norm = None
            if norm_codes and (wanted := norm_codes.get(product.sku_1c)):
                norm = next((ref for ref in product.norms if ref.item_code == wanted), None)
            if norm is None:
                norm = product.norm_for(session.profile.audience, session.profile.room or "")
            cart.add(
                CartItem(
                    sku_1c=product.sku_1c,
                    name=product.name,
                    price=product.price,
                    quantity=max(1, quantity),
                    url=product.url,
                    norm_citation=norm.citation if norm else None,
                    in_stock=product.quantity_available,
                )
            )
            added += 1
        if added:
            self.storage.save_cart(cart)
        return added, cart

    def _points_to_products(
        self, session: Session, points: list[tuple[str, int | None]], each: int | None,
        limit: int = POINTS_TO_CART,
    ) -> tuple[list[tuple[Product, int]], list[str], list[tuple[str, Product]], dict[str, str]]:
        """Пункты перечня — в позиции каталога: по одному товару на пункт, в порядке перечня.

        Пункт — это норма, а не товар: под ним в каталоге бывает несколько позиций. Берём ту,
        что в наличии и дешевле, и говорим человеку, что по пункту взята одна позиция, — иначе
        предзаказ из двадцати пунктов не собрать вовсе. Количество: названное для всех («все по
        1 шт.»), иначе указанное у самого пункта, иначе одна штука.

        Пункт без привязки в каталоге ищется по номеру в названии товара и по формулировке
        приказа (`catalog.points`), но в корзину не попадает: подобранное по формулировке —
        замена без выбора клиента, и 19.09 такая замена увезла в предзаказ игровой лабиринт
        вместо модульного пола. Кандидаты возвращаются отдельно — показываем и спрашиваем.

        Полной комплектации из профиля (`profile.kit`) лимит не нужен: там все позиции раздела,
        их не больше сотни, и человек попросил «по 1 шт. по каждой позиции».

        Возвращает также карту «код 1С → запрошенный пункт»: основание позиции в корзине —
        именно этот пункт, а не выбор по релевантности.
        """
        finder = self.point_finder(session)
        chosen: list[tuple[Product, int]] = []
        missing: list[str] = []
        review: list[tuple[str, Product]] = []
        codes: dict[str, str] = {}
        for code, quantity in points[:limit]:
            found = finder.find(code)
            if found is None:
                missing.append(code)
                continue
            if not found.confirmed:
                review.append((code, found.product))
                continue
            chosen.append((found.product, each or quantity or 1))
            codes[found.product.sku_1c] = code
        return chosen, missing, review, codes

    def point_finder(self, session: Session) -> PointFinder:
        """Поиск товара по пункту перечня для одной операции: словарь названий строится один раз."""
        return PointFinder(
            self.index,
            self.norm_texts,
            tuple(session.profile.norm_doc_ids),
            session.profile.audience,
        )

    def _last_answer(self, session: Session) -> str:
        for message in reversed(session.history):
            if message.get("role") == "assistant":
                return message.get("content") or ""
        return ""

    def checkout_by_intent(self, session: Session, text: str = "") -> list[Response] | None:
        """«Оформить», «сформируй предзаказ» словами: корзина и предзаказ — кодом, а не анкетой модели.

        Ночью 15.09 на «Оформить. Согласен. Организация, контакт, телефон…» бот отвечал анкетой
        «1. Название организации…» или советовал нажать кнопку, которой под сообщением не было:
        за 25 диалогов ни одного предзаказа и ни одной непустой корзины. 16.09 человек перечислил
        комплектацию спортзала пунктами приказа и написал «сформируй предзаказ» — ответом было
        «корзина пуста»: ядро искало показанные карточки, а разговор шёл о пунктах перечня.

        Порядок источников: пункты из самой реплики, построенная комплектация целиком
        (`profile.kit` — она не обрезана, в отличие от последнего ответа бота), показанные
        позиции, пункты из последнего ответа бота.
        """
        cart = self.storage.load_cart(session.user_id)
        if not cart.is_empty:
            return self._start_checkout(session)
        profile = session.profile
        each = intent.each_quantity(text)
        shown = profile.shortlist or profile.offered[-OFFERED_TO_CART:]
        points = intent.listed_points(text)
        kit_points: list[tuple[str, int | None]] = []
        if not points and profile.kit and profile.export == "kit":
            # «По этому списку сформируй предзаказ по 1 шт. по каждой позиции»: полный перечень
            # лежит в профиле, а последний ответ бота обрезан до 2500 знаков, и из него
            # выживают единичные коды — 21.09 комплектация логопеда на 88 позиций собралась
            # восемью.
            kit_points = [
                (position["code"], None)
                for position in profile.kit.get("positions") or []
                if position.get("code")
            ]
        if not points and not kit_points and not shown:
            points = intent.listed_points(self._last_answer(session))
        if points or kit_points:
            source = kit_points or points
            chosen, missing, review, codes = self._points_to_products(
                session, source, each, limit=len(source) if kit_points else POINTS_TO_CART
            )
            profile.review = [product.sku_1c for _, product in review]
            if not chosen and not review:
                answer = self._nothing_to_checkout(missing)
                session.remember("assistant", answer)
                return [Message(answer, keyboard=self._offer_menu())]
            self._remember(session)
            lines: list[str] = []
            keyboard = Keyboard()
            if chosen:
                added, cart = self._add_products(session, chosen, norm_codes=codes)
                lines.append(
                    f"Собрал корзину по перечню: {added} "
                    f"{plural(added, 'позиция', 'позиции', 'позиций')}"
                    + (f" из {len(source)}" if len(missing) + len(review) else "")
                    + ". В корзине "
                    f"{cart.count} шт. на {price_text(cart.total)}."
                )
                keyboard.row(Button("Моя корзина", "cart"), Button("Оформить", "checkout"))
            if review:
                shown_review = review[:REVIEW_TO_SHOW]
                candidates = "; ".join(f"«{product.name}» (п. {code})" for code, product in shown_review)
                if len(review) > len(shown_review):
                    candidates += f" и ещё {len(review) - len(shown_review)}"
                lines.append(
                    ("Точной привязки в каталоге нет у: " if chosen else "По формулировке приказа подобралось: ")
                    + candidates
                    + ". В корзину их не ставил — это замена без вашего выбора. Подходят?"
                )
                keyboard.row(Button("Добавить подобранное", "order_review"))
            if missing:
                lines.append(_missing_points(missing))
            # Текст зовёт нажать «Оформить» только когда кнопка реально прикреплена:
            # без выбранного текст ссылался на кнопку, которой под сообщением нет.
            if chosen:
                lines.append(
                    "Проверьте состав и нажмите «Оформить» — дальше спрошу организацию, контакт и телефон. "
                    "Если по какому-то пункту нужен другой вариант, назовите его номер."
                )
            else:
                lines.append(
                    "Подходят ли подобранные замены — подтвердите, и продолжим оформление: "
                    "спрошу организацию, контакт и телефон."
                )
            answer = " ".join(lines)
            session.remember("assistant", answer)
            return [Message(answer, keyboard=keyboard)]
        if not shown:
            answer = self._nothing_to_checkout([])
            session.remember("assistant", answer)
            return [Message(answer, keyboard=self._offer_menu())]
        # Показанные позиции сами в корзину не падают: «оформить» под чужим списком
        # собирало предзаказ из того, что бот показывал часом ранее. Сначала — что
        # именно будет положено, добавление и оформление — отдельным нажатием.
        names = "; ".join(
            f"«{product.name}»" for sku in shown if (product := self.index.get(sku)) is not None
        )
        answer = (
            "В корзине пока пусто. По слову «оформить» готов добавить показанные позиции: "
            f"{names}. Добавить и продолжить оформление?"
        )
        session.remember("assistant", answer)
        return [
            Message(
                answer,
                keyboard=Keyboard().row(
                    Button("Добавить и оформить", "order_shown"), Button("Меню", "menu")
                ),
            )
        ]

    def _add_shown(self, session: Session) -> list[Response]:
        """Кнопка «Добавить и оформить»: показанные позиции — в корзину по явному согласию."""
        profile = session.profile
        shown = profile.shortlist or profile.offered[-OFFERED_TO_CART:]
        added, _ = self._add_skus(session, shown, 1)
        if not added:
            return [Message("Добавлять нечего — список показанных пуст.", keyboard=self._main_menu())]
        return self._start_checkout(session)

    def _add_review(self, session: Session) -> list[Response]:
        """Кнопка «Добавить подобранное»: кандидаты по формулировке — только после явного «да»."""
        skus = session.profile.review
        added, cart = self._add_skus(session, skus, 1)
        session.profile.review = []
        self._remember(session)
        if not added:
            return [Message("Список для добавления пуст — подберём заново.", keyboard=self._main_menu())]
        return [
            Message(
                f"Добавил {added} {plural(added, 'позицию', 'позиции', 'позиций')} по формулировке "
                f"приказа — проверьте их особенно внимательно. В корзине {cart.count} шт. на "
                f"{price_text(cart.total)}.",
                keyboard=Keyboard().row(Button("Моя корзина", "cart"), Button("Оформить", "checkout")),
            )
        ]

    def _nothing_to_checkout(self, missing: list[str]) -> str:
        """Оформлять нечего: молчать нельзя, но и общая отговорка не годится, если пункты названы."""
        if missing:
            return (
                f"По этим пунктам в каталоге позиций нет: {_points_line(missing)}. "
                f"Назовите товар словами — подберу замену, или подключим менеджера: "
                f"{self.settings.manager_contact}."
            )
        return (
            "Оформлять пока нечего: корзина пуста. Скажите, для какого помещения подбираем "
            "или назовите пункт перечня — соберу список, и оформим его одним нажатием."
        )

    def order_cart(self, session: Session, default: int | None = None, override: int | None = None) -> list[Response]:
        """Найденные позиции присланного заказа — в корзину, без модели.

        15.09 на «сформируй предзаказ» и «все найденные позиции по 1 шт.» модель заново искала строки
        файла по названиям — 14 из 15, потом 4 из 15, — подменила набор зондов товаром с той же ценой и
        закончила «нажмите «Оформить»» без кнопки при пустой корзине. Позиции берутся из проверки
        заказа, количество — из файла, названное человеком (`override`) или кнопкой «по 1 шт.» (`default`).
        """
        profile = session.profile
        order = profile.order or {}
        positions = order.get("positions") or []
        name = order.get("file") or "заказ"
        found: list[tuple[Product, int | None]] = []
        seen: set[str] = set()
        resolved = 0
        for position in positions:
            product = self.index.get(position.get("sku") or "")
            if product is None:
                continue
            # Строки и товары считаются раздельно: превью проверки файла говорит
            # «найдено N строк», и «положил M позиций» по уникальным товарам не должно
            # выглядеть противоречащим (19.09: «найдено 58» рядом с «положил 57»).
            resolved += 1
            if product.id in seen:
                continue
            seen.add(product.id)
            found.append((product, _quantity(position.get("quantity"))))
        missing = len(positions) - resolved

        keyboard = Keyboard()
        if not found:
            text = f"По заказу «{name}» в каталоге не нашлось ни одной позиции — класть в корзину нечего."
            keyboard.row(Button("Связаться с менеджером", "manager"), Button("Меню", "menu"))
        elif override is None and default is None:
            unknown = sum(1 for _, quantity in found if quantity is None)
            if unknown:
                text = (
                    f"По заказу «{name}» в каталоге {resolved} из {len(positions)} строк, но у {unknown} "
                    f"{plural(unknown, 'позиции', 'позиций', 'позиций')} не указано количество. Нажмите "
                    "«Найденные в корзину по 1 шт.» или напишите, например, «все по 2»."
                )
                keyboard.row(Button("Найденные в корзину по 1 шт.", "order_cart:1"), Button("Меню", "menu"))
            else:
                # Количество есть везде — предзаказ по файлу целиком: не найденные строки менеджер увидит сам.
                text = (
                    f"По заказу «{name}» всё готово к предзаказу: в каталоге {resolved} из {len(positions)} "
                    "строк, количество — из файла. Нажмите «Оформить предзаказ»: строки, которых нет в "
                    "каталоге, менеджер проверит сам."
                )
                keyboard.row(Button("Оформить предзаказ", f"po_order:{order.get('id')}"), Button("Меню", "menu"))
        else:
            cart = self.storage.load_cart(session.user_id)
            for product, quantity in found:
                count = override or quantity or default or 1
                if cart.find(product.sku_1c) is not None:
                    # Повторное нажатие не удваивает количество.
                    cart.set_quantity(product.sku_1c, count)
                    continue
                norm = product.norm_for(profile.audience, profile.room or "")
                cart.add(
                    CartItem(
                        sku_1c=product.sku_1c,
                        name=product.name,
                        price=product.price,
                        quantity=count,
                        url=product.url,
                        norm_citation=norm.citation if norm else None,
                        in_stock=product.quantity_available,
                    )
                )
            self.storage.save_cart(cart)
            how = f"по {override} шт." if override else "количество — из файла" + (f", где его нет — {default} шт." if default else "")
            text = (
                f"Положил в корзину {len(found)} {plural(len(found), 'позицию', 'позиции', 'позиций')} из заказа "
                f"«{name}» ({resolved} из {len(positions)} строк найдено), {how} "
                f"В корзине {cart.count} шт. на {price_text(cart.total)}."
            )
            if missing:
                text += (
                    f"\nНе нашлось в каталоге: {missing} {plural(missing, 'строка', 'строки', 'строк')} — в корзину "
                    "не попали; их подберёт менеджер."
                )
            keyboard.row(Button("Моя корзина", "cart"), Button("Оформить", "checkout"))
        session.remember("assistant", text)
        return [Message(text, keyboard=keyboard)]

    def note(self, user_id: str, channel: str, text: str, order: dict | None = None) -> None:
        """Ответ, сыгранный мимо диалога, — в историю разговора: итог проверки присланного файла.

        Проверенный заказ — ещё и в профиль: на «подбери по этому заказу» список строится по нему.
        """
        with self.runtime.turn():
            session = self.session(user_id, channel)
            self._sync(session)
            session.remember("assistant", text)
            if order is not None:
                session.profile.remember_order(order)
            self._remember(session)

    def _offer_menu(self) -> Keyboard:
        return Keyboard().row(Button("Каталог", "catalog"), *self._manager_row())

    def _manager_row(self) -> list[Button]:
        # Единая функция (решение заказчика 21.09): одна кнопка «Связаться с менеджером».
        # Онлайн-чат открывается по нажатию (см. `_manager`), а не отдельной кнопкой —
        # рядом с ней он только путал.
        return [Button("Связаться с менеджером", "manager")]

    def _more(self, session: Session, offset: int) -> list[Response]:
        hits = session.last_hits
        chunk = hits[offset : offset + PAGE_SIZE]
        if not chunk:
            # «Показать ещё» после исчерпания — не тупик и не повтор дословно (сц. 10
            # прогона 04.10): варианты продолжения под рукой (шаги 5.5/7.2).
            return [
                Message(
                    "Других позиций по этой задаче нет. Назовите товар иначе, откройте каталог или позовём менеджера.",
                    keyboard=Keyboard().row(Button("Каталог", "catalog"), Button("Менеджер", "manager")),
                )
            ]
        return [
            self._list(
                chunk,
                "Ещё варианты",
                len(hits),
                offset=offset,
                show_norms=self._show_norms(session),
            )
        ]

    def _show_norms(self, session: Session) -> bool:
        """Показывать ли нормативные основания этому клиенту.

        Вопрос 10 опросного листа (текст совпадает у обоих отделов): частному
        лицу — только польза товара, без приказов и пунктов. Тот, кто сам
        спросил про документ, отвечает по делу — гейт его вопросов не глотает.
        """
        return session.profile.client_kind != "person"

    def _list(
        self, hits: list[SearchHit], title: str, total: int, offset: int, show_norms: bool = True
    ) -> ProductList:
        cards = [
            ProductCard(
                product=hit.product,
                # Пустая строка основания читается как «мы не проверяли». В
                # подробной карточке об этом сказано давно, а в выдаче позиция
                # без привязки молчала — и стояла вперемешку с обоснованными.
                citation=(hit.citation() or _not_listed(hit.audience)) if show_norms else None,
                keyboard=Keyboard().row(
                    Button("В корзину", f"add:{hit.product.sku_1c}"),
                    Button("Подробнее", f"card:{hit.product.sku_1c}"),
                ),
                # Снимок теперь есть и в выдаче. Раньше его не показывали, чтобы
                # не ходить на сайт заказчика пять раз за один ответ; сейчас файлы
                # лежат у нас, а позиций в выдаче три, а не пять.
                image=self._image(hit.product),
                image_path=self.photo_path(hit.product),
            )
            for hit in hits
        ]
        keyboard = Keyboard()
        if offset + len(hits) < total:
            keyboard.row(Button("Показать ещё", f"more:{offset + len(hits)}"))
        keyboard.row(Button("Моя корзина", "cart"), Button("Меню", "menu"))
        return ProductList(title=title, cards=cards, total_found=total, keyboard=keyboard)

    def _card(self, session: Session, sku: str, replace: bool = False) -> list[Response]:
        product = self.index.get(sku)
        if product is None:
            return [Message("Не нашёл такой товар.", keyboard=self._main_menu())]

        cart_item = self.storage.load_cart(session.user_id).find(sku)
        keyboard = Keyboard()
        if cart_item:
            keyboard.row(
                Button("−", f"card_dec:{sku}"),
                # Количество — надпись, а не кнопка: раньше нажатие на неё
                # присылало ту же карточку заново, и в чате копились дубли.
                Button(f"{cart_item.quantity} шт.", "noop"),
                Button("+", f"card_inc:{sku}"),
            )
        else:
            keyboard.row(Button("В корзину", f"add:{sku}"))
        # Вопрос 5 опросного листа (оба отдела): рядом с покупкой — «Похожее» и
        # менеджер. У товара без цены менеджерская кнопка зовётся уточнением цены.
        if product.price is None:
            keyboard.row(
                Button("Похожее", f"similar:{sku}"), Button("Уточнить цену у менеджера", "manager")
            )
        else:
            keyboard.row(Button("Похожее", f"similar:{sku}"), Button("Позвать менеджера", "manager"))
        if product.url:
            # Действие «noop», а не «card:»: кнопка — ссылка, повторное нажатие
            # не должно рисовать карточку заново (ТЗ BUG-17, шаг 7.5).
            keyboard.row(Button("Открыть на сайте", "noop", url=product.url))
        keyboard.row(Button("Моя корзина", "cart"), Button("Меню", "menu"))

        audience = session.profile.audience
        # Частнику (вопрос 10 опросного листа) оснований не показываем: только
        # польза товара. Явный вопрос о документе гейт обходит — он выше.
        person = not self._show_norms(session)
        norm = None if person else product.norm_for(audience, session.profile.room or "")
        # Предложение Бабковой (вопрос 5): не все читают кнопки — словами говорим,
        # что оформить можно самому, а менеджер рядом для смет и документов. Один
        # раз за разговор: повторное «Подробнее» не приносит подсказку снова.
        hint: Message | None = None
        if not session.profile.card_hint_shown:
            session.profile.card_hint_shown = True
            hint = Message(
                "Заказ можно оформить самому — кнопками под карточкой. Если нужно рассчитать "
                "доставку, подготовить документы или подобрать ещё что-то к кабинету — позовём менеджера."
            )
        # Карточка первой: она заменяет прежнюю по кнопке, подсказка — следом.
        return [
            ProductCard(
                product=product,
                quantity=cart_item.quantity if cart_item else 0,
                citation=norm.citation if norm else None,
                keyboard=keyboard,
                image=self._image(product),
                image_path=self.photo_path(product),
                norms=[] if person else self.norm_lines(product, audience),
                replace=replace,
            ),
            *([hint] if hint else []),
        ]

    def _similar(self, session: Session, sku: str) -> list[Response]:
        """«Похожее» — товары того же раздела каталога (вопрос 5 опросного листа)."""
        product = self.index.get(sku)
        if product is None:
            return [Message("Не нашёл такой товар.", keyboard=self._main_menu())]
        root = product.roots[0] if product.roots else None
        hits = self.index.search(
            SearchQuery(
                text=product.name,
                root=root,
                limit=PAGE_SIZE + 1,
                audience=session.profile.audience,
            )
        )
        hits = [hit for hit in hits if hit.product.sku_1c != sku][:PAGE_SIZE]
        if not hits:
            return [
                Message(
                    "Похожего в этом разделе не нашлось. Подбор под задачу возьмёт на себя "
                    "менеджер.",
                    keyboard=Keyboard().row(
                        Button("Связаться с менеджером", "manager"), Button("Меню", "menu")
                    ),
                )
            ]
        session.last_hits = hits
        return [
            self._list(
                hits,
                f"Похожее: {product.name}",
                len(hits),
                offset=0,
                show_norms=self._show_norms(session),
            )
        ]

    def norm_lines(self, product: Product, audience: str | None) -> list[str]:
        """Все основания товара с формулировками пунктов приказа.

        Раньше в карточке стоял голый номер — «позиция 2.4.35». Что за ним, было
        не узнать, не открыв приказ на полутора сотнях страниц. Теперь рядом стоит
        строка из самого документа.

        Чужой перечень сюда не попадает: школьный пункт не обосновывает закупку
        для детского сада, и показывать его человеку из сада — это ровно та
        путаница, на которую жаловался заказчик.
        """
        lines: list[str] = []
        for ref in product.norms_for(audience):
            item = self.norm_texts.get(ref.doc_id, ref.item_code or "")
            title = item.title if item else ref.item_title
            line = ref.citation
            if title:
                line += f" — {title}"
            if item and item.section:
                line += f" ({item.section})"
            lines.append(line)

        # Молчание об основании читается как «мы не проверяли». Почти половина
        # каталога к перечням не привязана, и человеку, который собирает закупку,
        # честный ответ нужнее пустого места.
        if not lines:
            note = _not_listed(audience)
            if note:
                lines.append(note)
        return lines

    def photo_path(self, product: Product) -> str | None:
        """Снимок, лежащий у нас на диске. Сеть здесь не трогаем.

        Telegram не может забрать картинку с vdm.ru сам — отвечает «failed to get
        HTTP URL content». Поэтому файл для него важнее адреса.

        Скачивать файл прямо здесь бот перестал 20.09: `PhotoStore` ходит на сайт
        тем же `PageFetcher`, и на медленной странице «Подробнее» подвисала на
        десятки секунд — вместе со всеми, кто в это время писал боту. Недостающий
        файл забирает фоновый сборщик (`media/prefetch.py`), снимок появляется
        со следующего показа.
        """
        if self.media is None:
            return None
        try:
            path = self.media.local_photo(product)
        except Exception as exc:  # фото не должно ломать ответ
            log.warning("Локальное фото для %s не найдено: %s", product.sku_1c, exc)
            return None
        if not path:
            self.media.want(product)
        return path

    def _image(self, product: Product) -> str | None:
        """Адрес снимка из уже собранного. За новым идёт фоновый сборщик."""
        # Собранная база знаний уже содержит снимки — тогда на сайт идти незачем.
        if product.images:
            return product.images[0]
        if self.media is None:
            return None
        try:
            return self.media.main_image(product, allow_fetch=False)
        except Exception as exc:  # фото не должно ломать ответ
            log.warning("Фото для %s не получено: %s", product.sku_1c, exc)
            return None

    def _found(self, hits: list[SearchHit]) -> str:
        """«24 позиции» или «более 50 позиций».

        Выдача ограничена сверху, поэтому ровно на пределе честнее сказать «более»:
        иначе бот сообщает как точное число размер собственной выборки.
        """
        if len(hits) >= SEARCH_CAP:
            return f"более {SEARCH_CAP} позиций"
        return f"{len(hits)} {plural(len(hits), 'позиция', 'позиции', 'позиций')}"

    def _result_title(self, hits: list[SearchHit], text: str) -> str:
        count = self._found(hits)
        if hits and hits[0].by_norm:
            code = hits[0].matched_code
            return f"По пункту {code}: {count}" if code else f"По перечню: {count}"
        return f"Нашлось {count} по запросу «{text}»"

    # --- Разделы --------------------------------------------------------------

    @property
    def roots(self) -> list[str]:
        """Корневые разделы каталога в порядке появления в выгрузке — из состояния версии."""
        return list(self.runtime.state.roots)

    def _sections(self) -> list[Response]:
        keyboard = Keyboard()
        # В кнопку кладём номер раздела, а не название: Telegram ограничивает
        # callback_data 64 байтами, а «ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838»
        # в кириллице занимает вдвое больше — такие кнопки молча пропадали.
        for number, root in enumerate(self.roots):
            # С большой буквы только первое слово: «Оборудование для детского сада»,
            # а не «Оборудование Для Детского Сада» (ТЗ BUG-20, шаг 7.4).
            keyboard.row(Button(root[:1].upper() + root[1:], f"root:{number}"))
        keyboard.row(Button("Подбор по приказу", "norms"), Button("Меню", "menu"))
        return [Message("Выберите раздел каталога:", keyboard=keyboard)]

    def _by_root(self, session: Session, arg: str) -> list[Response]:
        root = self._root_by(arg)
        if root is None:
            return [Message("Такого раздела нет.", keyboard=self._main_menu())]
        return self._consult_root(session, root)

    def _consult_root(self, session: Session, root: str) -> list[Response]:
        """Раздел каталога — начало консультации, а не выдача.

        14.09 на «Оборудование для детского сада» бот сразу выложил первые позиции раздела:
        игру «Мирознайка» и настольные игры — не спросив, что за группа и зачем. Заказчик:
        при выборе раздела сначала выяснить задачу, карточки — после. Дальше разговор ведут
        консультант и продавец, подбор — через Procurement Core.
        """
        profile = session.profile
        profile.update_from_text(root)
        # Раздел каталога начинает консультацию: следующий ответ человека продолжает её.
        profile.last_agent = "consult"
        if not profile.institution:
            ask = "Для детского сада или для школы подбираете?"
        elif not profile.room:
            ask = "Для какого помещения или зоны — группа, спортивный или музыкальный зал, кабинет специалиста?"
        elif not profile.age:
            ask = "Для какого возраста или класса?"
        else:
            # Всё ключевое уже известно — не переспрашивать возраст, который человек
            # называл раньше (23.09, сц. 4 и 14: «5 лет» спрашивали заново).
            ask = "Что подбираем в первую очередь?"
        text = f"Раздел «{root.title()}». Помогу выбрать из него то, что нужно под вашу задачу. {ask}"
        session.remember("assistant", text)
        return [Message(text, keyboard=Keyboard().row(Button("Меню", "menu")))]

    def _root_by(self, arg: str) -> str | None:
        """Номер раздела или его название.

        Название понимаем ради кнопок, нажатых в старых сообщениях: в чате они
        остаются рабочими и после обновления бота.
        """
        if arg.isdigit():
            number = int(arg)
            return self.roots[number] if number < len(self.roots) else None
        return arg if arg in self.roots else None

    def _norm_help(self) -> list[Response]:
        keyboard = Keyboard()
        for doc_id in norm_reference.known_documents():
            keyboard.row(Button(norm_docs.get(doc_id).short_name, f"norm_doc:{doc_id}"))
        keyboard.row(Button("Меню", "menu"))
        return [
            Message(
                "По какому документу подбираем? Нажмите — объясню, что это за документ "
                "и что по нему есть в каталоге.\n\n"
                "Можно и сразу номером пункта: «2.20.63», «п. 1.5.1» или подраздел «2.4».",
                keyboard=keyboard,
            )
        ]

    def _explain_norm(self, session: Session, doc_id: str) -> list[Response]:
        """Справка по документу — без обращения к модели.

        Нормативный вопрос обязан работать всегда, в том числе когда провайдер
        недоступен: именно на нём отказ модели заметнее всего, а ответ полностью
        собирается из наших данных.
        """
        text = norm_reference.explain(
            doc_id,
            norm_reference.coverage(self.index, doc_id, self.norm_texts.count(doc_id)),
        )
        if not text:
            return [Message("По этому документу справки пока нет.", keyboard=self._main_menu())]
        # Справка не назначает документ, по которому идёт закупка: человек
        # спросил, что это такое, а не сказал «оснащаю по нему». Иначе один
        # вопрос про 838 переводил в школьный режим весь остаток разговора.
        if doc_id not in session.profile.asked_about_docs:
            session.profile.asked_about_docs.append(doc_id)
        session.remember("assistant", text)

        keyboard = Keyboard().row(Button("Показать позиции", f"norm_items:{doc_id}"))
        keyboard.row(Button("Другой документ", "norms"), Button("Меню", "menu"))
        return [Message(text, keyboard=keyboard)]

    def _norm_items(self, session: Session, doc_id: str) -> list[Response]:
        if doc_id not in norm_docs.DOCUMENTS:
            return [Message("Такого документа нет.", keyboard=self._main_menu())]
        # Аудиторию берём у самого документа: если человек смотрит перечень для
        # школ, обосновывать позиции садовским приказом бессмысленно.
        subject = norm_docs.get(doc_id).subject
        audience = subject if subject in {"school", "preschool"} else session.profile.audience
        hits = self.index.search(
            SearchQuery(text="", norm_doc_id=doc_id, limit=SEARCH_CAP, audience=audience)
        )
        if not hits:
            products = [
                p for p in self.index.products if any(r.doc_id == doc_id for r in p.norms)
            ][:SEARCH_CAP]
            hits = [SearchHit(p, 0.0, "text", None, audience) for p in products]
        if not hits:
            return [
                Message(
                    "К этому документу в каталоге позиции не привязаны.",
                    keyboard=self._main_menu(),
                )
            ]
        session.last_hits = hits
        title = f"{norm_docs.get(doc_id).short_name}: {len(hits)} позиций"
        head: list[Response] = []
        if doc_id == "order_838" and session.profile.audience == "preschool":
            # Садовод нажал «Показать позиции» на школьном перечне — предупреждаем,
            # что это перечень для школ (ТЗ BUG-19), а не подмена выдачи.
            head = [Message("Это перечень для школ — позиции обоснованы приказом № 838.")]
        return [*head, self._list(hits[:PAGE_SIZE], title, len(hits), offset=0)]

    # --- Корзина ---------------------------------------------------------------

    def _add(self, session: Session, sku: str, quantity: int) -> list[Response]:
        product = self.index.get(sku)
        if product is None:
            return [Message("Не нашёл такой товар.", keyboard=self._main_menu())]

        cart = self.storage.load_cart(session.user_id)
        norm = product.norm_for(session.profile.audience, session.profile.room or "")
        cart.add(
            CartItem(
                sku_1c=product.sku_1c,
                name=product.name,
                price=product.price,
                quantity=quantity,
                url=product.url,
                norm_citation=norm.citation if norm else None,
                in_stock=product.quantity_available,
            )
        )
        self.storage.save_cart(cart)
        return [
            Message(
                f"«{product.name}» добавлен. В корзине {cart.count} шт. "
                f"на {price_text(cart.total)}.",
                keyboard=Keyboard().row(
                    Button("Моя корзина", "cart"),
                    Button("Оформить", "checkout"),
                ),
            )
        ]

    def _change(self, session: Session, sku: str, delta: int, remove: bool = False) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        item = cart.find(sku)
        if item is None:
            return self._show_cart(session, replace=True)
        cart.set_quantity(sku, 0 if remove else item.quantity + delta)
        self.storage.save_cart(cart)
        return self._show_cart(session, replace=True)

    def _change_on_card(self, session: Session, sku: str, delta: int) -> list[Response]:
        """Количество меняют прямо в карточке товара — её же и обновляем.

        Отдельные действия от корзинных не ради красоты: ответ должен заменить то
        сообщение, под которым нажали, а это разные сообщения.
        """
        cart = self.storage.load_cart(session.user_id)
        item = cart.find(sku)
        if item is not None:
            cart.set_quantity(sku, item.quantity + delta)
            self.storage.save_cart(cart)
        return self._card(session, sku, replace=True)

    def _show_cart(self, session: Session, replace: bool = False, page: int = 1) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        if cart.is_empty:
            return [self._empty_cart(session, replace)]

        # Название товара живёт в тексте, а не в кнопке. В кнопку Telegram влезает
        # десятка два символов, и заказчик видел «1 × Сенсом...» вместо позиции.
        # Кнопки теперь короткие и пронумерованы так же, как строки списка.
        # Полная комплектация — это до сотни строк: в превью помещаются первые
        # (лимит сообщения в Telegram — 4096 знаков), весь состав уходит менеджеру
        # и в файле.
        # Постраничные кнопки корзины (шаг 7.3): 20 строк × 4 кнопки — 80 колбэков,
        # Telegram такую клавиатуру обрезает. Страница — CART_PAGE_LINES строк.
        page_size = CART_PAGE_LINES
        total_pages = max(1, -(-len(cart.items) // page_size))
        page = max(0, min(page - 1, total_pages - 1))
        window = cart.items[page * page_size : (page + 1) * page_size]
        keyboard = Keyboard()
        for offset, item in enumerate(window, 1):
            number = page * page_size + offset
            keyboard.row(
                Button(f"{number} −", f"dec:{item.sku_1c}"),
                Button(f"{number}: {item.quantity} шт.", "noop"),
                Button(f"{number} +", f"inc:{item.sku_1c}"),
                Button(f"{number} ✕", f"del:{item.sku_1c}"),
            )
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(Button("←", f"cart_page:{page}"))
            nav.append(Button(f"Стр. {page + 1} из {total_pages}", "noop"))
            if page < total_pages - 1:
                nav.append(Button("→", f"cart_page:{page + 2}"))
            keyboard.row(*nav)
        keyboard.row(Button("Оформить заказ", "checkout"), Button("Очистить", "clear"))

        notes = []
        if len(cart.items) > len(cart.items[:CART_PREVIEW_LINES]):
            hidden = len(cart.items) - CART_PREVIEW_LINES
            notes.append(
                f"… и ещё {hidden} {plural(hidden, 'позиция', 'позиции', 'позиций')} — "
                "полный состав уйдёт менеджеру и в файле."
            )
        if total_pages > 1:
            notes.append("Кнопки количеством и удалением — постранично.")
        if any(item.price is None for item in cart.items):
            notes.append("По части позиций цена уточняется — менеджер пришлёт её при подтверждении.")
        note = " ".join(notes) or None
        return [
            OrderSummary(
                lines=[
                    OrderLine(
                        name=item.name,
                        quantity=item.quantity,
                        price=item.price,
                        sku_1c=item.sku_1c,
                        norm_citation=item.norm_citation,
                    )
                    # Текст превью — до CART_PREVIEW_LINES строк (ТЗ 4.7), кнопки — постранично.
                    for item in cart.items[:CART_PREVIEW_LINES]
                ],
                total=cart.total,
                note=note,
                keyboard=keyboard,
                replace=replace,
            )
        ]

    def _clear_cart(self, session: Session) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        cart.clear()
        self.storage.save_cart(cart)
        return [Message("Корзина очищена.", keyboard=self._main_menu())]

    def _empty_cart(self, session: Session, replace: bool = False) -> Message:
        """Пустая корзина — не тупик: была спецификация, предложим собрать её заново.

        Корзины каналов разделены (сайт и Telegram — разные пользователи), и «покажи
        корзину» после собранной в другом канале заявки отвечало тупым «пуста».
        """
        specification = self._last_specification(session)
        if specification is not None:
            return Message(
                f"Корзина пуста. Последняя спецификация {specification.id} — "
                f"{price_text(specification.totals.amount)}. Собрать её заново?",
                keyboard=Keyboard().row(
                    Button("Повторить заказ", "repeat_last_spec"),
                    Button("Меню", "menu"),
                ),
                replace=replace,
            )
        return Message("Корзина пуста.", keyboard=self._main_menu(), replace=replace)

    def _last_specification(self, session: Session):
        """Последняя спецификация пользователя; `None` — ядра закупки нет или пусто."""
        service = self.procurement_service()
        if service is None:
            return None
        try:
            found = service.repository.specifications_of(session.user_id, limit=1)
        except Exception as exc:  # повтор заказа не стоит поломки ответа
            log.warning("Спецификации %s не прочитаны: %s", session.user_id, exc)
            return None
        return found[0] if found else None

    def _repeat_last_spec(self, session: Session) -> list[Response]:
        """Спецификация → корзина: повтор прежнего заказа одним нажатием."""
        specification = self._last_specification(session)
        if specification is None:
            return [Message("Прежних спецификаций нет — скажите, что подобрать.", keyboard=self._main_menu())]
        chosen = []
        for item in specification.items:
            product = self.index.get(item.product_id)
            if product is not None:
                chosen.append((product, item.quantity))
        if chosen:
            self._add_products(session, chosen)
        return self._show_cart(session)

    # --- Оформление -------------------------------------------------------------

    def _start_checkout(self, session: Session) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        if cart.is_empty:
            return [Message("Сначала добавьте товары в корзину.", keyboard=self._main_menu())]

        if self.storage.active_consent(session.user_id) is None:
            session.pending_checkout = True
            return [
                Message(
                    CONSENT_TEXT,
                    keyboard=Keyboard().row(
                        Button("Согласен", "consent_yes"),
                        Button("Отказаться", "consent_no"),
                    ),
                )
            ]
        return self._ask_contact(session, step=0)

    def _grant_consent(self, session: Session) -> list[Response]:
        self.storage.record_consent(
            session.user_id, session.channel, CONSENT_VERSION, "granted"
        )
        if session.pending_checkout:
            session.pending_checkout = False
            return self._ask_contact(session, step=0)
        if session.pending_lead:
            # Лид ждал согласия: контакты уже в сессии — заявка уходит сейчас.
            if session.customer.phone:
                return self._submit_lead(session, "", session.customer.phone)
            return self._ask_lead_contact(session, session.lead_essence)
        return [Message("Согласие записано.", keyboard=self._main_menu())]

    def _ask_contact(self, session: Session, step: int) -> list[Response]:
        session.checkout_step = step
        _, question = CHECKOUT_FIELDS[step]
        return [
            Message(
                f"Шаг {step + 1} из {len(CHECKOUT_FIELDS)}. {question}",
                keyboard=Keyboard().row(Button("Отменить", "cancel")),
            )
        ]

    def _collect_contact(self, session: Session, text: str) -> list[Response]:
        # «Позовите человека» посреди анкеты — передача менеджеру, а не имя
        # контакта в заявке (ТЗ BUG-02, шаг 4.1).
        if intent.asks_manager(text):
            session.checkout_step = None
            session.pending_checkout = False
            return self._manager(session)
        step = session.checkout_step or 0
        field_name, _ = CHECKOUT_FIELDS[step]
        value = "" if text.strip() in {"-", "—", "нет"} else text.strip()
        if value and _CONTACT_NOISE.match(value):
            # Кнопки и меню данными не становятся — переспрашиваем тот же шаг.
            return self._ask_contact(session, step)
        if value and intent.asks_about_order(value):
            # Вопрос посреди анкеты («а можно доставку в другой регион?») названием
            # организации не становится: шаг переспрашивается, реплика — на совести
            # консультанта, а анкета не портится.
            return [
                Message(
                    "Сначала закончим оформление — или нажмите «Отменить», и я отвечу на вопрос.",
                    keyboard=Keyboard().row(Button("Отменить", "cancel")),
                ),
                *self._ask_contact(session, step),
            ]
        if field_name == "phone" and value and not _CONTACT_PHONE.search(value):
            return [
                Message(
                    "Не похоже на телефон. Напишите номер цифрами — например +7 900 123-45-67 — "
                    "или «-», если телефона нет.",
                    keyboard=Keyboard().row(Button("Отменить", "cancel")),
                )
            ]
        setattr(session.customer, field_name, value)

        if step + 1 < len(CHECKOUT_FIELDS):
            return self._ask_contact(session, step + 1)

        session.checkout_step = None
        if not session.customer.is_complete:
            session.customer = Customer()
            return [
                Message(
                    "Нужны хотя бы имя и телефон (или e-mail), иначе менеджер не сможет "
                    "с вами связаться. Начнём заново?",
                    keyboard=Keyboard().row(
                        Button("Заполнить заново", "checkout"), Button("Меню", "menu")
                    ),
                )
            ]

        cart = self.storage.load_cart(session.user_id)
        customer = session.customer
        summary = (
            f"Проверьте заказ:\n\n"
            f"Организация: {customer.organization or '—'}\n"
            f"Контакт: {customer.name}, {customer.phone or customer.email}\n"
            f"Регион: {customer.region or '—'}\n"
            f"Позиций: {cart.count} на {price_text(cart.total)}"
        )
        return [
            Message(
                summary,
                keyboard=Keyboard().row(
                    Button("Отправить менеджеру", "confirm_order"),
                    Button("Отменить", "cancel"),
                ),
            )
        ]

    def _submit(self, session: Session) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        # Комплектация из перечня уходит менеджеру целиком: в корзине только позиции с
        # товарами, а менеджеру нужен весь список приказа, чтобы подобрать остальное.
        extras: list[tuple[str, bytes]] = []
        if session.profile.kit:
            file = exports.build(self, session, exports.EXCEL)
            if file is not None:
                extras.append((file.filename, file.content))
        try:
            order = self.orders.submit(cart, session.customer, session.channel, extras=extras or None)
        except PermissionError:
            return self._start_checkout(session)
        except ValueError as exc:
            return [Message(str(exc), keyboard=self._main_menu())]

        session.customer = Customer()
        test = session.user_id in self.settings.qa_user_ids
        lines = [
            order_accepted(
                order.id,
                order.total,
                delivered=order.status == "sent" and not test,
                test=test,
            )
        ]
        note = delivery_note(order.total, self.settings.min_delivery_rub, self.settings.delivery_url)
        if note:
            lines.append(note)
        lines.append(f"Связаться напрямую: {self.settings.manager_contact}")
        return [Message("\n".join(lines), keyboard=self._main_menu())]

    # --- Права субъекта ПДн ------------------------------------------------------

    def _export_data(self, session: Session) -> list[Response]:
        data = self.storage.export_user_data(session.user_id)
        if not data["orders"] and not data["consents"] and not data["cart"]:
            return [Message("По вам не хранится никаких данных.", keyboard=self._main_menu())]
        lines = [
            "Что о вас хранится:",
            f"• позиций в корзине: {len(data['cart'])}",
            f"• заказов: {len(data['orders'])}",
            f"• записей о согласии: {len(data['consents'])}",
            f"• сохранённых разговоров: {len(data['dialogs'])} "
            "(переписка хранится обезличенной — имена и телефоны заменены метками, "
            "срок хранения 30 дней)",
            "",
            "Удалить всё и отозвать согласие — /delete_data",
        ]
        return [Message("\n".join(lines), keyboard=self._main_menu())]

    def _delete_data(self, session: Session) -> list[Response]:
        self.storage.delete_user_data(session.user_id, session.channel)
        session.customer = Customer()
        session.last_hits = []
        # Переписка и профиль стираются и в памяти процесса: удалить их только
        # в базе означало бы, что бот всё ещё помнит разговор.
        session.forget()
        return [
            Message(
                "Данные удалены, согласие отозвано. Переписка и то, что я о задаче "
                "запомнил, тоже стёрты. Переданные ранее заказы обезличены: контакты "
                "удалены, позиции остались у менеджера для учёта.",
                keyboard=self._main_menu(),
            )
        ]

    # --- Общее -------------------------------------------------------------------

    def _main_menu(self) -> Keyboard:
        """Кнопки под сообщением — только то, чего нет в меню команд.

        Корзина, оформление, помощь и «начать заново» переехали в командное меню
        Telegram: постоянные четыре кнопки под каждым ответом загромождали окно
        диалога, а нажать их всё равно можно было только у последнего сообщения.

        «Связаться с менеджером» — исключение, и стоит отдельной строкой. Заказчик
        просил не спрашивать контакты в начале разговора, но дать возможность
        оставить их в любой момент; в виджете командного меню нет, и без этой
        кнопки уйти к человеку было неоткуда. Онлайн-чат — по нажатию этой кнопки
        (`_manager`), отдельной кнопки у него больше нет.
        """
        return Keyboard().row(
            Button("Каталог", "catalog"),
            Button("Подбор по приказу", "norms"),
            Button("Начать заново", "restart"),
        ).row(*self._manager_row())

    def _confirm_restart(self) -> list[Response]:
        return [
            Message(
                "Начать заново? Я забуду, что мы обсуждали, и очищу корзину.",
                keyboard=Keyboard().row(
                    Button("Да, начать заново", "restart_yes"),
                    Button("Отмена", "menu"),
                ),
            )
        ]

    def _restart(self, session: Session) -> list[Response]:
        """Чистый лист: ни разговора, ни профиля, ни корзины.

        Пользователи не догадывались, что для нового подбора нужно звать /start,
        и продолжали прежний разговор — бот помнил старую задачу и подмешивал её
        в новую. Незаконченное оформление сбрасываем тоже: чужие контакты в чужой
        заявке хуже, чем лишний вопрос.
        """
        cart = self.storage.load_cart(session.user_id)
        cart.clear()
        self.storage.save_cart(cart)
        session.checkout_step = None
        session.pending_checkout = False
        session.pending_lead = False
        session.lead_essence = ""
        session.lead_stage = ""
        session.customer = Customer()
        session.last_hits = []
        session.forget()
        self._remember(session)
        return [Message(GREETING, keyboard=self._main_menu())]

    def _manager(self, session: Session | None = None) -> list[Response]:
        """Контакты менеджера — и путь к заявке, если корзина уже собрана.

        Ночью 16.09 пять диалогов кончились этим сообщением: человек нажимал кнопку,
        получал телефон и уходил, а заявки с составом корзины менеджер не видел.
        Подсказка «/order» текстом в Telegram не нажимается — теперь это кнопка.

        Единая функция (решение заказчика 21.09): кто предпочитает переписку, уходит
        в онлайн-чат заказчика одной кнопкой здесь же, без отдельной кнопки в меню.
        """
        cart = self.storage.load_cart(session.user_id).count if session is not None else 0
        keyboard = self._main_menu()
        if self.settings.support_chat_url:
            keyboard.row(Button("Написать в онлайн-чате", "noop", url=self.settings.support_chat_url))
        if cart:
            keyboard.row(Button("Оформить заявку", "checkout"))
        return [
            Message(
                "Менеджер ЭЛТИ-КУДИЦ ответит на вопросы по срокам, документам и "
                "нестандартной комплектации.\n\n"
                f"{self.settings.manager_contact}\n\n"
                + (
                    f"В корзине {cart} {plural(cart, 'позиция', 'позиции', 'позиций')} — нажмите "
                    "«Оформить заявку», оставьте имя и телефон, "
                    "и менеджер получит её со всеми позициями и основаниями."
                    if cart
                    else "Соберём корзину — и менеджер получит заявку со всеми позициями и "
                    "основаниями. Скажите, что подобрать."
                ),
                keyboard=keyboard,
            )
        ]

    # --- Лид без корзины (шаг 4.4) ----------------------------------------------

    def _lead_flow(self, session: Session, text: str) -> list[Response] | None:
        """«Нужен счёт» без единого товара — заявка менеджеру, а не потерянный клиент.

        Возвращает `None`, когда реплика лида не касается: есть корзина (работает
        чекаут), человек передумал и заговорил о товарах, либо лида в реплике нет.
        Данные собираем по одному вопросу — имя, город, телефон (опросный лист,
        вопросы 1 и 14: оба отдела просили минимум, «одним сообщением» — нет).
        """
        if not self.storage.load_cart(session.user_id).is_empty:
            session.pending_lead = False
            session.lead_stage = ""
            return None
        phone = PHONE.search(text or "")
        if session.pending_lead:
            if phone:
                # Телефон пришёл раньше своей очереди — им и закрываем заявку.
                return self._submit_lead(session, session.lead_essence, phone.group(0))
            if intent.asks_lead(text):
                return self._ask_lead_contact(session, text)
            # Человек заговорил о другом: подбираем товары, сбор контактов
            # снимается — иначе бот молчал бы вопросом вместо ответов.
            moved_on = intent.classify(text) not in (intent.OTHER,)
            if moved_on:
                session.pending_lead = False
                session.lead_stage = ""
                return None
            if session.lead_stage == "name":
                name = _lead_name(text)
                if name:
                    session.customer.name = name
                    session.lead_stage = "city"
                    return [
                        Message(
                            f"Спасибо, {name}! Из какого вы города? "
                            "Менеджер сразу подскажет сроки доставки.",
                            keyboard=self._main_menu(),
                        )
                    ]
                return [
                    Message(
                        "Напишите, как к вам обращаться — достаточно имени.",
                        keyboard=self._main_menu(),
                    )
                ]
            if session.lead_stage == "city":
                region = " ".join((text or "").split()).strip(" .;,!-")[:100]
                if region:
                    session.customer.region = region
                session.lead_stage = "phone"
                ack = f"Записал: {region}. " if region else ""
                return [
                    Message(
                        f"{ack}И последний шаг — телефон, чтобы менеджер мог перезвонить. "
                        "Например: +7 916 123-45-67.",
                        keyboard=self._main_menu(),
                    )
                ]
            if session.lead_stage == "phone":
                return [
                    Message(
                        "Не нашёл номер в сообщении. Напишите телефон в любом виде, "
                        "например: +7 916 123-45-67.",
                        keyboard=self._main_menu(),
                    )
                ]
            # Стадия потерялась (сессия пережила обновление) — спрашиваем с начала.
            return self._ask_lead_contact(session, session.lead_essence)
        if intent.asks_lead(text):
            if phone:
                return self._submit_lead(session, text, phone.group(0), name_from_text=True)
            if self._has_export_context(session):
                # «Пришлите счёт в Excel» по показанной комплектации — это файл и
                # счёт-нота из обычного потока, а не лид: контакты человек давать
                # не обязан (прогон 0923, сц. «Excel + счёт + срок»).
                session.pending_lead = False
                return None
            return self._ask_lead_contact(session, text)
        return None

    def _has_export_context(self, session: Session) -> bool:
        """Из разговора есть что выслать: комплектация, файл заказа или показанный выбор."""
        profile = session.profile
        return bool(profile.kit or profile.order or profile.offered)

    def _ask_lead_contact(self, session: Session, essence: str) -> list[Response]:
        session.pending_lead = True
        session.lead_essence = (essence or session.lead_essence)[:300]
        if session.customer.phone:
            # Контакты уже известны из прошлого хода: переспрашивать нечестно.
            return self._submit_lead(session, essence, session.customer.phone)
        # По одному вопросу за ход: имя → город → телефон.
        if session.customer.name:
            session.lead_stage = "city" if not session.customer.region else "phone"
        else:
            session.lead_stage = "name"
        questions = {
            "name": "Передам менеджеру — счёт, КП и реквизиты у него. Как вас зовут?",
            "city": "Из какого вы города? Менеджер сразу подскажет сроки доставки.",
            "phone": "Остался телефон, чтобы менеджер мог перезвонить. "
            "Например: +7 916 123-45-67.",
        }
        return [Message(questions[session.lead_stage], keyboard=self._main_menu())]

    def start_lead(self, user_id: str, channel: str, customer: Customer | None = None, essence: str = "") -> list[Response]:
        """Контакт без корзины из канала (кнопка «Отправить контакт», шлюз) — тот же лид.

        Канал отдаёт имя и телефон готовыми: остаётся согласие (если его ещё нет)
        и сама заявка. Согласие спрашиваем, а не ставим молча — это персональные
        данные; ответ на «Согласен» доводит заявку до менеджера.
        """
        session = self.session(user_id, channel)
        if customer is not None:
            session.customer = Customer(
                name=customer.name or session.customer.name, phone=customer.phone or session.customer.phone
            )
        if essence:
            session.lead_essence = essence[:300]
        if self.storage.active_consent(session.user_id) is None:
            session.pending_lead = True
            return [
                Message(
                    CONSENT_TEXT,
                    keyboard=Keyboard().row(
                        Button("Согласен", "consent_yes"),
                        Button("Отказаться", "consent_no"),
                    ),
                )
            ]
        if session.customer.phone:
            return self._submit_lead(session, session.lead_essence, session.customer.phone)
        session.pending_lead = True
        return self._ask_lead_contact(session, session.lead_essence)

    def _submit_lead(
        self, session: Session, essence: str, phone: str, *, name_from_text: bool = False
    ) -> list[Response]:
        if not session.customer.name:
            if name_from_text:
                # Реплика-триггер несёт имя: «перезвоните по номеру …, Мария».
                parsed = " ".join(PHONE.sub(" ", essence or "").replace(",", " ").split()).strip(" .;—-")
                session.customer.name = (parsed or "Клиент")[:200]
            else:
                # На шагах сбора essence — просьба «нужен счёт», ей имями не становятся.
                session.customer.name = "Клиент"
        session.customer.phone = phone[:50]
        if essence and not session.lead_essence:
            session.lead_essence = essence[:300]
        try:
            order = self.orders.submit_lead(
                session.user_id, session.channel, self._lead_customer(session)
            )
        except PermissionError:
            # Согласия нет: контакты сохранены в сессии, «Согласен» доведёт заявку.
            session.pending_lead = True
            return [
                Message(
                    CONSENT_TEXT,
                    keyboard=Keyboard().row(
                        Button("Согласен", "consent_yes"),
                        Button("Отказаться", "consent_no"),
                    ),
                )
            ]
        except ValueError as exc:
            return [Message(str(exc), keyboard=self._main_menu())]
        session.customer = Customer()
        session.pending_lead = False
        session.lead_essence = ""
        session.lead_stage = ""
        test = session.user_id in self.settings.qa_user_ids
        lines = [
            lead_accepted(order.id, delivered=order.status == "sent" and not test, test=test),
            f"Связаться напрямую: {self.settings.manager_contact}",
        ]
        return [Message("\n".join(lines), keyboard=self._main_menu())]

    def _lead_customer(self, session: Session) -> Customer:
        """Контакты лида + суть запроса и след диалога — то, что увидит менеджер.

        Резюме просил отдел ассортимента (вопрос 2: «телефон + короткое резюме»),
        Бабкова добавила переписку (вопрос 14: «информацию о переписке»). История
        маскируется при записи, поэтому наружу персональные данные не уходят.
        """
        essence = session.lead_essence or "Запрос без состава"
        kit = session.profile.kit
        if kit:
            title = kit.get("title") or ""
            essence += f"; интересовала комплектация {kit.get('code')} «{title}»"
        comment = f"Запрос без состава: {essence}"
        digest = self._dialog_digest(session)
        if digest:
            comment += f"\nПоследнее из диалога:\n{digest}"
        return Customer(
            name=session.customer.name,
            phone=session.customer.phone,
            email=session.customer.email,
            organization=session.customer.organization,
            region=session.customer.region,
            comment=comment[:2000],
        )

    def _dialog_digest(self, session: Session, limit: int = 10, width: int = 120) -> str:
        """Последние реплики клиента одной колонкой — менеджеру в заявку."""
        lines = []
        for turn in session.history:
            if turn.get("role") == "user":
                chunk = " ".join(str(turn.get("content", "")).split())[:width]
                if chunk:
                    lines.append(f"— {chunk}")
        return "\n".join(lines[-limit:])


def _quantity(value: object) -> int | None:
    """Количество строки заказа целым числом; нет или не число — `None`."""
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return int(number) if number >= 1 else None


# Ответы, после которых имени не бывает: «да»/«хорошо» — это согласие, а не имя.
_TINY_ANSWERS = {"да", "ок", "нет", "окей", "хорошо", "ладно", "ага", "угу"}
_LEAD_NAME_NOISE = re.compile(r"\bменя зовут\b|\bзовут\b|\bэто\b", re.IGNORECASE)


def _lead_name(text: str) -> str:
    """Имя из ответа на шаге сбора: чистим служебные слова, остальное — имя."""
    cleaned = " ".join((text or "").replace(",", " ").split())
    cleaned = _LEAD_NAME_NOISE.sub(" ", cleaned).strip(" .;!«»\"—-")
    if cleaned.lower() in _TINY_ANSWERS:
        return ""
    return cleaned[:200]


def describe(product: Product) -> str:
    """Короткое описание товара одной строкой — для списков и виджета."""
    parts = [product.name, price_text(product.price), stock_text(product)]
    norm = product.best_norm()
    if norm:
        parts.append(norm.citation)
    return " · ".join(parts)


def _points_line(codes: list[str]) -> str:
    shown = ", ".join(codes[:10])
    return shown if len(codes) <= 10 else f"{shown} и ещё {len(codes) - 10}"


def _missing_points(missing: list[str]) -> str:
    return f"Без позиций остались пункты {_points_line(missing)} — в каталоге по ним ничего нет."
