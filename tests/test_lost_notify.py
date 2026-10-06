"""Ушедшие клиенты: одно письмо менеджерам после CLIENT_LOST_DAYS тишины.

Вопросы 9, 16 и 17 опросного листа. Напоминаний клиенту нет по решению
заказчика: бот пишет только менеджерам, и один раз на одно молчание.
"""

import pytest

from core.config import Settings
from core.models import Cart, CartItem
from core.storage import Storage
from orders.lost import notify_lost_clients

CHANNEL = "telegram"
USER = "u1"
# Гарантированно в прошлом: любой срок молчания меньше этой даты.
OLD = "2026-08-01T10:00:00+00:00"


class FakeSmtp:
    instances: list = []

    def __init__(self, host, port, timeout=None):  # noqa: ANN001
        self.host, self.port = host, port
        self.sent = []
        self.logged_in = None
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):  # noqa: ANN001
        self.logged_in = (user, password)

    def send_message(self, message):  # noqa: ANN001
        self.sent.append(message)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    import smtplib

    FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    return FakeSmtp


def settings_for(**overrides):  # noqa: ANN003
    values = dict(
        smtp_host="smtp.test",
        smtp_user="bot@test",
        smtp_password="s3cret",
        smtp_from="bot@test",
        order_email_to="mgr@test",
    )
    values.update(overrides)
    return Settings(**values)


def backdate(storage: Storage, user_id: str, stamp: str = OLD) -> None:
    """Молчание имитируем сдвигом отметки разговора в прошлое."""
    storage._db.execute(
        "UPDATE dialog_state SET updated_at = ? WHERE user_id = ?", (stamp, user_id)
    )
    storage._db.commit()


def storage_with(tmp_path, history=None, profile=None, cart=None):  # noqa: ANN001, ANN003
    storage = Storage(tmp_path / "t.sqlite3")
    storage.save_dialog_state(USER, CHANNEL, history or [], profile or {})
    backdate(storage, USER)
    if cart is not None:
        storage.save_cart(cart)
    return storage


def test_silence_beyond_the_term_sends_one_letter(tmp_path):
    storage = storage_with(
        tmp_path,
        history=[{"role": "user", "content": "интересовала сенсорная комната"}],
        profile={"institution": "детский сад", "room": "сенсорная комната"},
    )
    settings = settings_for()

    assert notify_lost_clients(settings, storage) == 1
    [letter] = FakeSmtp.instances[0].sent
    assert "Ушедший клиент" in letter["Subject"]
    body = letter.get_body(preferencelist=("plain",)).get_content()
    assert "сенсорная комната" in body and "детский сад" in body

    # Отметка о письме: второе уведомление по тому же молчанию не уходит.
    assert notify_lost_clients(settings, storage) == 0
    assert len(FakeSmtp.instances[0].sent) == 1


def test_letter_names_the_left_cart(tmp_path):
    cart = Cart(user_id=USER, items=[CartItem("S1", "Мяч баскетбольный", 908, 2)])
    storage = storage_with(
        tmp_path, history=[{"role": "user", "content": "нужны мячи в зал"}], cart=cart
    )
    notify_lost_clients(settings_for(), storage)

    [letter] = FakeSmtp.instances[0].sent
    body = letter.get_body(preferencelist=("plain",)).get_content()
    assert "Мяч баскетбольный — 2 шт." in body


def test_fresh_dialog_is_ignored(tmp_path):
    storage = Storage(tmp_path / "t.sqlite3")
    storage.save_dialog_state(
        USER, CHANNEL, [{"role": "user", "content": "привет"}], {"institution": "школа"}
    )
    assert notify_lost_clients(settings_for(), storage) == 0
    assert FakeSmtp.instances == []


def test_qa_owner_is_skipped(tmp_path):
    storage = storage_with(
        tmp_path, history=[{"role": "user", "content": "нужен счёт"}], profile={}
    )
    assert notify_lost_clients(settings_for(qa_user_ids=frozenset({USER})), storage) == 0
    assert FakeSmtp.instances == []


def test_passerby_without_substance_is_marked_silently(tmp_path):
    """Пустая история без корзины — не ушедший клиент, и сканировать его больше не нужно."""
    storage = storage_with(tmp_path, history=[], profile={})
    assert notify_lost_clients(settings_for(), storage) == 0
    assert FakeSmtp.instances == []
    assert storage.stale_dialogs("2100-01-01", channel="telegram") == []


def test_disabled_or_unconfigured_is_a_no_op(tmp_path):
    storage = storage_with(
        tmp_path, history=[{"role": "user", "content": "нужен счёт"}], profile={}
    )
    assert notify_lost_clients(settings_for(lost_notify_enabled=False), storage) == 0
    assert notify_lost_clients(settings_for(smtp_host=""), storage) == 0
    assert FakeSmtp.instances == []
