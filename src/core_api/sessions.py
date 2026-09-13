"""Сессия Core API: кто обращается и через какой канал.

Сессия связывает канал и пользователя канала (`user_ref`) — тот же ключ, по
которому бот хранит корзину, согласие и данные субъекта. Поэтому человек,
открывший Mini App, видит корзину своего Telegram-бота, а `/delete_data` удаляет
всё сразу.

Кто такой пользователь, решает не API:

- анонимная сессия — `user_ref` выдаёт сервер, как идентификатор посетителя виджета;
- серверный адаптер с ключом `CORE_API_KEY` называет `user_ref` сам;
- публичный клиент (Mini App) предъявляет подписанные данные канала, их проверяет
  `IdentityVerifier`, подключённый адаптером. Ветвлений по каналам в API нет.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol

from core.database import CoreDatabase
from core.errors import InvalidRequest, Unauthorized

SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
CHANNEL = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
USER_REF = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")

ANONYMOUS = "anonymous"
ADAPTER = "adapter"


@dataclass(frozen=True)
class CoreSession:
    id: str
    channel: str
    user_ref: str
    origin: str
    created_at: str
    last_seen_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class IdentityVerifier(Protocol):
    """Проверка подписанных данных канала. Возвращает `user_ref` или бросает `Unauthorized`."""

    channel: str

    def verify(self, value: str) -> str: ...


class SqliteSessionRepository:
    def __init__(self, db: CoreDatabase) -> None:
        self.db = db

    def find(self, channel: str, user_ref: str) -> CoreSession | None:
        with self.db.read() as db:
            row = db.execute(
                "SELECT * FROM core_sessions WHERE channel = ? AND user_ref = ? "
                "ORDER BY last_seen_at DESC LIMIT 1",
                (channel, user_ref),
            ).fetchone()
        return CoreSession(**dict(row)) if row else None

    def get(self, session_id: str) -> CoreSession | None:
        with self.db.read() as db:
            row = db.execute("SELECT * FROM core_sessions WHERE id = ?", (session_id,)).fetchone()
        return CoreSession(**dict(row)) if row else None

    def save(self, session: CoreSession) -> None:
        with self.db.write() as db:
            db.execute(
                "INSERT INTO core_sessions(id, channel, user_ref, origin, created_at, last_seen_at) "
                "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (session.id, session.channel, session.user_ref, session.origin, session.created_at, session.last_seen_at),
            )

    def delete_user(self, user_ref: str) -> None:
        with self.db.write() as db:
            db.execute("DELETE FROM core_sessions WHERE user_ref = ?", (user_ref,))


class SessionService:
    def __init__(self, repository: SqliteSessionRepository, clock: Callable[[], datetime] | None = None) -> None:
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    def open(self, channel: str, user_ref: str | None, origin: str) -> CoreSession:
        if not CHANNEL.match(channel or ""):
            raise InvalidRequest("Канал — латиница, цифры и «_».", code="INVALID_CHANNEL")
        now = self._clock().isoformat(timespec="seconds")
        if user_ref is not None:
            if not USER_REF.match(user_ref):
                raise InvalidRequest("Недопустимый идентификатор пользователя канала.", code="INVALID_USER_REF")
            existing = self.repository.find(channel, user_ref)
            if existing is not None:
                touched = CoreSession(**{**existing.to_dict(), "last_seen_at": now})
                self.repository.save(touched)
                return touched
        session_id = secrets.token_hex(16)
        session = CoreSession(session_id, channel, user_ref or session_id, origin, now, now)
        self.repository.save(session)
        return session

    def get(self, session_id: str) -> CoreSession:
        session = self.repository.get(session_id) if SESSION_ID.match(session_id or "") else None
        if session is None:
            raise Unauthorized("Сессия не найдена — откройте новую.", code="SESSION_NOT_FOUND")
        return session

    def delete_user(self, user_ref: str) -> None:
        self.repository.delete_user(user_ref)


class SessionUserData:
    """Сессии — часть данных субъекта: удаляются вместе с корзиной и перепиской."""

    def __init__(self, sessions: SessionService) -> None:
        self.sessions = sessions

    def export(self, user_id: str) -> dict[str, object]:
        return {}

    def delete(self, user_id: str) -> None:
        self.sessions.delete_user(user_id)
