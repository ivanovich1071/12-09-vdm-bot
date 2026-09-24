"""Клиент модели по протоколу OpenAI.

Один и тот же класс работает и с Cloud.ru Foundation Models, и с OpenRouter: оба
принимают `/v1/chat/completions` с вызовом инструментов. Отличаются адресом, ключом,
названием модели и парой заголовков — всё это поля, а не отдельный код.

Зависимость от пакета `openai` здесь не нужна: обращение через стандартную библиотеку
избавляет прототип от лишних зависимостей и делает поведение при таймаутах
и ошибках предсказуемым.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Модель не ответила. Диалог должен продолжиться без неё, а не оборваться."""


class LLMAuthError(LLMError):
    """Ключ не принят или доступ закрыт. Повторять запрос бессмысленно."""


class LLMPaymentError(LLMAuthError):
    """На счету провайдера нет средств.

    От «не тот ключ» отличается тем, что чинится без нас: заказчик пополняет
    счёт, и провайдер оживает сам. Поэтому отказ по деньгам держит провайдера
    в стороне пять минут, а не полчаса, — иначе после пополнения бот ещё
    полчаса разговаривает запасной моделью.
    """


class LLMRateLimitError(LLMError):
    """Провайдер ограничил частоту обращений (429).

    На прогоне 23.09 (сц. 5) лимит Cloud.ru — 500 запросов в минуту — исчерпывал
    один сложный ход, и 5-минутный cooldown уходил пользователю фразой «консультант
    временно недоступен». Быстрый повтор после паузы обычно проходит.
    """

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Пауза быстрого повтора после 429: ждём Retry-After, но не дольше пяти секунд —
# дальше ход становится долгим ожиданием для человека.
QUICK_RETRY_AFTER = 5.0


@dataclass
class ChatClient:
    api_key: str
    base_url: str = "https://foundation-models.api.cloud.ru/v1"
    model: str = "deepseek-ai/DeepSeek-V4-Flash"
    timeout: float = 60.0
    # 2000 обрезали ответы посреди списка («1. Минимальный бюджет» и стоп, 15-17.09).
    max_tokens: int = 4000
    # Как провайдер называется в логах и в диагностике: «cloudru», «openrouter».
    name: str = "cloudru"
    # OpenRouter просит указать, откуда пришёл запрос; Cloud.ru лишние заголовки
    # игнорирует, поэтому отдельной ветки в коде не нужно.
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Рубли за миллион токенов — из прайса провайдера. Нужны, чтобы в журнале
    # стояла стоимость хода, а не только их количество: выбирать модель по
    # ощущению «эта пободрее» дорого, а по цифрам — нет.
    price_in: float = 0.0
    price_out: float = 0.0

    @property
    def host(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self.base_url).hostname or ""

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            # Маршрутизатору нужен короткий JSON, а не две тысячи токенов:
            # предел задаётся вызовом, иначе за него платим впустую.
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        body: dict[str, Any] | None = None
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                message = f"{self.name} вернул {exc.code}: {detail}"
                # 401 — не тот ключ, 403 — ключу закрыт доступ: нужен человек.
                # 402 — кончились деньги: чинится пополнением, ждать полчаса незачем.
                # 429 — лимит частоты: один быстрый повтор по Retry-After, и только
                # после него провайдер уходит в короткий cooldown (23.09, сц. 5).
                if exc.code == 429:
                    if attempt == 1:
                        pause = min(_retry_after(exc.headers), QUICK_RETRY_AFTER)
                        log.warning("%s: лимит запросов, повтор через %.1f с", self.name, pause)
                        time.sleep(pause)
                        continue
                    raise LLMRateLimitError(message, _retry_after(exc.headers)) from exc
                if exc.code == 402:
                    raise LLMPaymentError(message) from exc
                if exc.code in {401, 403}:
                    raise LLMAuthError(message) from exc
                raise LLMError(message) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise LLMError(f"{self.name} недоступен: {exc}") from exc
        if body is None:
            raise LLMError(f"{self.name}: ответа нет")
        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name}: пустой ответ модели")

        # Расход провайдер сообщает в каждом ответе, а мы его выбрасывали — и
        # посчитать, во сколько обходится один разговор, было нечем. Кладём его
        # в само сообщение под служебным ключом: сигнатура метода не меняется,
        # а обратно провайдеру такое сообщение не уходит — там пересобирается
        # только то, что он прислал сам.
        message = dict(choices[0]["message"])
        finish = (choices[0] or {}).get("finish_reason")
        if finish == "length":
            # Обрыв по лимиту не маскируем под полный ответ: в логах видно, что ход
            # кончился на полуслове, а не потому, что модель «так ответила».
            log.warning("%s: ответ обрезан по лимиту токенов", self.name)
        message["_usage"] = _usage(body, self.model)
        return message

    @property
    def usage_prices(self) -> tuple[float, float]:
        """Рубли за миллион токенов: вход, выход."""
        return self.price_in, self.price_out


def _retry_after(headers: Any) -> float:
    """Пауза из Retry-After в секундах. Нет заголовка или мусор в нём — две секунды."""
    try:
        return max(0.0, min(float(headers.get("Retry-After")), 30.0))
    except (TypeError, ValueError):
        return 2.0


def _usage(body: dict[str, Any], model: str) -> dict[str, Any]:
    raw = body.get("usage") or {}
    return {
        "model": model,
        "tokens_in": int(raw.get("prompt_tokens") or 0),
        "tokens_out": int(raw.get("completion_tokens") or 0),
    }
