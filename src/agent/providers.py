"""Выбор провайдера модели.

У прототипа их два, и это не роскошь. Cloud.ru — то, где всё будет работать
у заказчика: российское облако, российская площадка, оплата в рублях. OpenRouter —
то, где диалог можно проверить прямо сейчас: с машины разработки Cloud.ru не всегда
резолвится, и без запасного пути любая проверка упирается в сеть, а не в код.

Порядок приоритетов задаётся настройкой. Отказ провайдера — не конец разговора:
пробуем следующего, и только когда легли все, ядро отвечает поиском по каталогу.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field

from agent.client import (
    ChatClient,
    LLMAuthError,
    LLMError,
    LLMPaymentError,
    LLMRateLimitError,
    LLMTimeoutError,
)

log = logging.getLogger(__name__)

# Прогон 04.10: один общий cooldown на все сбои (300 с) выключал единственного
# провайдера всем пользователям из-за одного медленного ответа. Теперь пауза
# зависит от типа сбоя.
# Таймаут — не поломка: модель ответила медленно один раз. Полминуты достаточно,
# чтобы не долбить лежачего, но не оставить людей без модели на пять минут.
TIMEOUT_COOLDOWN_SECONDS = 30.0
# 5xx и прочие сбои провайдера — минута: сервисы сами поднимаются быстрее.
SERVER_COOLDOWN_SECONDS = 60.0
# Отказ по деньгам (402) чинится пополнением счёта без нас. Пока эти случаи жили
# под одним сроком с «не тот ключ», бот после пополнения Cloud.ru ещё полчаса
# разговаривал запасной моделью — поймано на прогоне 02.09.
PAYMENT_COOLDOWN_SECONDS = 300.0
# Отказ по ключу сам не пройдёт — тут нужен человек, а не повтор.
AUTH_COOLDOWN_SECONDS = 1800.0
# Лимит частоты (429) — не поломка, а очередь: Cloud.ru даёт 500 запросов в минуту,
# и один сложный ход её исчерпывает (23.09, сц. 5). Достаточно минуты.
RATE_COOLDOWN_SECONDS = 60.0
# Джиттер ±20%, чтобы параллельные ходы не снимали cooldown одновременно.
_JITTER = (0.8, 1.2)
# Как часто фоновый пингер проверяет заблокированного провайдера: он снимает
# блок сам, не дожидаясь живого клиента, которому иначе пришлось бы ждать.
PING_INTERVAL_SECONDS = 20.0


def _pause_for(exc: Exception) -> float:
    if isinstance(exc, LLMRateLimitError):
        return RATE_COOLDOWN_SECONDS * random.uniform(*_JITTER)
    if isinstance(exc, LLMPaymentError):
        return PAYMENT_COOLDOWN_SECONDS * random.uniform(*_JITTER)
    if isinstance(exc, LLMAuthError):
        return AUTH_COOLDOWN_SECONDS
    if isinstance(exc, LLMTimeoutError):
        return TIMEOUT_COOLDOWN_SECONDS * random.uniform(*_JITTER)
    return SERVER_COOLDOWN_SECONDS * random.uniform(*_JITTER)


@dataclass
class LLMRouter:
    clients: list[ChatClient] = field(default_factory=list)
    _blocked_until: dict[str, float] = field(default_factory=dict, init=False)
    _last_error: dict[str, str] = field(default_factory=dict, init=False)
    _pinger: threading.Thread | None = field(default=None, init=False, repr=False)
    _pinger_stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    @property
    def configured(self) -> bool:
        return bool(self.clients)

    @property
    def available(self) -> bool:
        """Есть ли хоть один провайдер, к которому сейчас можно обратиться."""
        return bool(self.ready())

    def ready(self) -> list[ChatClient]:
        now = time.monotonic()
        return [c for c in self.clients if self._blocked_until.get(c.name, 0.0) <= now]

    def blocked_for(self, client: ChatClient) -> float:
        """Сколько провайдер ещё простоит в блоке — для журнала хода."""
        return max(0.0, self._blocked_until.get(client.name, 0.0) - time.monotonic())

    def mark_down(self, client: ChatClient, exc: Exception) -> None:
        pause = _pause_for(exc)
        self._blocked_until[client.name] = time.monotonic() + pause
        self._last_error[client.name] = str(exc)[:300]
        log.warning(
            "Провайдер %s отключён на %.0f с: %s", client.name, pause, str(exc)[:200]
        )

    def mark_up(self, client: ChatClient) -> None:
        self._blocked_until.pop(client.name, None)
        self._last_error.pop(client.name, None)

    def start_pinger(self, interval: float = PING_INTERVAL_SECONDS) -> None:
        """Фоновая проверка заблокированных провайдеров.

        Пока проверка не шла, провайдер, отпущенный cooldown'ом, оставался
        выключенным до первого живого клиента — а тот платил за проверку
        собственным ожиданием. Здесь отказ стоит копеечного пинга в фоне.
        """
        if self._pinger is not None and self._pinger.is_alive():
            return

        def loop() -> None:
            while not self._pinger_stop.wait(interval):
                for client in list(self.clients):
                    if self.blocked_for(client) <= 0.0:
                        continue
                    try:
                        client.complete(
                            [{"role": "user", "content": "пинг"}], temperature=0.0, max_tokens=1
                        )
                    except LLMError as exc:
                        self.mark_down(client, exc)
                        continue
                    self.mark_up(client)
                    log.info("Провайдер %s снова отвечает (проверка в фоне).", client.name)

        self._pinger_stop.clear()
        self._pinger = threading.Thread(target=loop, name="llm-pinger", daemon=True)
        self._pinger.start()

    def stop_pinger(self) -> None:
        self._pinger_stop.set()

    def status(self) -> list[dict[str, object]]:
        """Состояние провайдеров для логов и диагностики."""
        now = time.monotonic()
        return [
            {
                "name": c.name,
                "model": c.model,
                "host": c.host,
                "blocked_for": max(0.0, self._blocked_until.get(c.name, 0.0) - now),
                "last_error": self._last_error.get(c.name),
            }
            for c in self.clients
        ]


def build_router(settings) -> LLMRouter:  # noqa: ANN001 — core.config.Settings
    """Собирает список провайдеров в порядке, заданном настройкой LLM_PROVIDER.

    Запасная модель живёт у того же провайдера, что и основная (тот же ключ и
    адрес, второе имя модели — `CLOUDRU_FALLBACK_MODEL`). Блокировка ведётся по
    имени клиента, поэтому пауза у основной модели не выключает запасную: ход
    сразу берёт она, а не путь без модели (прогон 04.10, К1).
    """
    clients: dict[str, ChatClient] = {}

    if settings.cloudru_api_key:
        clients["cloudru"] = ChatClient(
            api_key=settings.cloudru_api_key,
            base_url=settings.cloudru_base_url,
            model=settings.cloudru_model,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            name="cloudru",
            price_in=settings.cloudru_price_in,
            price_out=settings.cloudru_price_out,
        )
        if settings.cloudru_fallback_model:
            clients["cloudru-qwen"] = ChatClient(
                api_key=settings.cloudru_api_key,
                base_url=settings.cloudru_base_url,
                model=settings.cloudru_fallback_model,
                timeout=settings.llm_timeout_seconds,
                max_tokens=settings.llm_max_tokens,
                name="cloudru-qwen",
                price_in=settings.cloudru_fallback_price_in,
                price_out=settings.cloudru_fallback_price_out,
                # Поле reasoning_content — требование валидатора DeepSeek; другие
                # модели оно может не принять, поэтому запасной не отправляем его.
                reasoning_in_history=False,
            )
    if settings.openrouter_api_key:
        clients["openrouter"] = ChatClient(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            model=settings.openrouter_model,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            name="openrouter",
            price_in=settings.openrouter_price_in,
            price_out=settings.openrouter_price_out,
            extra_headers={
                "HTTP-Referer": settings.site_url,
                "X-Title": "ELTI-KUDITS catalog bot",
            },
        )

    order = ["cloudru", "cloudru-qwen", "openrouter"]
    if settings.llm_provider != "auto":
        order = [settings.llm_provider]
        if settings.llm_provider == "cloudru" and "cloudru-qwen" in clients:
            order.append("cloudru-qwen")
    chosen = [clients[name] for name in order if name in clients]
    if not chosen:
        log.warning(
            "Ни один провайдер модели не настроен (LLM_PROVIDER=%s): "
            "бот отвечает поиском по каталогу.",
            settings.llm_provider,
        )
    else:
        log.info("Провайдеры модели: %s", ", ".join(f"{c.name}/{c.model}" for c in chosen))
    return LLMRouter(clients=chosen)


def warm_up(router: LLMRouter) -> None:
    """Короткий вызов каждому провайдеру при запуске.

    Без него первый живой человек платит за проверку связи собственным
    ожиданием: если Cloud.ru не открывается, его таймаут в шестьдесят секунд
    достаётся первому же сообщению, и только потом ход уходит запасному.
    Здесь тот же отказ стоит времени запуска, а не времени пользователя.

    Ответ нам не нужен — важно, поднимется ошибка или нет.
    """
    for client in list(router.clients):
        try:
            client.complete(
                [{"role": "user", "content": "пинг"}], temperature=0.0, max_tokens=1
            )
        except LLMError as exc:
            router.mark_down(client, exc)
            continue
        router.mark_up(client)
        log.info("Провайдер %s отвечает (%s).", client.name, client.model)


__all__ = [
    "LLMRouter",
    "build_router",
    "warm_up",
    "LLMError",
    "LLMAuthError",
    "LLMPaymentError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "TIMEOUT_COOLDOWN_SECONDS",
    "SERVER_COOLDOWN_SECONDS",
    "PAYMENT_COOLDOWN_SECONDS",
    "AUTH_COOLDOWN_SECONDS",
    "RATE_COOLDOWN_SECONDS",
]
