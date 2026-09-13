"""Хранение импортов: интерфейс и реализация на SQLite (D4).

База отдельная — `data/catalog.sqlite3` (D9, решение D): запись тысяч товаров
импорта не блокирует запись диалогов работающего бота, а коммерческие данные не
лежат в одном файле с персональными. Схема — миграциями `migrations/*.sql`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

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

if TYPE_CHECKING:
    from catalog_import.diff import CatalogDiff, DiffRow

MIGRATIONS = Path(__file__).parent / "migrations"

_SELECT = (
    "SELECT i.id, i.status, i.file_id, i.uploaded_by, i.created_at, i.parsed_at, i.summary, "
    "i.error, i.base_version, i.diff_fingerprint, i.diffed_at, i.approved_by, i.approved_at, "
    "i.version, f.filename, f.mime_type, f.size, f.checksum, f.storage_path, "
    "f.uploaded_by AS file_uploaded_by, f.uploaded_at, f.status AS file_status "
    "FROM catalog_imports i JOIN files f ON f.id = i.file_id"
)

_MATCH_COLUMNS = (
    "import_id, sku_1c, state, diff_status, changed_fields, old_name, new_name, old_price, "
    "new_price, price_status, price_delta, price_delta_pct, old_stock, new_stock, stock_changed, "
    "match_status, match_method, match_confidence, matched_product_id, candidates, reason_codes, "
    "needs_review, recoding, row_error, is_returning"
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
        diff: CatalogDiff | None = None,
    ) -> CatalogImport: ...

    def fail(self, import_id: str, error: str) -> None: ...

    def get(self, import_id: str) -> CatalogImport | None: ...

    def list_imports(self, limit: int = 20) -> list[CatalogImport]: ...

    def items(self, import_id: str) -> list[ImportItem]: ...

    def issues(
        self, import_id: str, severity: Severity | None = None, limit: int | None = None
    ) -> list[Issue]: ...

    def rejected_codes(self, import_id: str) -> set[str]: ...

    def save_diff(
        self, import_id: str, diff: CatalogDiff, summary: ImportSummary, diffed_at: str
    ) -> CatalogImport: ...

    def diff_rows(self, import_id: str) -> list[DiffRow]: ...


class SqliteImportRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
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
        diff: CatalogDiff | None = None,
    ) -> CatalogImport:
        """Результат разбора и diff целиком или ничего. Повтор после сбоя переписывает прежнее."""
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
            self._write_diff(import_id, diff, parsed_at)
        return self.get(import_id)

    def save_diff(
        self, import_id: str, diff: CatalogDiff, summary: ImportSummary, diffed_at: str
    ) -> CatalogImport:
        """Новый diff вместо прежнего: строки, базовая версия, отпечаток и счётчики."""
        with self._db:
            self._db.execute(
                "UPDATE catalog_imports SET summary = ? WHERE id = ?",
                (json.dumps(summary.to_dict(), ensure_ascii=False), import_id),
            )
            self._write_diff(import_id, diff, diffed_at)
        return self.get(import_id)

    def _write_diff(self, import_id: str, diff: CatalogDiff | None, diffed_at: str) -> None:
        self._db.execute("DELETE FROM catalog_matches WHERE import_id = ?", (import_id,))
        if diff is None:
            self._db.execute(
                "UPDATE catalog_imports SET base_version = NULL, diff_fingerprint = NULL, "
                "diffed_at = NULL WHERE id = ?",
                (import_id,),
            )
            return
        placeholders = ", ".join("?" for _ in _MATCH_COLUMNS.split(","))
        self._db.executemany(
            f"INSERT INTO catalog_matches({_MATCH_COLUMNS}) VALUES({placeholders})",
            [_match_values(import_id, row) for row in diff.rows],
        )
        self._db.execute(
            "UPDATE catalog_imports SET base_version = ?, diff_fingerprint = ?, diffed_at = ? "
            "WHERE id = ?",
            (diff.base_version, diff.fingerprint, diffed_at, import_id),
        )

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

    def rejected_codes(self, import_id: str) -> set[str]:
        """Коды файла, исключённые ошибкой строки: исчезнувшими они не считаются."""
        rows = self._db.execute(
            "SELECT DISTINCT sku_1c FROM catalog_import_issues "
            "WHERE import_id = ? AND severity = ? AND sku_1c IS NOT NULL AND sku_1c != ''",
            (import_id, Severity.ERROR),
        ).fetchall()
        return {row["sku_1c"] for row in rows}

    def diff_rows(self, import_id: str) -> list[DiffRow]:
        from catalog_import.diff import DiffRow

        rows = self._db.execute(
            f"SELECT {_MATCH_COLUMNS} FROM catalog_matches WHERE import_id = ? ORDER BY rowid",
            (import_id,),
        ).fetchall()
        return [
            DiffRow.from_dict(
                {
                    **dict(row),
                    "changed_fields": json.loads(row["changed_fields"]),
                    "candidates": json.loads(row["candidates"]),
                    "reason_codes": json.loads(row["reason_codes"]),
                    "returning": row["is_returning"],
                }
            )
            for row in rows
        ]


def _match_values(import_id: str, row: DiffRow) -> tuple[object, ...]:
    return (
        import_id,
        row.sku_1c,
        str(row.state),
        str(row.diff_status),
        json.dumps(list(row.changed_fields)),
        row.old_name,
        row.new_name,
        row.old_price,
        row.new_price,
        str(row.price_status),
        row.price_delta,
        row.price_delta_pct,
        row.old_stock,
        row.new_stock,
        int(row.stock_changed),
        row.match_status,
        row.match_method,
        row.match_confidence,
        row.matched_product_id,
        json.dumps([dict(candidate) for candidate in row.candidates], ensure_ascii=False),
        json.dumps(list(row.reason_codes)),
        int(row.needs_review),
        int(row.recoding),
        int(row.row_error),
        int(row.returning),
    )


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
        base_version=row["base_version"],
        diff_fingerprint=row["diff_fingerprint"],
        diffed_at=row["diffed_at"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        version=row["version"],
    )
