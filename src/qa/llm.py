"""Модель тестировщика и судьи — OpenRouter, ключ тот же, что у бота (`OPENROUTER_API_KEY`)."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Protocol

from agent.client import ChatClient, LLMAuthError, LLMError

DEFAULT_MODEL = "deepseek/deepseek-chat-v3-0324"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class Model(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> dict[str, Any]: ...


def openrouter(settings, model: str = DEFAULT_MODEL) -> ChatClient:  # noqa: ANN001 — core.config.Settings
    return ChatClient(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        model=model,
        timeout=120.0,
        max_tokens=1200,
        name="openrouter",
        extra_headers={"HTTP-Referer": settings.site_url, "X-Title": "ELTI-KUDITS scenario tester"},
    )


def ask_json(
    model: Model,
    system: str,
    user: str,
    *,
    temperature: float = 0.7,
    max_tokens: int = 700,
    attempts: int = 3,
    pause: float = 5.0,
) -> dict[str, Any]:
    """JSON-ответ модели. Сеть, 5xx и ответ не-JSON повторяются; ключ и деньги — нет."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            message = model.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except LLMAuthError:
            raise
        except LLMError as exc:
            last = exc
        else:
            data = parse_json(message.get("content") or "")
            if data:
                return data
            last = ValueError("модель ответила не JSON")
        if attempt + 1 < attempts:
            time.sleep(pause * (attempt + 1))
    raise LLMError(f"модель тестировщика не ответила: {last}")


def parse_json(text: str) -> dict[str, Any]:
    text = _FENCE.sub("", (text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
