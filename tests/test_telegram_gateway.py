"""NEXT-4: Telegram поверх Core API — диалог, файл заказа, спецификация, предзаказ."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("aiogram")
pytest.importorskip("fastapi")

from adapters.telegram.bot import build_dispatcher, send  # noqa: E402
from adapters.telegram.gateway import ContactRequest, FileReply, TelegramGateway  # noqa: E402
from core.ui import Button, Keyboard, Message, ProductList  # noqa: E402
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
    assert "передан менеджеру" in done.text and len(api.notifier.sent) == 1
    preorder = api.core.services.preorders.get(api.notifier.sent[0], USER)
    assert preorder.customer["name"] == "Иван Петров" and preorder.customer["phone"].endswith("22-33")


def test_contact_button_path(env):
    api, gateway = env
    api.storage.record_consent(USER, "telegram", "test", "granted")
    [evaluation] = gateway.upload(USER, "o.xlsx", xlsx([HEADER, ["1", "B2", "Мат детский", "1", "8164"]]))
    _, ask = gateway.action(USER, next(a for a in actions(evaluation) if a.startswith("po_order:")))
    assert isinstance(ask, ContactRequest)
    [done] = gateway.contact(USER, "Иван", "+79001112233")
    assert "передан менеджеру" in done.text


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


def test_dispatcher_with_gateway_handles_files_and_contacts(env):
    api, gateway = env
    assert len(build_dispatcher(api.engine).message.handlers) == 1
    assert len(build_dispatcher(api.engine, gateway).message.handlers) == 3


ALLOWED = {
    "__future__", "logging", "collections.abc", "dataclasses",
    "core.errors", "core.ui", "core_api", "core_api.facade", "core_api.sessions",
    "privacy.consent", "privacy.masking",
}


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
