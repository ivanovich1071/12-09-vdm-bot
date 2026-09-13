"""Хранение версий каталога и истории товаров: SQLite за интерфейсом (D4).

База та же, что у импортов, — `data/catalog.sqlite3`, схема — миграцией `0003`.
Каждый метод — короткая транзакция только над базой: работа со снимками идёт вне
транзакций, под файловым замком (`lock.py`).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from catalog_import.models import ImportStatus
from catalog_import.repository import MIGRATIONS
from catalog_versions.models import (
    CatalogVersion,
    ChangeStatus,
    ProductChange,
    VersionSource,
    VersionStatus,
)
from core.migrations import apply_migrations

_COLUMNS = (
    "version, parent_version, source, import_id, status, created_at, created_by, applied_at, "
    "applied_seq, snapshot_path, sha256, product_count, counters, inputs, forced, error"
)
_CHUNK = 500


class SqliteVersionRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        apply_migrations(self._db, MIGRATIONS)

    def close(self) -> None:
        self._db.close()

    # --- Чтение ----------------------------------------------------------------

    def has_versions(self) -> bool:
        return self._db.execute("SELECT 1 FROM catalog_versions LIMIT 1").fetchone() is not None

    def get(self, version: str) -> CatalogVersion | None:
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM catalog_versions WHERE version = ?", (version,)
        ).fetchone()
        return _version(row) if row else None

    def list_versions(self, limit: int = 20) -> list[CatalogVersion]:
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM catalog_versions ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_version(row) for row in rows]

    def latest_applied(self) -> CatalogVersion | None:
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM catalog_versions WHERE status = ? "
            "ORDER BY applied_seq DESC LIMIT 1",
            (VersionStatus.APPLIED,),
        ).fetchone()
        return _version(row) if row else None

    def ready(self) -> list[CatalogVersion]:
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM catalog_versions WHERE status = ? ORDER BY created_at, rowid",
            (VersionStatus.READY,),
        ).fetchall()
        return [_version(row) for row in rows]

    def known_versions(self) -> set[str]:
        return {row[0] for row in self._db.execute("SELECT version FROM catalog_versions")}

    def next_version(self, day: str, taken: Callable[[str], bool]) -> str:
        """Номер вида `2026-09-13-001`. Номер с папкой-сиротой на диске пропускается."""
        count = self._db.execute(
            "SELECT COUNT(*) FROM catalog_versions WHERE version LIKE ?", (f"{day}-%",)
        ).fetchone()[0]
        number = count + 1
        while taken(f"{day}-{number:03d}") or self.get(f"{day}-{number:03d}") is not None:
            number += 1
        return f"{day}-{number:03d}"

    def has_history(self, version: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM product_versions WHERE version = ? LIMIT 1", (version,)
            ).fetchone()
            is not None
        )

    def returning_codes(self, codes: Iterable[str]) -> set[str]:
        """Коды, которые уже были в каталоге и исчезли: открытая строка истории — REMOVED."""
        codes = list(codes)
        found: set[str] = set()
        for start in range(0, len(codes), _CHUNK):
            chunk = codes[start : start + _CHUNK]
            marks = ", ".join("?" for _ in chunk)
            found |= {
                row[0]
                for row in self._db.execute(
                    f"SELECT DISTINCT sku_1c FROM product_versions WHERE sku_1c IN ({marks}) "
                    "AND valid_to IS NULL AND change_status = ?",
                    (*chunk, ChangeStatus.REMOVED),
                )
            }
        return found

    def product_history(self, sku_1c: str) -> list[dict[str, object]]:
        rows = self._db.execute(
            "SELECT version, change_status, valid_from, valid_to, name, price, in_stock, "
            "changed_fields FROM product_versions WHERE sku_1c = ? ORDER BY id",
            (sku_1c,),
        ).fetchall()
        return [
            {**dict(row), "changed_fields": json.loads(row["changed_fields"])} for row in rows
        ]

    def open_history_count(self) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM product_versions WHERE valid_to IS NULL AND change_status != ?",
            (ChangeStatus.REMOVED,),
        ).fetchone()[0]

    # --- Запись ----------------------------------------------------------------

    def create_ready(self, version: CatalogVersion, approved_at: str) -> None:
        """Версия READY и, у импорта 1С, импорт APPROVED — одной транзакцией."""
        with self._db:
            self._db.execute(
                f"INSERT INTO catalog_versions({_COLUMNS}) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    version.version,
                    version.parent_version,
                    version.source,
                    version.import_id,
                    VersionStatus.READY,
                    version.created_at,
                    version.created_by,
                    version.snapshot_path,
                    version.sha256,
                    version.product_count,
                    json.dumps(version.counters, ensure_ascii=False),
                    json.dumps(version.inputs, ensure_ascii=False),
                    int(version.forced),
                ),
            )
            if version.import_id:
                self._db.execute(
                    "UPDATE catalog_imports SET status = ?, approved_by = ?, approved_at = ?, "
                    "version = ?, error = NULL WHERE id = ?",
                    (
                        ImportStatus.APPROVED,
                        version.created_by,
                        approved_at,
                        version.version,
                        version.import_id,
                    ),
                )

    def mark_failed(self, version: str, error: str) -> None:
        with self._db:
            row = self._db.execute(
                "SELECT import_id FROM catalog_versions WHERE version = ?", (version,)
            ).fetchone()
            self._db.execute(
                "UPDATE catalog_versions SET status = ?, error = ? WHERE version = ?",
                (VersionStatus.FAILED, error, version),
            )
            if row and row["import_id"]:
                self._db.execute(
                    "UPDATE catalog_imports SET status = ?, error = ? WHERE id = ?",
                    (ImportStatus.FAILED, error, row["import_id"]),
                )

    def finalize(self, version: str, applied_at: str, changes: Sequence[ProductChange]) -> None:
        """История товаров, версия APPLIED и импорт APPLIED — одной транзакцией.

        Повтор безопасен: если строки истории этой версии уже есть, они не дублируются.
        """
        with self._db:
            current = self._db.execute(
                "SELECT status, import_id FROM catalog_versions WHERE version = ?", (version,)
            ).fetchone()
            if current is None:
                raise LookupError(f"Версии {version} нет в базе.")
            if current["status"] == VersionStatus.APPLIED:
                return
            if not self.has_history(version):
                for change in changes:
                    self._db.execute(
                        "UPDATE product_versions SET valid_to = ? "
                        "WHERE sku_1c = ? AND valid_to IS NULL",
                        (applied_at, change.sku_1c),
                    )
                self._db.executemany(
                    "INSERT INTO product_versions(sku_1c, version, change_status, valid_from, "
                    "valid_to, name, price, in_stock, changed_fields, card) "
                    "VALUES(?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                    [
                        (
                            change.sku_1c,
                            version,
                            change.change_status,
                            applied_at,
                            change.card.get("name"),
                            change.card.get("price"),
                            change.card.get("in_stock"),
                            json.dumps(list(change.changed_fields)),
                            json.dumps(change.card, ensure_ascii=False),
                        )
                        for change in changes
                    ],
                )
            sequence = self._db.execute(
                "SELECT COALESCE(MAX(applied_seq), 0) + 1 FROM catalog_versions"
            ).fetchone()[0]
            self._db.execute(
                "UPDATE catalog_versions SET status = ?, applied_at = ?, applied_seq = ?, "
                "error = NULL WHERE version = ?",
                (VersionStatus.APPLIED, applied_at, sequence, version),
            )
            if current["import_id"]:
                self._db.execute(
                    "UPDATE catalog_imports SET status = ?, version = ?, error = NULL WHERE id = ?",
                    (ImportStatus.APPLIED, version, current["import_id"]),
                )


def _version(row: sqlite3.Row) -> CatalogVersion:
    return CatalogVersion(
        version=row["version"],
        parent_version=row["parent_version"],
        source=VersionSource(row["source"]),
        import_id=row["import_id"],
        status=VersionStatus(row["status"]),
        created_at=row["created_at"],
        created_by=row["created_by"],
        applied_at=row["applied_at"],
        applied_seq=row["applied_seq"],
        snapshot_path=row["snapshot_path"],
        sha256=row["sha256"],
        product_count=row["product_count"],
        counters=json.loads(row["counters"]),
        inputs=json.loads(row["inputs"]),
        forced=bool(row["forced"]),
        error=row["error"],
    )
