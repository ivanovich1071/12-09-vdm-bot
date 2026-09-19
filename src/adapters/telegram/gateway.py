"""Telegram → Core API. Адаптер ничего не решает сам.

Он переводит события Telegram в вызовы `CoreApi` и отдаёт ответы рендеру
(`adapters/telegram/bot.py`). Товары, цены, нормативы, сопоставление, спецификацию
и предзаказ считает ядро: здесь нет ни каталога, ни базы, ни правил продажи — это
проверяет тест импортов.

Своё у адаптера — только то, что есть лишь в Telegram: файл-вложение, кнопка
«Отправить контакт» и ожидание контакта между двумя сообщениями. Ожидание живёт в
памяти процесса: контакты не хранятся нигде, пока их не передали менеджеру.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from core.errors import DomainError
from core.ui import (
    Button,
    Keyboard,
    Message,
    Response,
    delivery_note,
    order_accepted,
    plural,
    price_text,
)
from core_api import dto
from core_api.facade import CoreApi
from core_api.sessions import CoreSession
from privacy.consent import CONSENT_TEXT
from privacy.masking import PHONE

log = logging.getLogger(__name__)

CHANNEL = "telegram"
PROBLEM_LINES = 10
# Сколько позиций будущего предзаказа показываем в сообщении: остальное — в файле.
PREVIEW_LINES = 12
# Надписи постоянной клавиатуры и «Меню»: пока бот ждёт контакт, они сбрасывают ожидание, а не читаются как имя.
LEAVES_CONTACT = frozenset(
    {"начать заново", "каталог", "моя корзина", "корзина", "менеджер", "связаться с менеджером", "меню", "отмена"}
)
# Надпись кнопки, которая сама передаёт номер. Если она пришла текстом, номера в ней нет,
# и «Не вижу телефона» человек читает как отказ: ночью 16.09 это повторилось 21 раз.
CONTACT_BUTTON = frozenset({"отправить контакт", "поделиться контактом", "отправить номер"})
# Вопрос или длинная фраза — человек говорит о другом, а не диктует имя (сц. 10: «когда
# менеджер свяжется?» получило в ответ «Не вижу телефона»). Имя с телефоном — короткая
# строка без вопроса, так что разбирать реплику по смыслу шлюзу не нужно.
_ASKING_WORDS = ("когда", "сколько", "почему", "зачем", "какой", "какие")
NAME_WORDS = 4
MATCHED = frozenset({"MATCHED_EXACT", "MATCHED_HIGH", "MATCHED_REVIEW"})

EVALUATION_LABELS = {
    "READY": "всё сошлось — можно передавать менеджеру",
    "READY_WITH_WARNINGS": "можно передавать, но есть предупреждения",
    "REVIEW_REQUIRED": "часть позиций проверит менеджер",
    "REJECTED": "позиции не найдены в каталоге — передать нельзя",
}
NOTICE_LABELS = {
    "NOT_FOUND": "не найдено в каталоге",
    "AMBIGUOUS": "подходит несколько товаров",
    "MATCH_REVIEW": "сопоставление проверит менеджер",
    "QUANTITY_UNKNOWN": "не указано количество",
    "QUANTITY_INVALID": "количество не распознано",
    "PRICE_CHANGED": "цена изменилась",
    "PRICE_NOT_FOUND": "нет цены в каталоге",
    "NOT_AVAILABLE": "нет в наличии",
    "UNKNOWN_STOCK": "наличие неизвестно",
    "INSUFFICIENT_STOCK": "в наличии меньше",
    "NORM_MISMATCH": "не тот пункт перечня",
    "NORM_REVIEW": "норматив проверит менеджер",
    "NORM_UNKNOWN": "нет данных о нормативе",
}


@dataclass
class FileReply:
    """Файл в чат: спецификация Excel или Word."""

    filename: str
    content: bytes
    caption: str = ""
    keyboard: Keyboard | None = None


@dataclass
class ContactRequest:
    """Просьба прислать контакт кнопкой Telegram."""

    text: str


TelegramReply = Response | FileReply | ContactRequest


class TelegramGateway:
    def __init__(self, core: CoreApi, max_upload_bytes: int) -> None:
        self.core = core
        self.max_upload_bytes = max_upload_bytes
        self._sessions: dict[str, CoreSession] = {}
        self._awaiting_contact: dict[str, str] = {}
        self._contact_misses: dict[str, int] = {}

    @property
    def storage(self):  # noqa: ANN201 — кэш file_id снимков у рендера
        return self.core.storage

    def session(self, user_id: str) -> CoreSession:
        cached = self._sessions.get(user_id)
        if cached is None:
            opened = self.core.open_session(channel=CHANNEL, user_ref=user_id, trusted=True)
            cached = self._sessions[user_id] = self.core.session(opened.session_id or "")
        return cached

    # --- События Telegram ------------------------------------------------------

    def text(self, user_id: str, text: str) -> list[TelegramReply]:
        stripped = (text or "").strip()
        command = stripped.split()[0].lower() if stripped.startswith("/") else ""
        if user_id in self._awaiting_contact and not command:
            if stripped.lower() in LEAVES_CONTACT:
                # «Начать заново» и постоянная клавиатура — не контакт. Ночью 14.09 тестовый клиент не мог
                # сбросить разговор, и следующий сценарий начался с «Не вижу телефона».
                self._forget_contact(user_id)
            elif PHONE.search(stripped):
                return self._guard(lambda: self._contact_from_text(user_id, stripped))
            elif stripped.lower() in CONTACT_BUTTON and not self._contact_misses.get(user_id):
                # Кнопка нажата, но номер не пришёл: у клиента запрещена передача контакта.
                self._contact_misses[user_id] = 1
                return [ContactRequest(_CONTACT_BY_HAND)]
            elif not _about_something_else(stripped) and not self._contact_misses.get(user_id):
                return self._guard(lambda: self._contact_from_text(user_id, stripped))
            else:
                # Второй раз без телефона — человек говорит о другом: отвечаем на реплику, предзаказ ждёт.
                preorder_id = self._awaiting_contact.get(user_id)
                self._forget_contact(user_id)
                note = Message(
                    f"Предзаказ {preorder_id} сохранён, но без телефона менеджеру не ушёл. Передать его можно "
                    "позже: «Оформить» ещё раз и имя с телефоном одним сообщением."
                )
                return [note, *self.core.message_primitives(self.session(user_id), text)]
        if command == "/order":
            return self._guard(lambda: self._checkout(user_id))
        if command == "/spec":
            return self._guard(lambda: self._cart_specification(user_id))
        if command == "/preorders":
            return self._guard(lambda: self._history(user_id))
        if command in ("/start", "/delete_data"):
            self._forget_contact(user_id)
        replies = self.core.message_primitives(self.session(user_id), text)
        if command == "/delete_data":
            self._sessions.pop(user_id, None)
        return list(replies)

    def action(self, user_id: str, data: str) -> list[TelegramReply]:
        verb, _, arg = data.partition(":")
        handlers: dict[str, Callable[[], list[TelegramReply]]] = {
            # «Оформить» под ответами ядра — в предзаказ, а не в прежнюю анкету из шести шагов.
            "checkout": lambda: self._checkout(user_id),
            "po_order": lambda: self._preorder(user_id, "uploaded_order", arg),
            "po_spec": lambda: self._preorder(user_id, "specification", arg),
            "po_consent": lambda: self._consent(user_id, arg),
            "spec_xlsx": lambda: self._spec_file(user_id, arg, "xlsx"),
            "spec_docx": lambda: self._spec_file(user_id, arg, "docx"),
            # Комплектация или список разговора файлом — по кнопкам «Скачать Excel» и «Скачать Word».
            "export": lambda: self._export(user_id, arg),
        }
        if verb in handlers:
            return self._guard(handlers[verb])
        if verb == "restart_yes":
            # Начали заново — недособранный предзаказ контакта больше не ждёт.
            self._forget_contact(user_id)
        return list(self.core.action_primitives(self.session(user_id), data))

    def upload(self, user_id: str, filename: str, content: bytes) -> list[TelegramReply]:
        return self._guard(lambda: self._upload(user_id, filename, content))

    def contact(self, user_id: str, name: str, phone: str) -> list[TelegramReply]:
        return self._guard(lambda: self._send_preorder(user_id, name, phone))

    # --- Сценарии ----------------------------------------------------------------

    def _upload(self, user_id: str, filename: str, content: bytes) -> list[TelegramReply]:
        session = self.session(user_id)
        order = self.core.upload_order(session, filename, content, self.core.order_context(session)).data
        assert isinstance(order, dto.OrderOut)
        if order.status == "FAILED":
            return [Message(f"Файл «{order.source_file['filename']}» не удалось прочитать: {order.error}")]
        evaluation = self.core.evaluate_order(session, order.id).data
        assert isinstance(evaluation, dto.EvaluationOut)
        keyboard = Keyboard()
        text = evaluation_text(order, evaluation)
        if evaluation.status != "REJECTED":
            matched = [item for item in evaluation.items if item.get("match_status") in MATCHED]
            if all(item.get("quantity") is not None for item in matched):
                # Количество есть у всех найденных строк — это уже предзаказ: показываем состав
                # с ценами, чтобы человек согласовал его до того, как отдаст контакт.
                text += preorder_preview(matched)
                keyboard.row(Button("Оформить предзаказ", f"po_order:{order.id}"))
            else:
                # 15.09 свой же файл «Подобранные позиции» без количества ушёл менеджеру предзаказом на 0 ₽.
                keyboard.row(Button("Найденные в корзину по 1 шт.", "order_cart:1"))
                text += (
                    "\n\nКоличество указано не у всех позиций. Нажмите «Найденные в корзину по 1 шт.» "
                    "или напишите, например, «все по 2»."
                )
        keyboard.row(Button("Меню", "menu"))
        if not evaluation.summary.get("checked") and order.warnings:
            # «Позиций 0» без причины читается как «ничего нет в каталоге» (14.09): причина — первой.
            text = f"{order.warnings[0].message}\n\n{text}"
        # Итог проверки — в разговор: иначе «подбери по этому заказу» ни к чему не привязано.
        self.core.note_dialog(session, text, order, evaluation)
        return [Message(text, keyboard=keyboard)]

    def _export(self, user_id: str, fmt: str) -> list[TelegramReply]:
        file = self.core.export_dialog_list(self.session(user_id), "docx" if fmt == "docx" else "xlsx")
        if file is None:
            return [
                Message(
                    "Сохранять пока нечего: сначала соберём комплектацию или подберём позиции.",
                    keyboard=Keyboard().row(Button("Меню", "menu")),
                )
            ]
        return [FileReply(file.filename, file.content, file.caption)]

    def _cart_specification(self, user_id: str) -> list[TelegramReply]:
        session = self.session(user_id)
        spec = self.core.cart_specification(session).data
        assert isinstance(spec, dto.SpecificationOut)
        return [self._file(session, spec, "xlsx")]

    def _checkout(self, user_id: str) -> list[TelegramReply]:
        """«Оформить» и /order: спецификацию и предзаказ собирает ядро, канал просит согласие и контакт."""
        session = self.session(user_id)
        spec_result, preorder_result = self.core.checkout(session)
        spec, preorder = spec_result.data, preorder_result.data
        assert isinstance(spec, dto.SpecificationOut) and isinstance(preorder, dto.PreorderOut)
        return [self._file(session, spec, "xlsx", preorder_button=False), *self._offer_preorder(user_id, preorder)]

    def _spec_file(self, user_id: str, spec_id: str, fmt: str) -> list[TelegramReply]:
        session = self.session(user_id)
        spec = self.core.get_specification(session, spec_id).data
        assert isinstance(spec, dto.SpecificationOut)
        return [self._file(session, spec, fmt)]

    def _file(
        self, session: CoreSession, spec: dto.SpecificationOut, fmt: str, preorder_button: bool = True
    ) -> FileReply:
        document, _, _ = self.core.export_specification(session, spec.id, fmt)
        totals = spec.totals
        caption = (
            f"Спецификация {spec.id}: позиций {totals['positions']}, на {price_text(totals['amount'])}"
            + ("" if totals["complete"] else f" (без цены: {totals['missing_prices']})")
            + f". Цены — по версии каталога {spec.catalog_version}."
        )
        keyboard = Keyboard()
        if preorder_button:
            keyboard.row(Button("Оформить предзаказ", f"po_spec:{spec.id}"))
        keyboard.row(Button("Excel", f"spec_xlsx:{spec.id}"), Button("Word", f"spec_docx:{spec.id}"))
        return FileReply(document.filename, document.content, caption, keyboard)

    def _preorder(self, user_id: str, source: str, source_id: str) -> list[TelegramReply]:
        session = self.session(user_id)
        preorder = self.core.create_preorder(session, source, source_id, None).data
        assert isinstance(preorder, dto.PreorderOut)
        return self._offer_preorder(user_id, preorder)

    def _offer_preorder(self, user_id: str, preorder: dto.PreorderOut) -> list[TelegramReply]:
        session = self.session(user_id)
        summary = (
            f"Предварительный заказ {preorder.id}: позиций {preorder.totals['positions']} "
            f"на {price_text(preorder.totals['amount'])} по текущим ценам. Это ещё не заказ: "
            "наличие, срок и окончательную цену подтвердит менеджер."
        )
        if not self.core.get_session(session).data.consent.active:  # type: ignore[attr-defined]
            keyboard = Keyboard().row(
                Button("Согласен", f"po_consent:{preorder.id}"), Button("Отказаться", "menu")
            )
            return [Message(summary), Message(CONSENT_TEXT, keyboard=keyboard)]
        self._awaiting_contact[user_id] = preorder.id
        return [Message(summary), ContactRequest(_ASK_CONTACT)]

    def _consent(self, user_id: str, preorder_id: str) -> list[TelegramReply]:
        self.core.consent(self.session(user_id), True)
        self._awaiting_contact[user_id] = preorder_id
        return [ContactRequest(_ASK_CONTACT)]

    def _contact_from_text(self, user_id: str, text: str) -> list[TelegramReply]:
        match = PHONE.search(text)
        if match is None:
            self._contact_misses[user_id] = self._contact_misses.get(user_id, 0) + 1
            return [ContactRequest("Не вижу телефона. " + _ASK_CONTACT)]
        name = " ".join(PHONE.sub(" ", text).replace(",", " ").split()).strip(" .;—-") or "Клиент"
        return self._send_preorder(user_id, name, match.group(0).strip())

    def _send_preorder(self, user_id: str, name: str, phone: str) -> list[TelegramReply]:
        preorder_id = self._awaiting_contact.pop(user_id, None)
        self._contact_misses.pop(user_id, None)
        if preorder_id is None:
            return [Message("Контакт получен, но предзаказ не выбран. Соберите его заново: /order или файлом заказа.")]
        customer = dto.CustomerIn(name=name[:200], phone=phone[:50])
        sent = self.core.send_preorder(self.session(user_id), preorder_id, customer).data
        assert isinstance(sent, dto.PreorderOut)
        # Текст приёма заявки — общий для всех каналов (`core.ui.order_accepted`):
        # клиент, написавший и в Telegram, и на сайте, должен видеть одно и то же.
        settings = self.core.services.settings
        amount = sent.totals.get("amount")
        lines = [order_accepted(sent.id, amount, delivered=sent.status == "SENT_TO_MANAGER")]
        note = delivery_note(amount, settings.min_delivery_rub, settings.delivery_url)
        if note:
            lines.append(note)
        return [Message("\n".join(lines))]

    def _history(self, user_id: str) -> list[TelegramReply]:
        data = self.core.history(self.session(user_id)).data
        assert isinstance(data, dto.HistoryOut)
        if not data.preorders and not data.specifications:
            return [Message("Предзаказов и спецификаций пока нет. Соберите корзину и нажмите /order.")]
        lines = ["Ваши предзаказы:"] if data.preorders else []
        lines += [f"• {p['id']} — {_status(p['status'])}, {price_text(p['amount'])}" for p in data.preorders[:10]]
        if data.specifications:
            lines += ["", "Спецификации:"]
            lines += [f"• {s['id']} — {price_text(s['amount'])}, каталог {s['catalog_version']}" for s in data.specifications[:10]]
        return [Message("\n".join(lines).strip())]

    def _forget_contact(self, user_id: str) -> None:
        self._awaiting_contact.pop(user_id, None)
        self._contact_misses.pop(user_id, None)

    def _guard(self, work: Callable[[], list[TelegramReply]]) -> list[TelegramReply]:
        """Ошибка ядра — понятная фраза человеку, а не молчание бота."""
        try:
            return work()
        except DomainError as exc:
            log.info("Telegram: операция ядра отклонена: %s", exc.code)
            return [Message(exc.message, keyboard=Keyboard().row(Button("Меню", "menu")))]


_ASK_CONTACT = (
    "Чтобы менеджер связался с вами, нажмите «Отправить контакт» или напишите имя и телефон "
    "одним сообщением."
)
_STATUSES = {
    "READY_FOR_MANAGER": "готов к передаче",
    "SENT_TO_MANAGER": "у менеджера",
    "MANAGER_REVIEW": "менеджер проверяет",
    "CONFIRMED": "подтверждён",
    "REJECTED": "отклонён",
}
_CONTACT_BY_HAND = (
    "Кнопка «Отправить контакт» передаёт номер сама — похоже, она не сработала. "
    "Напишите имя и телефон одним сообщением, и менеджер получит заявку."
)


def _about_something_else(text: str) -> bool:
    """Реплика во время ожидания контакта — про другое, а не имя с телефоном."""
    lowered = text.lower()
    return "?" in text or len(text.split()) > NAME_WORDS or any(word in lowered for word in _ASKING_WORDS)


def _status(code: str) -> str:
    return _STATUSES.get(code, code)


def preorder_preview(matched: list[dict]) -> str:
    """Состав будущего предзаказа: позиция, количество, цена, сумма — и итог.

    До 16.09 итог проверки файла был только счётом строк и общей суммой. Человек, приславший
    заполненную выгрузку бота, соглашался вслепую: что именно и почём уходит менеджеру, он видел
    лишь в файле предзаказа — уже после того, как оставил телефон.
    """
    rows = [item for item in matched if item.get("quantity")]
    if not rows:
        return ""
    lines = ["", "Предзаказ на согласование:"]
    for item in rows[:PREVIEW_LINES]:
        name = item.get("name") or item.get("source_name") or ""
        price, total = item.get("current_price"), item.get("current_total")
        if price:
            lines.append(f"{item['line_no']}. {name} — {item['quantity']} × {price_text(price)} = {price_text(total)}")
        else:
            lines.append(f"{item['line_no']}. {name} — {item['quantity']} шт., цена по запросу")
    if len(rows) > PREVIEW_LINES:
        left = len(rows) - PREVIEW_LINES
        lines.append(f"… и ещё {left} {plural(left, 'позиция', 'позиции', 'позиций')} — весь состав в файле предзаказа.")
    amount = sum(item.get("current_total") or 0 for item in rows)
    lines.append(f"Итого {len(rows)} {plural(len(rows), 'позиция', 'позиции', 'позиций')} на {price_text(amount)}.")
    return "\n".join(lines)


def evaluation_text(order: dto.OrderOut, evaluation: dto.EvaluationOut) -> str:
    """Итог проверки заказа. Числа — из оценки ядра, здесь только слова."""
    summary = evaluation.summary
    lines = [
        f"Проверил заказ «{order.source_file['filename']}»: позиций {summary['checked']}.",
        f"✓ найдено в каталоге: {summary['matched']}",
    ]
    for key, label in (
        ("price_changed", "цена изменилась"),
        ("not_available", "нет в наличии"),
        ("unknown_stock", "наличие неизвестно"),
        ("review_required", "требуют проверки менеджера"),
    ):
        if summary.get(key):
            lines.append(f"• {label}: {summary[key]}")
    lines.append(
        f"Сумма по текущим ценам: {price_text(summary['current_amount'])} "
        f"(в файле {price_text(summary['document_amount'])})."
    )
    lines.append(f"Итог: {EVALUATION_LABELS.get(evaluation.status, evaluation.status)}.")
    problems = [item for item in evaluation.items if item["errors"] or item["warnings"]]
    if problems:
        lines.append("")
        for item in problems[:PROBLEM_LINES]:
            codes = [notice["code"] for notice in (*item["errors"], *item["warnings"])]
            labels = ", ".join(NOTICE_LABELS.get(code, code) for code in codes if code != "DOCUMENT_PRICE_MISSING")
            if labels:
                lines.append(f"Строка {item['source_line']}: {item['source_name'] or item['source_article']} — {labels}")
        if len(problems) > PROBLEM_LINES:
            lines.append(f"… и ещё {len(problems) - PROBLEM_LINES}")
    return "\n".join(lines)
