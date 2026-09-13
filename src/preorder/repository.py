"""Хранение предзаказов, истории, уведомлений и ручных решений (D4: SQL только здесь)."""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from core.database import CoreDatabase
from core.errors import Notice
from preorder.models import (
    NotificationStatus,
    Preorder,
    PreorderEvent,
    PreorderItem,
    PreorderSource,
    PreorderStatus,
    PreorderTotals,
)


class PreorderRepository(Protocol):
    def save(self, preorder: Preorder) -> None: ...

    def get(self, preorder_id: str) -> Preorder | None: ...

    def of_owner(self, owner: str, limit: int = 20) -> list[Preorder]: ...

    def by_status(self, status: PreorderStatus, limit: int = 50) -> list[Preorder]: ...

    def set_notification(self, preorder_id: str, channel: str, status: NotificationStatus, error: str | None, at: str) -> int: ...

    def failed_notifications(self, max_attempts: int) -> list[str]: ...

    def record_decision(self, kind: str, subject: str, payload: dict[str, Any], actor: str, status: str, at: str) -> str: ...

    def decisions(self, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...

    def export_owner(self, owner: str) -> list[dict[str, Any]]: ...

    def anonymize_owner(self, owner: str) -> int: ...


class SqlitePreorderRepository:
    def __init__(self, db: CoreDatabase) -> None:
        self.db = db

    def save(self, preorder: Preorder) -> None:
        with self.db.write() as db:
            db.execute(
                "INSERT INTO preorders(id, owner, channel, source, source_id, evaluation_id, status, "
                "catalog_version, norm_version, review_required, totals, warnings, customer, consent_id, "
                "comment, manager_comment, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status = excluded.status, "
                "review_required = excluded.review_required, totals = excluded.totals, "
                "warnings = excluded.warnings, customer = excluded.customer, "
                "consent_id = excluded.consent_id, comment = excluded.comment, "
                "manager_comment = excluded.manager_comment, updated_at = excluded.updated_at",
                (
                    preorder.id,
                    preorder.owner,
                    preorder.channel,
                    str(preorder.source),
                    preorder.source_id,
                    preorder.evaluation_id,
                    str(preorder.status),
                    preorder.catalog_version,
                    preorder.norm_version,
                    int(preorder.review_required),
                    json.dumps(preorder.totals.to_dict()),
                    json.dumps([notice.to_dict() for notice in preorder.warnings], ensure_ascii=False),
                    json.dumps(preorder.customer, ensure_ascii=False) if preorder.customer else None,
                    preorder.consent_id,
                    preorder.comment,
                    preorder.manager_comment,
                    preorder.created_at,
                    preorder.updated_at,
                ),
            )
            db.execute("DELETE FROM preorder_items WHERE preorder_id = ?", (preorder.id,))
            db.executemany(
                "INSERT INTO preorder_items(preorder_id, line_no, payload) VALUES(?, ?, ?)",
                [(preorder.id, item.line_no, json.dumps(item.to_dict(), ensure_ascii=False)) for item in preorder.items],
            )
            known = db.execute(
                "SELECT COUNT(*) FROM preorder_events WHERE preorder_id = ?", (preorder.id,)
            ).fetchone()[0]
            db.executemany(
                "INSERT INTO preorder_events(preorder_id, status, actor, comment, at) VALUES(?, ?, ?, ?, ?)",
                [
                    (preorder.id, str(event.status), event.actor, event.comment, event.at)
                    for event in preorder.history[known:]
                ],
            )

    def get(self, preorder_id: str) -> Preorder | None:
        with self.db.read() as db:
            row = db.execute("SELECT * FROM preorders WHERE id = ?", (preorder_id,)).fetchone()
            if row is None:
                return None
            items = db.execute(
                "SELECT payload FROM preorder_items WHERE preorder_id = ? ORDER BY line_no", (preorder_id,)
            ).fetchall()
            events = db.execute(
                "SELECT status, actor, comment, at FROM preorder_events WHERE preorder_id = ? ORDER BY id",
                (preorder_id,),
            ).fetchall()
            notification = db.execute(
                "SELECT status, last_error FROM preorder_notifications WHERE preorder_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (preorder_id,),
            ).fetchone()
        return Preorder(
            id=row["id"],
            owner=row["owner"],
            channel=row["channel"],
            source=PreorderSource(row["source"]),
            source_id=row["source_id"],
            evaluation_id=row["evaluation_id"],
            status=PreorderStatus(row["status"]),
            catalog_version=row["catalog_version"],
            norm_version=row["norm_version"],
            review_required=bool(row["review_required"]),
            items=tuple(PreorderItem.from_dict(json.loads(item["payload"])) for item in items),
            totals=PreorderTotals(**json.loads(row["totals"])),
            warnings=tuple(Notice(**notice) for notice in json.loads(row["warnings"])),
            customer=json.loads(row["customer"]) if row["customer"] else None,
            consent_id=row["consent_id"],
            comment=row["comment"],
            manager_comment=row["manager_comment"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            history=tuple(
                PreorderEvent(PreorderStatus(event["status"]), event["actor"], event["at"], event["comment"])
                for event in events
            ),
            notification=NotificationStatus(notification["status"]) if notification else None,
            notification_error=notification["last_error"] if notification else None,
        )

    def of_owner(self, owner: str, limit: int = 20) -> list[Preorder]:
        return self._many("SELECT id FROM preorders WHERE owner = ? ORDER BY created_at DESC LIMIT ?", (owner, limit))

    def by_status(self, status: PreorderStatus, limit: int = 50) -> list[Preorder]:
        return self._many(
            "SELECT id FROM preorders WHERE status = ? ORDER BY updated_at LIMIT ?", (str(status), limit)
        )

    def set_notification(
        self, preorder_id: str, channel: str, status: NotificationStatus, error: str | None, at: str
    ) -> int:
        with self.db.write() as db:
            db.execute(
                "INSERT INTO preorder_notifications(preorder_id, channel, status, attempts, last_error, updated_at) "
                "VALUES(?, ?, ?, 1, ?, ?) ON CONFLICT(preorder_id, channel) DO UPDATE SET "
                "status = excluded.status, attempts = attempts + 1, last_error = excluded.last_error, "
                "updated_at = excluded.updated_at",
                (preorder_id, channel, str(status), error, at),
            )
            return db.execute(
                "SELECT attempts FROM preorder_notifications WHERE preorder_id = ? AND channel = ?",
                (preorder_id, channel),
            ).fetchone()[0]

    def failed_notifications(self, max_attempts: int) -> list[str]:
        with self.db.read() as db:
            rows = db.execute(
                "SELECT DISTINCT preorder_id FROM preorder_notifications WHERE status = ? AND attempts < ? "
                "ORDER BY updated_at",
                (str(NotificationStatus.FAILED), max_attempts),
            ).fetchall()
        return [row["preorder_id"] for row in rows]

    def record_decision(
        self, kind: str, subject: str, payload: dict[str, Any], actor: str, status: str, at: str
    ) -> str:
        decision_id = uuid.uuid4().hex
        with self.db.write() as db:
            db.execute(
                "INSERT INTO manual_decisions(id, kind, subject, payload, actor, status, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (decision_id, kind, subject, json.dumps(payload, ensure_ascii=False), actor, status, at),
            )
        return decision_id

    def decisions(self, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM manual_decisions"
        params: tuple = ()
        if kind:
            query += " WHERE kind = ?"
            params = (kind,)
        with self.db.read() as db:
            rows = db.execute(query + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*params, limit)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def export_owner(self, owner: str) -> list[dict[str, Any]]:
        return [preorder.to_dict() for preorder in self.of_owner(owner, limit=1000)]

    def anonymize_owner(self, owner: str) -> int:
        """Контакты удаляются, позиции остаются у менеджера для учёта — как у заказов бота."""
        with self.db.write() as db:
            cursor = db.execute(
                "UPDATE preorders SET customer = NULL, owner = 'deleted' WHERE owner = ?", (owner,)
            )
            return cursor.rowcount or 0

    def _many(self, query: str, params: tuple) -> list[Preorder]:
        with self.db.read() as db:
            ids = [row["id"] for row in db.execute(query, params).fetchall()]
        return [preorder for preorder_id in ids if (preorder := self.get(preorder_id))]
