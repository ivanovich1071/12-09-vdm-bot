"""Хранение импортов: интерфейс и реализация на SQLite (D4).

База отдельная — `data/catalog.sqlite3` (D9, решение D): запись тысяч товаров
импорта не блокирует запись диалогов работающего бота, а коммерческие данные не
лежат в одном файле с персональными. Схема — миграциями `migrations/*.sql`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Protocol

from catalog_import.models import (
    CatalogImport,
    ImportItem,
    ImportStatus,
    ImportSummary,
    Issue,
    Severity,
    StoredFile,
)
from core.migrations import apply_migrations

MIGRATIONS = Path(__file__).parent / "migrations"

_SELECT = (
    "SELECT i.id, i.status, i.file_id, i.uploaded_by, i.created_at, i.parsed_at, i.summary, "
    "i.error, f.filename, f.mime_type, f.size, f.checksum, f.storage_path, "
    "f.uploaded_by AS file_uploaded_by, f.uploaded_at, f.status AS file_status "
    "FROM catalog_imports i JOIN files f ON f.id = i.file_id"
)


class ImportRepository(Protocol):
    def find_by_checksum(self, checksum: str) -> CatalogImport | None: ...

    def create(self, file: StoredFile) -> CatalogImport: ...

    def finish(
        self,
        import_id: str,
        status: ImportStatus,
        summary: ImportSummary,
        items: list[ImportItem],
        issues: list[Issue],
        parsed_at: str,
    ) -> CatalogImport: ...

    def fail(self, import_id: str, error: str) -> None: ...

    def get(self, import_id: str) -> CatalogImport | None: ...

    def list_imports(self, limit: int = 20) -> list[CatalogImport]: ...

    def items(self, import_id: str) -> list[ImportItem]: ...

    def issues(
        self, import_id: str, severity: Severity | None = None, limit: int | None = None
    ) -> list[Issue]: ...


class SqliteImportRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        apply_migrations(self._db, MIGRATIONS)

    def close(self) -> None:
        self._db.close()

    def find_by_checksum(self, checksum: str) -> CatalogImport | None:
        row = self._db.execute(f"{_SELECT} WHERE f.checksum = ?", (checksum,)).fetchone()
        return _record(row) if row else None

    def create(self, file: StoredFile) -> CatalogImport:
        """Файл и импорт в статусе UPLOADED — одной транзакцией.

        Номер импорта — дата и порядковый номер за день: `2026-09-12-001`. Его
        читает человек в командной строке, а позже в админке.
        """
        day = file.uploaded_at[:10]
        with self._db:
            count = self._db.execute(
                "SELECT COUNT(*) FROM catalog_imports WHERE id LIKE ?", (f"{day}-%",)
            ).fetchone()[0]
            import_id = f"{day}-{count + 1:03d}"
            self._db.execute(
                "INSERT INTO files(id, filename, mime_type, size, checksum, storage_path, "
                "uploaded_by, uploaded_at, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file.id,
                    file.filename,
                    file.mime_type,
                    file.size,
                    file.checksum,
                    file.storage_path,
                    file.uploaded_by,
                    file.uploaded_at,
                    file.status,
                ),
            )
            self._db.execute(
                "INSERT INTO catalog_imports(id, file_id, status, uploaded_by, created_at, summary) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (import_id, file.id, ImportStatus.UPLOADED, file.uploaded_by, file.uploaded_at, "{}"),
            )
        return self.get(import_id)

    def finish(
        self,
        import_id: str,
        status: ImportStatus,
        summary: ImportSummary,
        items: list[ImportItem],
        issues: list[Issue],
        parsed_at: str,
    ) -> CatalogImport:
        """Результат разбора целиком или ничего. Повтор после сбоя переписывает прежнее."""
        with self._db:
            self._db.execute("DELETE FROM catalog_import_items WHERE import_id = ?", (import_id,))
            self._db.execute("DELETE FROM catalog_import_issues WHERE import_id = ?", (import_id,))
            self._db.executemany(
                "INSERT INTO catalog_import_items(import_id, sku_1c, name, price, stock, rows, payload) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        import_id,
                        item.sku_1c,
                        item.name,
                        item.price,
                        item.stock,
                        json.dumps(item.rows),
                        json.dumps(item.payload, ensure_ascii=False),
                    )
                    for item in items
                ],
            )
            self._db.executemany(
                "INSERT INTO catalog_import_issues(import_id, severity, code, message, sheet, "
                "row_number, column_name, sku_1c) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        import_id,
                        issue.severity,
                        issue.code,
                        issue.message,
                        issue.sheet,
                        issue.row_number,
                        issue.column,
                        issue.sku_1c,
                    )
                    for issue in issues
                ],
            )
            self._db.execute(
                "UPDATE catalog_imports SET status = ?, summary = ?, parsed_at = ?, error = NULL "
                "WHERE id = ?",
                (status, json.dumps(summary.to_dict(), ensure_ascii=False), parsed_at, import_id),
            )
        return self.get(import_id)

    def fail(self, import_id: str, error: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE catalog_imports SET error = ? WHERE id = ?", (error, import_id)
            )

    def get(self, import_id: str) -> CatalogImport | None:
        row = self._db.execute(f"{_SELECT} WHERE i.id = ?", (import_id,)).fetchone()
        return _record(row) if row else None

    def list_imports(self, limit: int = 20) -> list[CatalogImport]:
        rows = self._db.execute(
            f"{_SELECT} ORDER BY i.created_at DESC, i.id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_record(row) for row in rows]

    def items(self, import_id: str) -> list[ImportItem]:
        rows = self._db.execute(
            "SELECT sku_1c, name, price, stock, rows, payload FROM catalog_import_items "
            "WHERE import_id = ? ORDER BY rowid",
            (import_id,),
        ).fetchall()
        return [
            ImportItem(
                sku_1c=row["sku_1c"],
                name=row["name"],
                price=row["price"],
                stock=row["stock"],
                rows=json.loads(row["rows"]),
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]

    def issues(
        self, import_id: str, severity: Severity | None = None, limit: int | None = None
    ) -> list[Issue]:
        """Сначала ошибки, затем предупреждения; внутри — по листу и номеру строки."""
        query = (
            "SELECT severity, code, message, sheet, row_number, column_name, sku_1c "
            "FROM catalog_import_issues WHERE import_id = ?"
        )
        params: list[object] = [import_id]
        if severity is not None:
            query += " AND severity = ?"
            params.append(severity)
        query += (
            " ORDER BY CASE severity WHEN 'ERROR' THEN 0 ELSE 1 END, sheet, row_number, id"
        )
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return [
            Issue(
                severity=Severity(row["severity"]),
                code=row["code"],
                message=row["message"],
                row_number=row["row_number"],
                column=row["column_name"],
                sku_1c=row["sku_1c"],
                sheet=row["sheet"],
            )
            for row in self._db.execute(query, params).fetchall()
        ]


def _record(row: sqlite3.Row) -> CatalogImport:
    return CatalogImport(
        id=row["id"],
        status=ImportStatus(row["status"]),
        file=StoredFile(
            id=row["file_id"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            size=row["size"],
            checksum=row["checksum"],
            storage_path=row["storage_path"],
            uploaded_by=row["file_uploaded_by"],
            uploaded_at=row["uploaded_at"],
            status=row["file_status"],
        ),
        uploaded_by=row["uploaded_by"],
        created_at=row["created_at"],
        parsed_at=row["parsed_at"],
        summary=ImportSummary.from_dict(json.loads(row["summary"])),
        error=row["error"],
    )
