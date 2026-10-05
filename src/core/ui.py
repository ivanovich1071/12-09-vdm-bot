"""Примитивы ответа, общие для всех каналов.

Ядро никогда не собирает разметку Telegram или HTML виджета. Оно возвращает эти
объекты, а адаптер рендерит их по-своему: Telegram — карточкой с кнопками, виджет —
текстом и списком заказа. Благодаря этому логика продажи не размножается по каналам.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from catalog.models import Product

# Потолок раскладки берём по самому строгому каналу — MAX: до 7 кнопок в ряду,
# до 3, если это кнопки-ссылки. Иначе интерфейс разъедется между каналами.
MAX_BUTTONS_IN_ROW = 7
MAX_LINK_BUTTONS_IN_ROW = 3

# Markdown консультанта (**жирный**, ____, `код`): Telegram рисует его сам, а виджет
# и мини-апп показывают текст как есть — 21.09 клиент видел «**2.20 «Кабинет труда»**»
# со звёздочками. Веб-рендеры снимают разметку этим помощником.
_MARKDOWN = re.compile(r"\*\*|__|`")


def plain_text(text: str) -> str:
    """Текст без markdown-разметки: для каналов, которые её не рисуют."""
    return _MARKDOWN.sub("", text or "")


@dataclass(frozen=True)
class Button:
    title: str
    action: str  # callback-команда ядра
    url: str | None = None

    @property
    def is_link(self) -> bool:
        return self.url is not None


@dataclass
class Keyboard:
    rows: list[list[Button]] = field(default_factory=list)

    def row(self, *buttons: Button) -> Keyboard:
        limit = MAX_LINK_BUTTONS_IN_ROW if any(b.is_link for b in buttons) else MAX_BUTTONS_IN_ROW
        if len(buttons) > limit:
            raise ValueError(f"В ряду не больше {limit} кнопок: {[b.title for b in buttons]}")
        # Повтор кнопки (по действию и ссылке) в одной клавиатуре — всегда ошибка
        # сборки: 21.09 «Связаться с менеджером» стояла в одном сообщении дважды.
        # Повтор не добавляем, а не падаем: ответ важнее раскладки.
        fresh = [
            button
            for button in buttons
            if not any(
                button.action == seen.action and button.url == seen.url
                for row in self.rows
                for seen in row
            )
        ]
        if fresh:
            self.rows.append(fresh)
        return self


@dataclass
class Message:
    text: str
    keyboard: Keyboard | None = None
    # Заменить сообщение, под которым нажали кнопку, вместо отправки нового.
    # Без этого каждое «+» в корзине оставляло в чате ещё одну её копию, и
    # человек, не видя изменений, жал снова — пять одинаковых карточек подряд
    # в переписке заказчика появились именно так.
    replace: bool = False


@dataclass
class ProductCard:
    """Карточка товара «как на сайте».

    В виджете рендерится строкой без фото — там карточек нет по договорённости,
    но данные те же, чтобы ответы каналов не расходились.
    """

    product: Product
    quantity: int = 0
    citation: str | None = None
    keyboard: Keyboard | None = None
    # Заполняется на лету: в выгрузке 1С изображений нет, они добираются с сайта.
    image: str | None = None
    # Тот же снимок, уже лежащий у нас на диске. Telegram не может забрать
    # картинку с vdm.ru сам, поэтому файл ему нужнее адреса.
    image_path: str | None = None
    # Все нормативные основания с формулировками пунктов — для подробной карточки.
    # В строке выдачи показывается одно, в карточке нужны все: по ним собирают
    # спецификацию, и там важно видеть, сколько позиций перечня товар закрывает.
    norms: list[str] = field(default_factory=list)
    replace: bool = False


@dataclass
class ProductList:
    title: str
    cards: list[ProductCard]
    total_found: int = 0
    keyboard: Keyboard | None = None


@dataclass
class OrderLine:
    """Строка заказа. Отдельный тип, потому что кортежа перестало хватать.

    Заказчик не мог опознать товар в корзине: название жило внутри кнопки и
    обрезалось до 24 символов — «1 × Сенсом...». Теперь название и артикул идут
    текстом, где места сколько угодно, а кнопки остаются короткими.
    """

    name: str
    quantity: int
    price: int | None
    sku_1c: str = ""
    norm_citation: str | None = None


@dataclass
class OrderSummary:
    lines: list[OrderLine]
    total: int
    note: str | None = None
    keyboard: Keyboard | None = None
    replace: bool = False


Response = Message | ProductCard | ProductList | OrderSummary


def price_text(value: int | None) -> str:
    """Цена в человеческом виде. Пустая цена — не ноль, а «по запросу»."""
    if value is None:
        return "цена по запросу"
    return f"{value:,}".replace(",", " ") + " ₽"


def stock_text(product: Product) -> str:
    return f"в наличии {product.in_stock} шт." if product.available else "под заказ"


# Часы работы менеджеров — заказчик просил называть их при каждом приёме заявки,
# чтобы человек не ждал ответа ночью.
WORKING_HOURS = "Заявки обрабатываются в рабочее время, 10:00–18:00 МСК."


def order_accepted(order_id: str, total: int | None, *, delivered: bool = True, test: bool = False) -> str:
    """Что клиент видит после «Оформить» — одинаково во всех каналах.

    Текст был написан дважды, в диалоге и в Telegram-шлюзе, и разошёлся: в одном
    месте про рабочее время говорилось, в другом нет. Срок поставки не называем —
    его подтверждает менеджер, это условие заказчика.
    """
    if test:
        # Тестовый владелец (QA_USER_IDS): заявка наружу не уходит — и клиент
        # должен это видеть, а не ждать звонок (ТЗ BUG-37, шаг 5.8).
        tail = "ТЕСТ: заявка сохранена, менеджеру не отправлена."
    elif delivered:
        tail = "Менеджер свяжется с вами в ближайшее время."
    else:
        tail = "Заказ сохранён, менеджер получит его чуть позже — мы повторим отправку."
    return f"Ваш заказ {order_id} принят на {price_text(total)}. {tail}\n{WORKING_HOURS}"


def delivery_note(total: int | None, min_rub: int, url: str) -> str | None:
    """Оговорка про доставку, когда сумма не дотягивает до порога.

    Это не запрет: заявка уходит менеджеру при любой сумме. Стоимость и сроки бот
    не считает — лестница тарифов зависит от региона и от того, частное лицо или
    учреждение, поэтому отправляем к условиям и к менеджеру.
    """
    if total is None or min_rub <= 0 or total >= min_rub:
        return None
    return (
        f"Доставка оформляется от {price_text(min_rub)}. При меньшей сумме остаются "
        f"самовывоз и отправка транспортной компанией — менеджер подскажет, что удобнее. "
        f"Условия доставки: {url}"
    )


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение после числительного: 1 позиция, 2 позиции, 5 позиций."""
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many
