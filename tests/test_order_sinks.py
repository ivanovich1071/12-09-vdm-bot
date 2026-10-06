"""Куда уходит заказ: письмо менеджеру, наличие в спецификации, дубли."""

import zipfile

import pytest

from core.models import Cart, CartItem, Customer, Order
from orders.sinks import CompositeSink, JsonlSink, SmtpSink, XlsxSink, order_rows, order_xlsx


@pytest.fixture
def order():
    cart = Cart(
        user_id="u1",
        items=[
            CartItem("S1", "Мяч баскетбольный", 908, 2, in_stock=4),
            CartItem("S2", "Стол логопеда", 12000, 1, in_stock=0),
        ],
    )
    return Order.create(cart, Customer(name="Иванов", phone="+7 916 330-02-79"), "web", "c1")


class FakeSmtp:
    """Подставной сервер: запоминает вход и отправленные письма."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.logged_in = None
        self.sent = []
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    import smtplib

    FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    return FakeSmtp


def sink(**kwargs):
    return SmtpSink(host="smtp.example", user="bot@example", password="s3cret", to="m@example", **kwargs)


def test_letter_carries_the_specification_as_an_attachment(order):
    sink().push(order)

    [letter] = FakeSmtp.instances[0].sent
    assert order.id in letter["Subject"] and "2 позиции" in letter["Subject"]
    [attachment] = [part for part in letter.iter_attachments()]
    assert attachment.get_filename() == f"{order.id}.xlsx"
    with zipfile.ZipFile(__import__("io").BytesIO(attachment.get_payload(decode=True))) as book:
        sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Мяч баскетбольный" in sheet


def test_letter_body_gives_the_manager_contacts_and_composition(order):
    sink().push(order)

    [letter] = FakeSmtp.instances[0].sent
    body = letter.get_body(preferencelist=("plain",)).get_content()
    assert "Иванов" in body and "330-02-79" in body
    assert "Мяч баскетбольный" in body and "13 816 ₽" in body


def test_port_465_logs_in_over_ssl(order):
    sink().push(order)
    server = FakeSmtp.instances[0]
    assert server.port == 465 and server.logged_in == ("bot@example", "s3cret")


def test_unconfigured_sink_refuses_instead_of_silently_dropping(order):
    with pytest.raises(RuntimeError):
        SmtpSink(host="", to="").push(order)


def test_broken_mail_does_not_lose_the_order(order, tmp_path, monkeypatch):
    """Письмо — не единственная копия: заявка остаётся в jsonl и в Excel."""

    def explode(self, message):
        raise OSError("сервер не отвечает")

    monkeypatch.setattr(FakeSmtp, "send_message", explode)
    composite = CompositeSink(
        [sink(), JsonlSink(path=tmp_path / "orders.jsonl"), XlsxSink(directory=tmp_path)]
    )

    composite.push(order)  # не падает: доставку обеспечили файлы

    assert (tmp_path / "orders.jsonl").exists()
    assert (tmp_path / f"{order.id}.xlsx").exists()


def test_specification_shows_stock_as_the_client_saw_it(order):
    with zipfile.ZipFile(__import__("io").BytesIO(order_xlsx(order))) as book:
        sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Наличие" in sheet
    assert "в наличии 4 шт." in sheet and "под заказ" in sheet


# --- Лид без состава (шаг 4.4) ------------------------------------------------------------


@pytest.fixture
def lead():
    return Order.create_lead(
        "u1",
        "web",
        Customer(name="Иванов", phone="+7 916 330-02-79", comment="Запрос без состава: нужен счёт"),
        "c1",
    )


def test_lead_keeps_contacts_in_the_table(lead):
    """Заявка без состава — одна строка контактов: без неё приёмники молчали бы."""
    [row] = order_rows(lead)
    assert "Иванов" in row and "+7 916 330-02-79" in row
    assert "нужен счёт" in row[-1]


def test_lead_letter_names_the_request_instead_of_zero_items(lead):
    """Тема «0 позиций · 0 ₽» читалась менеджером как сбой; это живой запрос."""
    letter = SmtpSink(host="smtp.test", to="mgr@test").message(lead)
    assert "Запрос без состава" in letter["Subject"]
    assert "0 позиций" not in letter["Subject"]
    body = letter.get_body(preferencelist=("plain",)).get_content()
    assert "Иванов" in body and "330-02-79" in body
    assert "нужен счёт" in body
