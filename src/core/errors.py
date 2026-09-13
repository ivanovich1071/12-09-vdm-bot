"""Ошибки и предупреждения доменных сервисов ядра с машинным кодом.

Канал не разбирает текст ошибки: Core API отдаёт `code`, `message` и `details`, а
что показать человеку, решает адаптер. Поэтому доменный код бросает эти исключения,
а не `ValueError` со строкой.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Notice:
    """Предупреждение с машинным кодом. Как его показать, решает канал."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


class DomainError(Exception):
    code = "DOMAIN_ERROR"

    def __init__(
        self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class NotFound(DomainError):
    code = "NOT_FOUND"


class InvalidRequest(DomainError):
    code = "INVALID_REQUEST"


class Conflict(DomainError):
    """Операция не подходит к текущему состоянию: статус, версия, владелец."""

    code = "CONFLICT"


class Forbidden(DomainError):
    code = "FORBIDDEN"


class Unauthorized(DomainError):
    """Нет сессии или ключа адаптера."""

    code = "UNAUTHORIZED"


class Unavailable(DomainError):
    """Возможность выключена конфигурацией: например, не задан ключ."""

    code = "SERVICE_UNAVAILABLE"
