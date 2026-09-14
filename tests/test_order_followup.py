"""Продолжение живого прогона 14.09: присланный заказ и «детский сад целиком».

- docx «Заказ на Элтик» — 65 строк с пунктами 1057, «найдено в каталоге: 0»: названия перечня не
  совпадают с названиями каталога, а поиска по пункту перечня не было;
- «подбери по этому заказу», «подбери из наличия 30 позиций» после файла получали вопрос о помещении;
- «мы открыли частный детский сад» получало общий текст без единого раздела перечня.
"""

from __future__ import annotations

import pytest

from core import intent
from core.profile import DialogProfile, whole_object
from core_fixtures import item as norm_item
from norms.items import ItemIndex
from test_agent import USER, FakeCloudRu, answer, attach, client
from test_core_api import build
from test_live_0914 import _docx_with_lines

ORDER_LINES = [
    "Заказ на Элтик",
    "1.5.1.7 Мат гимнастический — 1 шт.",
    "1.5.1.33 Мяч для игр — 4 шт.",
    "1.5.1.13 Доска с ребристой поверхностью — 2 шт.",
    "1.13.3.1.4 Кресло педагога — 1 шт.",
]


@pytest.fixture
def env(tmp_path):
    from adapters.telegram.gateway import TelegramGateway

    api = build(tmp_path)
    return api, TelegramGateway(api.core, 20 * 1024 * 1024)


def _upload(gateway, tmp_path):
    path = _docx_with_lines(tmp_path / "Заказ на Элтик.docx", ORDER_LINES)
    return gateway.upload(USER, path.name, path.read_bytes())[0]


def _actions(reply) -> set[str]:
    return {button.action for row in reply.keyboard.rows for button in row}


def test_order_lines_are_found_by_their_norm_point(env, tmp_path):
    api, gateway = env
    reply = _upload(gateway, tmp_path)

    assert "найдено в каталоге: 3" in reply.text
    order = api.engine.session(USER, "telegram").profile.order
    names = [api.engine.index.get(position["sku"]).name if position["sku"] else None for position in order["positions"]]
    assert names == ["Мат детский", "Мяч баскетбольный № 3", "Доска ребристая", None]
    assert [position["point"] for position in order["positions"]][-1] == "1.13.3.1.4"


def test_list_by_the_uploaded_order_does_not_ask_for_a_room(env, tmp_path):
    api, gateway = env
    _upload(gateway, tmp_path)
    with FakeCloudRu([answer("Для какого помещения подбираем?")] * 6) as cloud:
        attach(api.engine, client(cloud.base_url))
        listed = gateway.text(USER, "подбери что -либо по этому заказу. выведи списком  - то что есть")[0]
        in_stock = gateway.text(USER, "ты дал огромный перечень товара, подбери из наличия 30 позиций и дай списком")[0]

    session = api.engine.session(USER, "telegram")
    assert session.route["fallback"] == "order_list"
    assert "По заказу «Заказ на Элтик.docx» в каталоге: 3 позиции из 4 строк" in listed.text
    assert "Мат детский" in listed.text and "п. 1.5.1.7" in listed.text
    assert "в наличии: 2 позиции" in in_stock.text and "Доска ребристая" not in in_stock.text
    assert {"export:xlsx", "export:docx", "add_all"} <= _actions(in_stock)
    assert len(session.profile.shortlist) == 2


def test_more_continues_the_order_list(env, tmp_path):
    api, gateway = env
    _upload(gateway, tmp_path)
    session = api.engine.session(USER, "telegram")

    first = api.engine.order_list(session, "", 1)[0]
    assert "order_more" in _actions(first)
    more = gateway.action(USER, "order_more")[0]
    assert "показываю 2–3" in more.text and "Мяч баскетбольный" in more.text
    assert "больше позиций в каталоге нет" in gateway.action(USER, "order_more")[0].text


def test_whole_kindergarten_gets_the_rooms_of_order_1057(env):
    api, gateway = env
    items = [
        norm_item("order_1057", "1.5", "Спортивный зал"),
        norm_item("order_1057", "1.5.1", "Спортивное оборудование и инвентарь"),
        norm_item("order_1057", "1.13", "Кабинеты специалистов"),
        norm_item("order_1057", "1.13.3", "Кабинет учителя-логопеда"),
        norm_item("order_1057", "1.14", "Групповые помещения"),
        norm_item("order_1057", "1.14.2", "Групповые помещения для детей до 1 года"),
        norm_item("order_1057", "1.14.8", "Групповые помещения для детей 6 - 7 лет"),
    ]
    api.engine.norm_texts = ItemIndex({"order_1057": {entry.code: entry for entry in items}})
    with FakeCloudRu([answer("Для сада нужны мебель, игрушки и дидактика.")] * 6) as cloud:
        attach(api.engine, client(cloud.base_url))
        reply = gateway.text(USER, "мы открыли частный детский сад. дай рекомендации по его оснащению")[0]

    assert api.engine.session(USER, "telegram").route["fallback"] == "object_rooms"
    assert "• 1.14 Групповые помещения — отдельно по возрастам: до 1 года … 6–7 лет" in reply.text
    assert "• 1.5 Спортивный зал (в каталоге 3 товара)" in reply.text
    assert "• 1.13 Кабинеты специалистов: учителя-логопеда" in reply.text
    assert "С какого помещения начнём?" in reply.text


@pytest.mark.parametrize("text", ["подбери что -либо по этому заказу", "что есть из файла", "по заказу выведи списком"])
def test_order_is_referenced(text):
    assert intent.mentions_order(text)


def test_making_an_order_is_not_a_reference_to_the_file():
    assert not intent.mentions_order("хочу сделать заказ мячей")


def test_whole_object_is_an_institution_without_a_room():
    assert whole_object("мы открыли частный детский сад. дай рекомендации по его оснащению")
    assert not whole_object("Оснастить спортивный зал ДОУ по приказу №1057")
    assert not whole_object("Оснастить кружок робототехники")
    assert not whole_object("Открываем новую группу в детском саду, с чего начать закупку?")


def test_order_survives_the_state_file():
    order = {"id": "UO-1", "file": "Заказ.docx", "positions": [{"point": "1.5.1.7", "name": "Мат", "quantity": 1, "sku": "B2"}]}
    profile = DialogProfile()
    profile.remember_order(order)

    restored = DialogProfile.from_dict(profile.to_dict())
    assert restored.order == order and restored.export == "order"
    assert "Прислан заказ «Заказ.docx»: строк 1, товаров каталога нашлось 1" in restored.as_prompt()
    restored.reset_task()
    assert restored.order is None
