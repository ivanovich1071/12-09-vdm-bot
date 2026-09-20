"""NEXT-4: Telegram поверх Core API — диалог, файл заказа, спецификация, предзаказ."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("aiogram")
pytest.importorskip("fastapi")

from adapters.telegram.bot import build_dispatcher, send, to_markup  # noqa: E402
from adapters.telegram.gateway import ContactRequest, FileReply, TelegramGateway  # noqa: E402
from core.models import Cart, CartItem  # noqa: E402
from core.ui import Button, Keyboard, Message, OrderSummary, ProductList  # noqa: E402
from test_core_api import build  # noqa: E402
from test_order_core import HEADER, xlsx  # noqa: E402

USER = "555"


@pytest.fixture
def env(tmp_path):
    api = build(tmp_path)
    return api, TelegramGateway(api.core, 20 * 1024 * 1024)


def actions(reply) -> list[str]:
    keyboard = getattr(reply, "keyboard", None)
    return [button.action for row in (keyboard.rows if keyboard else []) for button in row]


def test_dialogue_goes_through_core_api(env):
    api, gateway = env
    replies = gateway.text(USER, "Школа, кабинет информатики")
    assert any(isinstance(reply, ProductList) for reply in replies)
    session = api.core.services.sessions.repository.find("telegram", USER)
    assert session is not None and session.origin == "adapter"


def test_uploaded_order_is_checked_by_core(env):
    _, gateway = env
    content = xlsx([HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "800"], ["2", "", "Телескоп космический", "1", "1"]])
    [reply] = gateway.upload(USER, "заказ.xlsx", content)
    assert "Проверил заказ «заказ.xlsx»: позиций 2" in reply.text
    assert "цена изменилась: 1" in reply.text and "не найдено в каталоге" in reply.text
    assert any(action.startswith("po_order:UO-") for action in actions(reply))


def test_preorder_with_consent_and_contact_text(env):
    api, gateway = env
    [evaluation] = gateway.upload(USER, "заказ.xlsx", xlsx([HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "908"]]))
    order_action = next(a for a in actions(evaluation) if a.startswith("po_order:"))
    summary, consent = gateway.action(USER, order_action)
    assert "ещё не заказ" in summary.text and "согласие" in consent.text.lower()
    consent_action = next(a for a in actions(consent) if a.startswith("po_consent:"))
    [ask] = gateway.action(USER, consent_action)
    assert isinstance(ask, ContactRequest) and api.storage.active_consent(USER)

    [again] = gateway.text(USER, "меня зовут Иван")
    assert isinstance(again, ContactRequest) and "телефон" in again.text
    [done] = gateway.text(USER, "Иван Петров, +7 900 111-22-33")
    assert "Менеджер свяжется" in done.text and len(api.notifier.sent) == 1
    preorder = api.core.services.preorders.get(api.notifier.sent[0], USER)
    assert preorder.customer["name"] == "Иван Петров" and preorder.customer["phone"].endswith("22-33")


def test_contact_button_path(env):
    api, gateway = env
    api.storage.record_consent(USER, "telegram", "test", "granted")
    [evaluation] = gateway.upload(USER, "o.xlsx", xlsx([HEADER, ["1", "B2", "Мат детский", "1", "8164"]]))
    _, ask = gateway.action(USER, next(a for a in actions(evaluation) if a.startswith("po_order:")))
    assert isinstance(ask, ContactRequest)
    [done] = gateway.contact(USER, "Иван", "+79001112233")
    assert "Менеджер свяжется" in done.text


def test_spec_command_sends_excel_and_word(env):
    _, gateway = env
    gateway.action(USER, "add:I1")
    [excel] = gateway.text(USER, "/spec")
    assert isinstance(excel, FileReply) and excel.filename.endswith(".xlsx") and excel.content.startswith(b"PK")
    assert "50 000 ₽" in excel.caption
    spec_id = next(a for a in actions(excel) if a.startswith("po_spec:")).split(":", 1)[1]
    [word] = gateway.action(USER, f"spec_docx:{spec_id}")
    assert word.filename == f"{spec_id}.docx"


def test_errors_become_human_messages(env):
    _, gateway = env
    [reply] = gateway.text(USER, "/spec")
    assert isinstance(reply, Message) and "Корзина пуста" in reply.text
    [unknown] = gateway.action(USER, "po_spec:SP-NOPE")
    assert "не найдена" in unknown.text


def test_preorders_command(env):
    _, gateway = env
    [empty] = gateway.text(USER, "/preorders")
    assert "пока нет" in empty.text
    gateway.action(USER, "add:I3")
    [excel] = gateway.text(USER, "/spec")
    gateway.action(USER, next(a for a in actions(excel) if a.startswith("po_spec:")))
    [listing] = gateway.text(USER, "/preorders")
    assert "PO-" in listing.text and "готов к передаче" in listing.text


def test_telegram_users_cannot_reach_each_other(env):
    api, gateway = env
    alice, bob = "1001", "1002"
    gateway.action(alice, "add:I1")
    [excel] = gateway.text(alice, "/spec")
    spec_id = next(a for a in actions(excel) if a.startswith("po_spec:")).split(":", 1)[1]
    [evaluation] = gateway.upload(alice, "заказ.xlsx", xlsx([HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "908"]]))
    order_id = next(a for a in actions(evaluation) if a.startswith("po_order:")).split(":", 1)[1]
    _, consent = gateway.action(alice, f"po_order:{order_id}")
    preorder_id = next(a for a in actions(consent) if a.startswith("po_consent:")).split(":", 1)[1]

    for foreign in (f"spec_xlsx:{spec_id}", f"spec_docx:{spec_id}", f"po_spec:{spec_id}", f"po_order:{order_id}"):
        [reply] = gateway.action(bob, foreign)
        assert type(reply) is Message and "не найден" in reply.text, (foreign, reply)
    # Чужой предзаказ не отправить и через согласие с контактом.
    gateway.action(bob, f"po_consent:{preorder_id}")
    [refused] = gateway.contact(bob, "Боб", "+79001112233")
    assert "не найден" in refused.text and api.notifier.sent == []
    assert "пока нет" in gateway.text(bob, "/preorders")[0].text
    assert "Корзина пуста" in gateway.text(bob, "/spec")[0].text


def test_delete_data_resets_adapter_session(env):
    _, gateway = env
    gateway.text(USER, "здравствуйте")
    assert USER in gateway._sessions
    gateway.text(USER, "/delete_data")
    assert USER not in gateway._sessions


class FakeBot:
    def __init__(self) -> None:
        self.documents: list[tuple[str, str]] = []
        self.messages: list[tuple[str, object]] = []

    async def send_document(self, chat_id, document, caption=None, reply_markup=None):  # noqa: ANN001
        self.documents.append((document.filename, caption))

    async def send_message(self, chat_id, text, reply_markup=None):  # noqa: ANN001
        self.messages.append((text, reply_markup))


async def test_renderer_sends_files_and_contact_button():
    bot = FakeBot()
    reply = FileReply("SP-1.xlsx", b"PK", "Спецификация", Keyboard().row(Button("Предзаказ", "po_spec:SP-1")))
    await send(bot, 1, [reply, ContactRequest("Пришлите контакт")])
    assert bot.documents == [("SP-1.xlsx", "Спецификация")]
    text, markup = bot.messages[0]
    assert text == "Пришлите контакт" and markup.keyboard[0][0].request_contact is True


def test_dispatcher_handles_text_files_and_contacts_through_gateway(env):
    _, gateway = env
    dispatcher = build_dispatcher(gateway)
    # Текст, документ, контакт и перехватчик всего остального (фото, голосовое,
    # стикер): без последнего бот на такое сообщение молчал.
    assert len(dispatcher.message.handlers) == 4 and len(dispatcher.callback_query.handlers) == 1


def test_deeplink_from_widget_transfers_cart(env):
    """«Продолжить в Telegram»: /start <сессия сайта> переносит корзину из виджета в чат."""
    api, gateway = env
    widget_session = "a" * 32
    api.storage.save_cart(
        Cart(
            user_id=widget_session,
            items=[CartItem(sku_1c="B1", name="Мяч баскетбольный № 3", price=908, quantity=2)],
        )
    )

    replies = gateway.text(USER, f"/start {widget_session}")

    texts = [reply.text for reply in replies if isinstance(reply, Message)]
    assert any("виджета" in text for text in texts), texts
    cart = api.storage.load_cart(USER)
    assert cart.count == 2 and cart.items[0].sku_1c == "B1"
    # Корзина в чате показана сразу — тот же вид, что по кнопке «Корзина».
    assert any(isinstance(reply, OrderSummary) or "Корзина" in getattr(reply, "text", "") for reply in replies)


def test_deeplink_with_foreign_payload_does_not_touch_cart(env):
    api, gateway = env
    unknown = "b" * 32  # такой сессии нет — корзину не переносим

    replies = gateway.text(USER, f"/start {unknown}")

    assert api.storage.load_cart(USER).is_empty
    assert not any("виджета" in getattr(reply, "text", "") for reply in replies)


ALLOWED = {
    "__future__", "logging", "collections.abc", "dataclasses", "re",
    "core.errors", "core.models", "core.ui", "core_api", "core_api.facade",
    "core_api.sessions", "privacy.consent", "privacy.masking",
}


# --- Кнопка «Задать вопрос» (онлайн-чат заказчика, SUPPORT_CHAT_URL) ------------

CHAT_URL = "https://vdmmsk.bitrix24.ru/online/chat"


def test_support_chat_button_in_main_menu(tmp_path):
    """URL задан — в главном меню появляется кнопка со ссылкой на чат."""
    api = build(tmp_path, support_chat_url=CHAT_URL)
    gateway = TelegramGateway(api.core, 20 * 1024 * 1024)
    [reply] = gateway.action(USER, "menu")
    chat = [
        button
        for row in reply.keyboard.rows
        for button in row
        if button.title == "Задать вопрос"
    ]
    assert chat and chat[0].url == CHAT_URL and chat[0].action == "noop"


def test_support_chat_button_hidden_without_url(env):
    """URL пуст (как по умолчанию) — меню в точности прежнее, лишней кнопки нет."""
    _, gateway = env
    [reply] = gateway.action(USER, "menu")
    assert all(
        button.title != "Задать вопрос"
        for row in reply.keyboard.rows
        for button in row
    )


def test_support_chat_button_renders_as_url_button(tmp_path):
    """Telegram получает кнопку-ссылку: открытие чата делает клиент, не бот."""
    api = build(tmp_path, support_chat_url=CHAT_URL)
    gateway = TelegramGateway(api.core, 20 * 1024 * 1024)
    [reply] = gateway.action(USER, "menu")
    markup = to_markup(reply.keyboard)
    urls = [button.url for row in markup.inline_keyboard for button in row if button.url]
    assert CHAT_URL in urls


def test_gateway_has_no_business_logic_dependencies():
    """Шлюз не ищет товары, не читает каталог и базу, не проверяет нормы — только Core API."""
    path = Path(__file__).parents[1] / "src" / "adapters" / "telegram" / "gateway.py"
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert imported <= ALLOWED, imported - ALLOWED


CHANNEL = "telegram"


def test_repeated_checkout_reuses_one_preorder(env):
    """Вторая кнопка «Оформить» с той же корзиной — прежняя заявка, а не копия (19.09)."""
    api, gateway = env
    api.engine.handle_action(USER, CHANNEL, "add:B1")

    first = gateway.action(USER, "checkout")
    second = gateway.action(USER, "checkout")

    def preorder_id(replies):
        for reply in replies:
            text = getattr(reply, "text", "") or ""
            if "Предварительный заказ" in text:
                return text.split("Предварительный заказ ")[1].split(":")[0]
        return None

    one, two = preorder_id(first), preorder_id(second)
    assert one and one == two
    assert len(api.core.services.preorders.of_owner(USER)) == 1
