"""Этап 1 (К1): модель доступна, ход короткий.

Прогон 04.10: один медленный ответ выключал единственного провайдера всем на
пять минут, ход уходил в запасной подбор минуя сервисные ветки ядра, а ответы
тянулись до 147 секунд. Здесь проверено: пауза по типу сбоя, фоновый пингер,
сбой маршрутизатора без блокировки, бюджет хода, запасная модель, сервисные
ветки без модели и разные ответы на «спасибо»/«вы бот?».
"""

from __future__ import annotations

import time

import pytest

from agent.agent import SalesAgent
from agent.client import (
    LLMAuthError,
    LLMError,
    LLMPaymentError,
    LLMRateLimitError,
    LLMTimeoutError,
    without_reasoning,
)
from agent.providers import (
    AUTH_COOLDOWN_SECONDS,
    PAYMENT_COOLDOWN_SECONDS,
    RATE_COOLDOWN_SECONDS,
    SERVER_COOLDOWN_SECONDS,
    TIMEOUT_COOLDOWN_SECONDS,
    LLMRouter,
    _pause_for,
    build_router,
)
from agent.routing import Orchestrator
from catalog.models import Product
from catalog.search import CatalogIndex
from core.config import Settings
from core.dialog import DialogEngine, Session
from core.storage import Storage
from core_api.composition import build_core
from norms.repository import FileNormRepository
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"
USER = "u1"


class FakeClient:
    """Провайдер с заданным поведением: падает один раз, отвечает или молчит."""

    def __init__(self, name: str, answer: str = "готово", fail: Exception | None = None):
        self.name = name
        self.model = "fake-model"
        self.host = "fake"
        self.reasoning_in_history = True
        self.answer = answer
        self.fail = fail
        self.calls = 0

    def complete(self, messages, tools=None, temperature=0.3, max_tokens=None, timeout=None):  # noqa: ANN001, ANN202
        self.calls += 1
        if self.fail is not None:
            fail, self.fail = self.fail, None  # падает только первый вызов
            raise fail
        return {"role": "assistant", "content": self.answer}


# --- Пауза по типу сбоя -------------------------------------------------------------------------


def test_pause_by_failure_type():
    """Таймаут — полминуты, 5xx — минута, 402 — пять минут, ключ — полчаса, 429 — минута."""
    table = [
        (LLMTimeoutError("не ответил"), TIMEOUT_COOLDOWN_SECONDS),
        (LLMError("500 ошибка"), SERVER_COOLDOWN_SECONDS),
        (LLMPaymentError("нет денег"), PAYMENT_COOLDOWN_SECONDS),
        (LLMAuthError("не тот ключ"), AUTH_COOLDOWN_SECONDS),
        (LLMRateLimitError("429", 2.0), RATE_COOLDOWN_SECONDS),
    ]
    for exc, base in table:
        pause = _pause_for(exc)
        assert base * 0.79 <= pause <= base * 1.21, f"{type(exc).__name__}: {pause:.0f} с"


def test_timeout_does_not_kill_provider_for_five_minutes():
    router = LLMRouter(clients=[FakeClient("cloudru")])
    router.mark_down(router.clients[0], LLMTimeoutError("не ответил за 30 с"))
    blocked = router.blocked_for(router.clients[0])
    assert 0 < blocked <= TIMEOUT_COOLDOWN_SECONDS * 1.21
    # Пока блок — недоступен; спустя паузу провайдер снова в строю (полуоткрытая проверка).
    assert not router.available
    router._blocked_until["cloudru"] = time.monotonic() - 1.0
    assert router.available


def test_background_pinger_releases_block():
    client = FakeClient("cloudru", fail=LLMTimeoutError("первый пинг мимо"))
    router = LLMRouter(clients=[client])
    router.mark_down(client, LLMTimeoutError("таймаут хода"))
    router.start_pinger(interval=0.05)
    try:
        deadline = time.monotonic() + 3.0
        while router.blocked_for(client) > 0.0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert router.blocked_for(client) == 0.0, "пингер не снял блок"
        assert client.calls >= 2, "пингер не перепроверил провайдера"
    finally:
        router.stop_pinger()


# --- Маршрутизатор не блокирует провайдера -------------------------------------------------------


def test_router_failure_does_not_block_provider():
    """К1: сбой короткого вызова маршрутизатора — не повод выключать модель всем."""
    router = LLMRouter(clients=[FakeClient("cloudru", fail=LLMError("500 от маршрутизатора"))])
    orchestrator = Orchestrator(router)
    session = Session(user_id=USER, channel=CHANNEL)
    orchestrator.decide(session, "абракадабра синтаксис необъяснимый")
    assert router.status()[0]["blocked_for"] == 0.0, "маршрутизатор заблокировал провайдера"


# --- Бюджет хода ---------------------------------------------------------------------------------


def test_call_timeout_from_budget():
    agent = SalesAgent.__new__(SalesAgent)
    assert agent._call_timeout(None, 30.0) == 30.0
    assert agent._call_timeout(time.monotonic() + 100, 30.0) == 30.0
    rest = agent._call_timeout(time.monotonic() + 10, 30.0)
    assert 8.0 < rest <= 10.0
    with pytest.raises(LLMTimeoutError):
        agent._call_timeout(time.monotonic() - 1, 30.0)


# --- Запасная модель у того же провайдера --------------------------------------------------------


def test_fallback_model_joins_router():
    settings = Settings(
        cloudru_api_key="ключ-не-настоящий",
        cloudru_model="deepseek-ai/DeepSeek-V4-Flash",
        cloudru_fallback_model="Qwen/Qwen3.7",
    )
    router = build_router(settings)
    assert [c.name for c in router.clients] == ["cloudru", "cloudru-qwen"]
    qwen = router.clients[1]
    assert qwen.model == "Qwen/Qwen3.7"
    assert not qwen.reasoning_in_history, "Qwen не должен получать reasoning_content"


def test_fallback_model_absent_by_default():
    router = build_router(Settings(cloudru_api_key="ключ-не-настоящий"))
    assert [c.name for c in router.clients] == ["cloudru"]


def test_blocked_primary_leaves_fallback_ready():
    """Пауза основной модели не выключает весь роутер: ход берёт запасная."""
    primary = FakeClient("cloudru")
    fallback = FakeClient("cloudru-qwen", answer="запасной ответ")
    router = LLMRouter(clients=[primary, fallback])
    router.mark_down(primary, LLMTimeoutError("таймаут"))
    assert router.ready() == [fallback]


def test_history_stripped_of_reasoning_for_models_without_it():
    messages = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "ответ", "reasoning_content": "мысли"},
        {"role": "tool", "tool_call_id": "x", "content": "результат"},
    ]
    cleaned = without_reasoning(messages)
    assert cleaned[1] == {"role": "assistant", "content": "ответ"}
    assert cleaned[0] == messages[0] and cleaned[2] == messages[2]


# --- Сервисные ветки работают без модели ---------------------------------------------------------


def make_engine(tmp_path):
    index = CatalogIndex(
        [
            Product.from_dict(
                {
                    "sku_1c": "S1",
                    "name": "Фрезерный станок с ЧПУ",
                    "price": 253000,
                    "currency": "RUB",
                    "in_stock": 1,
                    # Раздел кабинета нужен ядру подбора: фильтр помещения без него товар отсекает.
                    "category_paths": [
                        [
                            "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838",
                            "Раздел 2. Комплекс оснащения предметных кабинетов",
                            "Подраздел 20. Кабинет технологии",
                        ]
                    ],
                    "description": "",
                    "kit_contents": [],
                    "norms": [
                        {
                            "doc_id": "order_838",
                            "doc_citation": "приказ Минпросвещения России от 28.11.2024 № 838",
                            "item_code": "2.20.63",
                            "item_title": None,
                            "source": "heading",
                            "confidence": 0.9,
                        }
                    ],
                    "bitrix_id": None,
                    "url": "https://vdm.ru/s1",
                    "short_url": None,
                }
            )
        ]
    )
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
    engine = DialogEngine(index, storage, orders, settings)
    # Подбор в диалоге идёт через Procurement Core: без него offer живёт на индексе.
    build_core(settings, engine, norms=FileNormRepository())
    return engine


@pytest.fixture
def agent_without_model(tmp_path):
    engine = make_engine(tmp_path)
    dead = FakeClient("cloudru", fail=LLMTimeoutError("не отвечает"))
    router = LLMRouter(clients=[dead])
    router.mark_down(dead, LLMTimeoutError("таймаут хода"))
    return SalesAgent(engine, router), engine


def test_export_asked_by_text_works_without_model(agent_without_model):
    """«Сохрани список в excel» при лежащей модели — ядро строит файл, а не заглушка (К1)."""
    agent, engine = agent_without_model
    engine.handle_text(USER, CHANNEL, "покажи фрезерный станок")  # список для выгрузки
    session = engine.session(USER, CHANNEL)
    out = agent.reply(session, "сохрани этот список в excel")
    text = "\n".join(getattr(r, "text", "") for r in out)
    assert "Excel" in text or "excel" in text
    assert "проще обычного" not in text


def test_checkout_by_text_works_without_model(agent_without_model):
    """«Оформить» при лежащей модели ведёт в оформление, а не в подбор без модели."""
    agent, engine = agent_without_model
    engine.handle_action(USER, CHANNEL, "add:S1")
    session = engine.session(USER, CHANNEL)
    out = agent.reply(session, "оформить заказ")
    text = "\n".join(getattr(r, "text", "") for r in out)
    assert "персональных данных" in text
    assert "не нашлось" not in text


def test_blocked_provider_route_is_logged(agent_without_model):
    """Журнал хода (1.7): видно, почему модель не звали, и сколько провайдер простоит."""
    agent, engine = agent_without_model
    session = engine.session(USER, CHANNEL)
    agent.reply(session, "нужны столы для школы")
    assert session.route.get("llm_skipped") in {"provider_blocked", "service_branch"}
    if session.route.get("llm_skipped") == "provider_blocked":
        assert session.route.get("provider_blocked_for")


# --- Честная деградация и small talk -------------------------------------------------------------


@pytest.fixture
def plain_engine(tmp_path):
    return make_engine(tmp_path)


def test_small_talk_variants(plain_engine):
    thanks = plain_engine.handle_text(USER, CHANNEL, "спасибо")[0]
    identity = plain_engine.handle_text(USER, CHANNEL, "ты бот или человек?")[0]
    hello = plain_engine.handle_text(USER, CHANNEL, "привет")[0]
    assert "Пожалуйста" in thanks.text
    assert "Элтик" in identity.text
    assert "Здравствуйте" in hello.text
    assert len({thanks.text, identity.text, hello.text}) == 3, "один ответ на всё"


def test_degradation_then_manager_instead_of_repeat(plain_engine):
    """Вторая заглушка подряд не приходит: менеджер и телефон вместо «временно недоступен»."""
    first = plain_engine.handle_text(USER, CHANNEL, "а почему так вышло?")[0]
    second = plain_engine.handle_text(USER, CHANNEL, "ну так что?")[0]
    assert "недоступен" in first.text
    assert "недоступен" not in second.text
    assert plain_engine.settings.manager_contact in second.text


def test_catalog_reply_ends_degradation_streak(plain_engine):
    plain_engine.handle_text(USER, CHANNEL, "а почему так вышло?")
    plain_engine.handle_text(USER, CHANNEL, "ну так что?")  # менеджер вместо второй заглушки
    plain_engine.handle_text(USER, CHANNEL, "покажи фрезерный станок")  # подбор — выход из деградации
    third = plain_engine.handle_text(USER, CHANNEL, "а почему снова так?")[0]
    assert "недоступен" in third.text, "после удачного ответа заглушка снова уместна"
