"""Регрессионный набор 04.10 — реплики-провалы из трёх отчётов.

Источники: «баги 04-10.md» (прогон на живом боте), «ОТЧЁТ_БИТЫЕ_ПЕРЕХОДЫ_2026-10-04.md»
(переписка заказчика), «ОТЧЁТ_ПРОВЕРКА_ТЗ_БОТ_ЭЛТИК.md» (эмуляция без модели).

Три уровня:
- разбор — чистые функции `core.intent` и `procurement.discovery`;
- ядро — `DialogEngine` без модели на фикстурах `core_fixtures` (путь деградации);
- каталог — настоящий `data/kb` (метка `catalog`, без данных пропускается).

Каждый тест описывает ожидаемое поведение после исправления. До своего этапа плана
от 05-10 он красный — это доказательство дефекта, а не поломка набора. Базу без
регресса гоняют `pytest -m "not regress"`, весь набор — на этапе 9.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from catalog.runtime import CatalogRuntime
from catalog.search import CatalogIndex
from core import dialog as dialog_module
from core import intent
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from core.ui import Message, OrderSummary, ProductCard, ProductList
from core_api.composition import build_core
from core_fixtures import procurement_service, products, state
from orders.service import OrderService
from orders.sinks import JsonlSink
from procurement import discovery
from procurement.models import ProcurementTask
from qa.checks import CatalogFacts, check_turn
from qa.models import BotMessage, Turn

USER = "u-regress"
CHANNEL = "telegram"

ROOT = Path(__file__).parents[1]
KB = ROOT / "data" / "kb" / "products.jsonl"

pytestmark = pytest.mark.regress


@pytest.fixture
def engine(tmp_path):
    return make_engine(tmp_path)


def make_engine(tmp_path, extra=None):
    storage = Storage(tmp_path / "regress.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
    engine = DialogEngine(CatalogIndex(products(extra)), storage, orders, settings)
    engine.procurement = procurement_service(tmp_path, CatalogRuntime(state(extra=extra)))
    return engine


def new_task() -> ProcurementTask:
    return ProcurementTask(id="t-regress", owner="u", channel="telegram", created_at="", updated_at="")


def flat(responses) -> str:
    """Весь текст ответа хода: сообщения, заголовки, названия карточек, строки корзины.

    Карточка в Telegram несёт «Код 1С: …» (проверка CARD_TEXT по нему и работает),
    поэтому для карточек он добавлен сюда же.
    """
    parts: list[str] = []
    for response in responses:
        if isinstance(response, Message):
            parts.append(response.text)
        elif isinstance(response, ProductList):
            parts.append(response.title)
            parts += [card.product.name for card in response.cards]
        elif isinstance(response, ProductCard):
            parts.append(response.product.name)
            parts.append(f"Код 1С: {response.product.sku_1c}")
        elif isinstance(response, OrderSummary):
            parts.append(response.note or "")
            parts += [f"{line.name} × {line.quantity}" for line in response.lines]
            parts.append(f"Итого: {response.total} ₽")
    return "\n".join(part for part in parts if part)


def goods(responses) -> list:
    return [r for r in responses if isinstance(r, (ProductList, ProductCard))]


# --- Этап 2 (К2): разбор запроса --------------------------------------------------------------


class TestParsing:
    def test_task_word_needed_dropped(self):
        """BUG-06: «нужен» не матчится шаблоном «нужн\\w*» и уходит в поиск товаров."""
        assert "нужен" not in discovery.query_from_text("нужен ростомер для медкабинета").split()

    def test_greeting_example_is_not_a_query(self):
        """ТЗ BUG-01: приветствие предлагает «чем оснастить спортзал…», а в поиск шло «чем».
        Честное поведение — пустой запрос: подбор по учреждению и помещению, без мусора."""
        assert discovery.query_from_text("чем оснастить спортзал в саду, дети 3–6 лет") == ""

    def test_subject_survives_politeness(self):
        """К2: «подробнее про металлофон — какие размеры и код 1С?» не должно терять металлофон
        и тащить «подробнее/размеры/код» (пробирки вместо ростомера, кукла вместо металлофона)."""
        query = discovery.query_from_text("покажите подробнее про металлофон — какие размеры и код 1С?")
        assert "металлофон" in query
        for noise in ("подробнее", "размеры", "код"):
            assert noise not in query

    def test_negation_becomes_exclusion(self):
        """Переход №7: «комплектация зала, а не песочница» — песочницы больше не показывать."""
        task = new_task()
        discovery.apply_text(task, "нужна комплектация зала, а не песочница")
        assert "песочниц" in " ".join(task.preferences.get("exclude_terms") or [])

    def test_query_not_drifted_by_service_words(self):
        """BUG-08: после «сколько по времени…» задача остаётся прежней, «Показать ещё» не улетает."""
        task = new_task()
        discovery.apply_text(task, "нужны мячи для спортзала")
        discovery.apply_text(task, "сколько по времени оформление счёта?")
        assert "мяч" in (task.preferences.get("query") or "")
        assert "времени" not in (task.preferences.get("query") or "")

    def test_norm_code_with_question_mark(self):
        """К3: «п. 1.13.2.3.9?» разбиралось как 1.13.2.3 — знак вопроса обрезал код."""
        assert intent.norm_code("что за п. 1.13.2.3.9?") == "1.13.2.3.9"

    def test_norm_range_expands(self):
        """BUG-09: «2.30.1-2.30.6» — диапазон, а не пункт «2.30»."""
        assert intent.norm_codes("строго по пунктам 2.30.1-2.30.6 приказа 838") == [
            "2.30.1",
            "2.30.2",
            "2.30.3",
            "2.30.4",
            "2.30.5",
            "2.30.6",
        ]

    def test_point_question_beats_show(self):
        """BUG-14: вопрос о формулировке пункта не должен побиваться словом «покажите»."""
        assert intent.classify("пункт 1.12.5 у вас есть? Покажите формулировку") == intent.NORM_QUESTION

    def test_manager_intent_exists(self):
        """ТЗ BUG-02: «хочу менеджера» — намерение, а не задача подбора."""
        assert intent.asks_manager("хочу менеджера")
        assert intent.asks_manager("позовите человека")
        assert not intent.asks_manager("нужны мячи для зала")

    def test_add_intent_exists(self):
        """BUG-04: «возьму/добавьте» — намерение добавить в корзину."""
        assert intent.asks_add("возьму Лесенку 4 шт.")
        assert intent.asks_add("металлофон 1 шт., маракас 2 шт. добавьте в корзину")
        assert not intent.asks_add("а что вы умеете?")

    def test_checkout_by_list_phrase(self):
        """BUG-03 (главный кейс): «по этому списку подбери все по 1 шт.» — сборка корзины."""
        assert intent.asks_list_to_cart("по этому списку подбери все по 1 шт.")
        assert intent.asks_list_to_cart("собери всё по комплектации по 1 шт.")


# --- Этапы 1.6, 2, 3, 4: ядро без модели на фикстурах ------------------------------------------


class TestCoreFallback:
    def test_manager_request_reaches_manager_card(self, engine):
        """BUG-02: «хочу менеджера» — карточка менеджера, а не подбор по слову «менеджера»."""
        out = flat(engine.handle_text(USER, CHANNEL, "хочу менеджера"))
        assert engine.settings.manager_contact in out
        assert "не нашлось" not in out.lower() and "не нашёл" not in out.lower()

    def test_manager_shout_inside_form_is_not_a_field(self, engine):
        """ТЗ BUG-02: «позовите человека» посреди анкеты — передача менеджеру, а не имя контакта."""
        engine.handle_action(USER, CHANNEL, "add:I1")
        engine.handle_action(USER, CHANNEL, "checkout")
        engine.handle_action(USER, CHANNEL, "consent_yes")
        engine.handle_text(USER, CHANNEL, "Школа 1")
        out = flat(engine.handle_text(USER, CHANNEL, "позовите человека"))
        assert "менеджер" in out.lower()
        assert "Шаг" not in out

    def test_take_words_add_to_cart(self, engine):
        """BUG-04/05: «возьму волейбольный, 2 штуки» кладёт в корзину ровно 2 шт."""
        engine.handle_text(USER, CHANNEL, "покажи волейбольный мяч")
        engine.handle_text(USER, CHANNEL, "возьму волейбольный, 2 штуки")
        cart = engine.storage.load_cart(USER)
        assert cart.count == 2
        assert cart.total == 2400

    def test_cart_change_is_reported(self, engine):
        """BUG-05: молчаливых «5 шт.» не бывает — изменение корзины озвучивается."""
        engine.handle_text(USER, CHANNEL, "покажи волейбольный мяч")
        out = flat(engine.handle_text(USER, CHANNEL, "возьму волейбольный 4 шт."))
        assert "4" in out and "орзине" in out

    def test_collect_all_by_shown_list(self, engine):
        """BUG-03 (главный кейс): «по этому списку подбери все по 1 шт.» собирает корзину."""
        engine.handle_text(USER, CHANNEL, "покажите позиции по пункту 1.5.1 приказа 1057")
        out = flat(engine.handle_text(USER, CHANNEL, "по этому списку подбери все по 1 шт."))
        assert not engine.storage.load_cart(USER).is_empty
        assert "не нашлось" not in out.lower()

    def test_add_phrase_beats_manager_mention(self, engine):
        """Прогон 05.10 (СЦ5/СЦ7): «возьму … 4 шт. и счёт от менеджера» — корзина
        пополняется словами, а не глотается карточкой менеджера."""
        engine.handle_text(USER, CHANNEL, "покажи волейбольный мяч")
        out = flat(engine.handle_text(USER, CHANNEL, "возьму волейбольный 4 шт. и счёт от менеджера"))
        assert engine.storage.load_cart(USER).count == 4
        assert engine.settings.manager_contact not in out

    def test_pure_manager_request_still_reaches_manager(self, engine):
        """Перестановка не задела чистую просьбу: слов добавления нет — карточка менеджера."""
        out = flat(engine.handle_text(USER, CHANNEL, "позовите, пожалуйста, живого человека"))
        assert engine.settings.manager_contact in out

    def test_lead_request_asks_for_contacts(self, engine):
        """Шаг 4.4: «нужен счёт» без корзины — просьба контактов, не заглушка и не менеджер-карточка."""
        out = flat(engine.handle_text(USER, CHANNEL, "нужен счёт на организацию, оплатим по безналу"))
        assert "имя и телефон" in out.lower()
        assert "недоступен" not in out.lower()

    def test_lead_in_one_message_with_consent(self, engine):
        """«Перезвоните по номеру…» — заявка без состава уходит сразу."""
        engine.handle_action(USER, CHANNEL, "consent_yes")
        out = flat(engine.handle_text(USER, CHANNEL, "перезвоните по номеру +7 916 222-33-44, Мария"))
        assert "Заявка" in out and "менеджеру" in out
        [lead] = engine.storage.orders_of(USER)
        assert lead.items == []
        assert lead.customer.phone == "+7 916 222-33-44"
        assert "Мария" in lead.customer.name
        assert "перезвоните" in lead.customer.comment.lower()

    def test_lead_without_consent_asks_first(self, engine):
        """Согласие раньше передачи контактов: «Согласен» доводит заявку до менеджера."""
        first = flat(engine.handle_text(USER, CHANNEL, "нужен счёт, вот телефон +7 916 123-45-67"))
        assert "огласие" in first.lower()
        out = flat(engine.handle_action(USER, CHANNEL, "consent_yes"))
        assert "Заявка" in out and "менеджеру" in out
        [lead] = engine.storage.orders_of(USER)
        assert lead.customer.phone == "+7 916 123-45-67"

    def test_lead_released_when_user_moves_on(self, engine):
        """Человек передумал и спросил товар — бот отвечает, а не виснет на просьбе телефона."""
        engine.handle_text(USER, CHANNEL, "нужен счёт")
        out = flat(engine.handle_text(USER, CHANNEL, "покажи волейбольный мяч"))
        assert "имя и телефон" not in out.lower()

    def test_pure_manager_card_unchanged_by_lead_flow(self, engine):
        """«Хочу менеджера» — карточка с телефоном, как и было (лид её не перехватил)."""
        out = flat(engine.handle_text(USER, CHANNEL, "хочу менеджера"))
        assert engine.settings.manager_contact in out

    def test_price_objection_answered(self, engine):
        """BUG-02/17: на «дорого» не бывает ни деградации, ни «ничего не нашлось»."""
        engine.handle_text(USER, CHANNEL, "покажи мат гимнастический")
        out = flat(engine.handle_text(USER, CHANNEL, "дорого, у других дешевле"))
        assert "недоступен" not in out.lower()
        assert "не нашлось" not in out.lower()
        assert "менеджер" in out.lower() or "₽" in out

    def test_details_refer_to_shown(self, engine):
        """BUG-07: «подробнее по первой» — карточка показанного, а не новый подбор."""
        engine.handle_text(USER, CHANNEL, "покажи волейбольный мяч")
        out = flat(engine.handle_text(USER, CHANNEL, "подробнее по первой — какой код 1С?"))
        assert "Мяч волейбольный" in out
        assert "Код 1С" in out

    def test_total_question_sums_shown(self, engine):
        """ТЗ BUG-09: «сколько стоит весь комплект?» — сумма, а не новая выдача."""
        engine.handle_text(USER, CHANNEL, "покажи волейбольный мяч")
        out = flat(engine.handle_text(USER, CHANNEL, "сколько стоит весь комплект?"))
        assert "1 200" in out or "1200" in out
        assert "не нашлось" not in out.lower()

    def test_missing_point_named_honestly(self, engine):
        """BUG-09: пункта 2.30.x в 838 нет — честный ответ, а не случайные товары."""
        responses = engine.handle_text(USER, CHANNEL, "покажите пункт 2.30.1 приказа 838")
        out = flat(responses)
        assert "2.30.1" in out
        assert "нет" in out.lower()
        assert not goods(responses)

    def test_point_of_other_document_named(self, engine):
        """BUG-10: 2.18.5 есть только в 838 — бот называет документ, а не выдаёт товары по 1057."""
        responses = engine.handle_text(USER, CHANNEL, "покажите пункт 2.18.5 по приказу 1057")
        out = flat(responses)
        assert "838" in out
        assert not goods(responses)

    def test_section_without_goods_named_honestly(self, engine):
        """Сц. 12: в разделе 1.5.1 (1057) — спортинвентарь; столов там нет, и бот это говорит."""
        out = flat(engine.handle_text(USER, CHANNEL, "столы по разделу 1.5.1 приказа 1057"))
        assert "стол" in out.lower()
        assert "нет" in out.lower()
        assert "Пробирка" not in out and "Кубики" not in out

    def test_unknown_section_named_honestly(self, engine):
        """К2/К3: «по разделу 1.12» — раздела нет в справочнике, слова товара не стираются в мусор."""
        out = flat(engine.handle_text(USER, CHANNEL, "ростомер, весы, кушетка по разделу 1.12"))
        assert "1.12" in out
        assert "нет" in out.lower()
        assert "Пробирка" not in out

    def test_age_subsection_not_substituted(self, engine):
        """BUG-11 (переход №10): «дети 3–7» — ящика «до года» (1.14.2) в выдаче нет."""
        engine.handle_text(USER, CHANNEL, "группа 4–7 лет")
        out = flat(engine.handle_text(USER, CHANNEL, "покажи каталку"))
        assert "Каталка" not in out
        assert "не нашлось" in out.lower() or "нет" in out.lower()

    def test_two_rejections_stop_the_listing(self, engine):
        """К2.6: два «не то» подряд — стоп и менеджер, а не следующая тройка мусора."""
        engine.handle_text(USER, CHANNEL, "что-нибудь для группы")
        engine.handle_text(USER, CHANNEL, "это не то")
        responses = engine.handle_text(USER, CHANNEL, "опять не то")
        assert not goods(responses)
        out = flat(responses)
        assert "менеджер" in out.lower() or engine.settings.manager_contact in out

    def test_more_keeps_task_after_service_question(self, engine):
        """BUG-08: «Показать ещё» после «сколько по времени?» продолжает задачу, а не листает мусор."""
        engine.handle_text(USER, CHANNEL, "мячи для спортзала школы")
        engine.handle_text(USER, CHANNEL, "сколько по времени оформление?")
        out = flat(engine.handle_action(USER, CHANNEL, "select_more"))
        assert "Кресло" not in out and "Ноутбук" not in out

    def test_degradation_not_twice_in_a_row(self, engine, monkeypatch):
        """Этап 1.6: второй раз подряд заглушки не будет — менеджер и телефон."""
        monkeypatch.setattr(dialog_module.intent, "classify", lambda text: "unparsed")
        first = flat(engine.handle_text(USER, CHANNEL, "ну и что делать"))
        second = flat(engine.handle_text(USER, CHANNEL, "вы вообще живые?"))
        assert "недоступен" in first
        assert "недоступен" not in second
        assert engine.settings.manager_contact in second


# --- Этап 0.2: честная метрика QA (зелёные сразу) ----------------------------------------------


class TestQaChecks:
    def test_service_message_alone_is_not_repeat(self):
        turn = Turn(2, "text", "ок", seconds=0.2, messages=[BotMessage(id=1, text="Что дальше?")])
        findings = check_turn(turn, CatalogFacts([]), ["Что дальше?"])
        assert not any(f.code == "REPEAT" for f in findings)

    def test_substantive_repeat_still_flagged(self):
        text = "Ничего не нашёл по этому запросу. Попробуйте назвать товар иначе или указать пункт приказа."
        turn = Turn(2, "text", "мячи", seconds=0.3, messages=[BotMessage(id=1, text=text)])
        findings = check_turn(turn, CatalogFacts([]), [text])
        assert any(f.code == "REPEAT" for f in findings)

    def test_degradation_flagged(self):
        text = "Сейчас я отвечаю проще обычного — консультант временно недоступен. Могу показать каталог."
        turn = Turn(1, "text", "дорого", seconds=0.4, messages=[BotMessage(id=1, text=text)])
        findings = check_turn(turn, CatalogFacts([]), [])
        assert any(f.code == "DEGRADED" for f in findings)
        assert not any(f.code == "FALLBACK_OFFER" for f in findings)

    def test_fallback_offer_flagged_only_when_instant(self):
        header = "Могу предложить товары из каталога — вот 3 из 12\n• Мяч баскетбольный № 3\n• Мат детский"
        fast = Turn(1, "text", "покажи что есть", seconds=0.4, messages=[BotMessage(id=1, text=header)])
        slow = Turn(1, "text", "покажи что есть", seconds=30.0, messages=[BotMessage(id=1, text=header)])
        assert any(f.code == "FALLBACK_OFFER" for f in check_turn(fast, CatalogFacts([]), []))
        assert not any(f.code == "FALLBACK_OFFER" for f in check_turn(slow, CatalogFacts([]), []))

    def test_sku_in_backticks_is_known(self):
        product = SimpleNamespace(sku_1c="0Э-00006646", name="Стеллаж", price=5000)
        text = "Стеллаж металлический. Код 1С: `0Э-00006646`, цена 5 000 ₽."
        turn = Turn(1, "text", "стеллаж", messages=[BotMessage(id=1, text=text)])
        findings = check_turn(turn, CatalogFacts([product]), [])
        assert not any(f.code == "UNKNOWN_SKU" for f in findings)

    def test_code_inside_name_is_not_a_point(self):
        facts = CatalogFacts([], {"1.5.1.7"})
        listing = "1. Набор «7.71.2» лабораторный — 500 ₽ — в наличии\n2. Мат гимнастический — 8 164 ₽ — 1.5.1.7"
        turn = Turn(1, "text", "покажи", messages=[BotMessage(id=1, text=listing)])
        assert not any(f.code == "UNKNOWN_POINT" for f in check_turn(turn, facts, []))

    def test_card_title_code_is_not_a_point(self):
        facts = CatalogFacts([], {"1.5.1.7"})
        text = "Кукла (крупного размера) 7.71.2\nКод 1С: KL-1\nЦена 900 ₽\nПозиция 1.5.1.7"
        turn = Turn(1, "text", "подробнее", messages=[BotMessage(id=1, text=text)])
        assert not any(f.code == "UNKNOWN_POINT" for f in check_turn(turn, facts, []))

    def test_unknown_point_still_flagged(self):
        facts = CatalogFacts([], {"1.5.1.7"})
        text = "По пункту 2.30.1 нашлись позиции:"
        turn = Turn(1, "text", "2.30.1", messages=[BotMessage(id=1, text=text)])
        assert any(f.code == "UNKNOWN_POINT" for f in check_turn(turn, facts, []))


# --- Этапы 2Б, 2.12: настоящий каталог (метка catalog) ------------------------------------------


@pytest.mark.skipif(not KB.exists(), reason="реального каталога data/kb нет")
class TestRealCatalog:
    def engine(self, tmp_path):
        settings = Settings()
        runtime = CatalogRuntime.open(settings.kb_path)
        storage = Storage(tmp_path / "real.sqlite3")
        orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
        engine = DialogEngine(runtime, storage, orders, settings)
        build_core(settings, engine)
        return engine

    def test_logoped_room_shows_real_goods(self, tmp_path):
        """К9: «оснастить кабинет логопеда в детском саду» — товары со склада, а не заготовки."""
        engine = self.engine(tmp_path)
        responses = engine.handle_text(USER, CHANNEL, "оснастить кабинет логопеда в детском саду")
        cards = [card for r in responses if isinstance(r, ProductList) for card in r.cards]
        assert cards, "выдачи нет"
        assert all(card.product.in_stock for card in cards), "показаны позиции без наличия (заготовки)"

    def test_classifier_lost_but_catalog_subject_still_searched(self, tmp_path):
        """Прогон 05.10 (СЦ2): «тактильные дорожки 3 шт. срок 4 недели» классификатор
        не понял — запасной путь видит предмет в словаре каталога и ищет, а не
        отвечает «проще обычного»."""
        engine = self.engine(tmp_path)
        out = flat(engine.handle_text(USER, CHANNEL, "тактильные дорожки 3 шт. срок 4 недели"))
        assert "проще обычного" not in out.lower()

    def test_chatter_without_subject_still_degrades(self, tmp_path):
        """Обратная сторона словарного гейта: болтовня без предметов каталога —
        прежняя заглушка, а не случайная выдача."""
        engine = self.engine(tmp_path)
        out = flat(engine.handle_text(USER, CHANNEL, "а можно скидку посерьёзнее?"))
        assert "недоступен" in out.lower()

    def test_group_room_shows_real_goods(self, tmp_path):
        """К9: «оснастить группу 3–4 лет» — настоящий товар групповой комнаты."""
        engine = self.engine(tmp_path)
        responses = engine.handle_text(USER, CHANNEL, "оснастить группу 3–4 лет")
        cards = [card for r in responses if isinstance(r, ProductList) for card in r.cards]
        assert cards, "выдачи нет"
        assert all(card.product.in_stock for card in cards), "показаны позиции без наличия (заготовки)"

    def test_point_beats_room_filter(self, tmp_path):
        """К9.4: названный пункт сильнее помещения в профиле (1.13.3.3.43 — пирамидки)."""
        engine = self.engine(tmp_path)
        engine.handle_text(USER, CHANNEL, "оснастить кабинет логопеда в детском саду")
        out = flat(engine.handle_text(USER, CHANNEL, "покажите пункт 1.13.3.3.43"))
        assert "ирамид" in out.lower()
        assert "не нашлось" not in out.lower()
