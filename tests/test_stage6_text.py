"""Этап 6 (К6): текст, карточки и характеристики не расходятся.

- названные пункты, которых нет в каталоге, не добиваются «товарами того же
  раздела» (столы 2.14.1–2.14.3 → термометры, прогон 04.10, сц. 13);
- код 1С в бэктиках и кавычках вырезается из ответа (BUG-13);
- ответ, урезанный проверкой до одной служебной ноты, не отправляется;
- пустая подстановка «По вашему запросу «»» не возвращается (переход №22).
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.agent import MIN_KEPT, SalesAgent, _substantial, _without_codes
from catalog.models import Product

CHANNEL = "telegram"
USER = "u1"


def _product(sku: str, name: str) -> Product:
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "price": 1000,
            "currency": "RUB",
            "in_stock": 1,
            "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"]],
            "description": "",
            "kit_contents": [],
            "norms": [],
            "bitrix_id": None,
            "url": f"https://vdm.ru/{sku}",
            "short_url": None,
        }
    )


def _tools(skus: dict[str, str]) -> tuple[SimpleNamespace, SimpleNamespace]:
    products = {sku: _product(sku, name) for sku, name in skus.items()}
    agent = SimpleNamespace(
        engine=SimpleNamespace(index=SimpleNamespace(get=products.get)),
        # «упомянутые товары» в ответе — нет: проверяем ветку дописывания каталога.
        _mentioned_skus=lambda tools, answer: [],
    )
    tools = SimpleNamespace(shown_skus=list(products), prices=set(), norm_refs=set())
    return agent, tools


def test_absent_points_are_not_backfilled_with_section_goods():
    """К6.1: под «по пунктам 2.14.1-2.14.3 столов нет» не приходят термометры."""
    agent, tools = _tools(
        {
            "T1": "Термометр лабораторный",
            "S1": "1.14.9.1 Стол иного раздела",
        }
    )
    answer = "Лабораторные столы — это пункты 2.14.1, 2.14.2 и 2.14.3 приказа 838."
    # _with_catalog_positions — метод агента: зовём через класс с заглушкой self.
    text = SalesAgent._with_catalog_positions(agent, tools, answer)
    assert "Термометр" not in text, "чужой раздел дописан под текстом о других пунктах"
    assert "2.14.1" in text and "товаров в каталоге нет" in text


def test_exact_points_still_backfilled():
    """Точные совпадения пунктов по-прежнему дополняются списком тех же позиций."""
    agent, tools = _tools({"A1": "1.14.5.1.1 Ковёр детский", "A4": "1.14.2.2.3 Ящик"})
    answer = "По пункту 1.14.5.1.1 — ковёр."
    text = SalesAgent.__dict__["_with_catalog_positions"](agent, tools, answer)
    assert "Ковёр детский" in text
    assert "Ящик" not in text


def test_code_in_backticks_is_cut():
    """К6.2: «Код 1С: `0Э-00006646`» не доходит до клиента служебным кодом."""
    text = _without_codes("Стеллаж. Код 1С: `0Э-00006646`, цена 5 000 ₽.")
    assert "0Э-00006646" not in text
    assert "Стеллаж" in text


def test_code_in_quotes_is_cut():
    text = _without_codes("Мат детский (артикул «Д-214»), 8 164 ₽.")
    assert "Д-214" not in text
    assert "Мат детский" in text


def test_thin_answer_after_verification_is_not_sent():
    """К6.3: приветствие без содержания не проходит как «подтверждённый» ответ."""
    assert not _substantial("Здравствуйте! Я консультант ЭЛТИ-КУДИЦ. Чем помочь?")
    assert _substantial("Мат детский — 8 164 ₽, в наличии.")
    assert _substantial("По пункту 1.5.1 нашлись позиции:\n• Мяч")


def test_min_kept_threshold_unchanged():
    assert MIN_KEPT >= 40
