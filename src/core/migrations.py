"""Версионированные миграции SQLite.

Новые таблицы (с EPIC 2) создаются пронумерованными SQL-файлами, а не
`CREATE TABLE IF NOT EXISTS` в коде: видно, какая схема у базы, и изменение
таблицы не превращается в ручную правку живого файла. Таблицы бота в
`core/storage.py` пока не переведены.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_VERSION = re.compile(r"^\d{4}_[a-z0-9_]+$")


def apply_migrations(db: sqlite3.Connection, directory: Path) -> list[str]:
    """Применяет недостающие миграции каталога по порядку имён. Возвращает применённые.

    Миграция идёт одной транзакцией вместе с отметкой в `schema_migrations`:
    упавшая не оставляет ни половины таблиц, ни ложной отметки.
    """
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    db.commit()
    applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}

    done: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        version = path.stem
        if version in applied:
            continue
        if not _VERSION.match(version):
            raise ValueError(f"Имя миграции должно быть вида 0001_name.sql: {path.name}")
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        body = path.read_text(encoding="utf-8").strip().rstrip(";")
        script = (
            f"BEGIN;\n{body};\n"
            f"INSERT INTO schema_migrations(version, applied_at) VALUES('{version}', '{stamp}');\n"
            "COMMIT;"
        )
        try:
            db.executescript(script)
        except sqlite3.Error:
            if db.in_transaction:
                db.rollback()
            raise
        done.append(version)
    return done
