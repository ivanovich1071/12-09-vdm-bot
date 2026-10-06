"""Сегментация «частник/организация» и гейт приказов (вопросы 10–12 опросного листа).

Оба отдела дали один текст: физлицам — польза товара простым языком, учреждениям —
приказы как показатель экспертности. Прямой вопрос «вы для себя или для организации?»
не задаём (против прямо возражала Бабкова): сегмент определяем по словам реплик.
"""


from catalog.models import Product
from catalog.search import CatalogIndex
from core.config import Settings
from core.dialog import DialogEngine
from core.profile import DialogProfile, client_kind_of
from core.storage import Storage
from core.ui import ProductCard
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"
USER = "u1"


def product(sku, name, price=1000, norms=("2.20.63",)):
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "price": price,
            "currency": "RUB",
            "in_stock": 3,
            "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"]],
            "description": "",
            "kit_contents": [],
            "norms": [
                {
                    "doc_id": "order_838",
                    "doc_citation": "приказ Минпросвещения России от 28.11.2024 № 838",
                    "item_code": code,
                    "item_title": None,
                    "source": "heading",
                    "confidence": 0.9,
                }
                for code in norms
            ],
            "bitrix_id": None,
            "url": f"https://vdm.ru/{sku}",
            "short_url": None,
        }
    )


def engine_for(tmp_path, *products):
    index = CatalogIndex(list(products))
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
    return DialogEngine(index, storage, orders, settings)


def test_client_kind_by_words():
    assert client_kind_of("ищу машинку ребенку 3 лет") == "person"
    assert client_kind_of("нужен подарок сыну, он на день рождения") == "person"
    assert client_kind_of("нужно оснастить группу в детском саду") == "org"
    assert client_kind_of("подберите мебель по приказу 838") == "org"
    assert client_kind_of("нужна сенсорная лампа") == "unknown"
    assert client_kind_of("") == "unknown"


def test_profile_remembers_and_updates_kind():
    profile = DialogProfile()
    profile.update_from_text("ищу машинку ребенку на день рождения")
    assert profile.client_kind == "person"
    # Сегмент меняется, когда человек сам объяснил задачу точнее.
    profile.update_from_text("вообще-то это для группы детского сада")
    assert profile.client_kind == "org"


def test_institution_means_org_even_without_other_signals():
    profile = DialogProfile()
    profile.update_from_text("мы частный детский сад, оснастить нужно игровую")
    assert profile.client_kind == "org"


def test_person_cards_hide_norm_grounds(tmp_path):
    """Вопрос 10: физлицу — польза товара, ни 838, ни пунктов."""
    engine = engine_for(tmp_path, product("S1", "Мяч баскетбольный"))
    engine.handle_text(USER, CHANNEL, "ищу мяч ребенку в подарок")
    card = [r for r in engine.handle_action(USER, CHANNEL, "card:S1") if isinstance(r, ProductCard)][0]
    assert card.citation is None
    assert card.norms == []


def test_org_cards_keep_norm_grounds(tmp_path):
    engine = engine_for(tmp_path, product("S1", "Мяч баскетбольный"))
    engine.handle_text(USER, CHANNEL, "оснащаем спортивный зал школы")
    card = [r for r in engine.handle_action(USER, CHANNEL, "card:S1") if isinstance(r, ProductCard)][0]
    assert card.citation is not None or card.norms


def test_prompt_tells_the_model_about_the_segment():
    profile = DialogProfile()
    profile.update_from_text("ищу подарок ребенку")
    prompt = profile.as_prompt()
    assert "частное лицо" in prompt
    assert "не упоминай" in prompt

    org = DialogProfile()
    org.update_from_text("оснащаем школу, нужен кабинет физики")
    assert "учреждение" in org.as_prompt()
