-- EPIC 2: загрузка выгрузки 1С на проверку (docs/DECISIONS.md, D9).
-- Каталог бота эти таблицы не читает: утверждение и применение — EPIC 4.

-- Загруженный файл. Ключ идемпотентности — контрольная сумма: одно и то же
-- содержимое хранится и разбирается один раз, как бы ни назывался файл.
CREATE TABLE files (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    mime_type    TEXT NOT NULL,
    size         INTEGER NOT NULL,
    checksum     TEXT NOT NULL UNIQUE,
    storage_path TEXT NOT NULL,
    uploaded_by  TEXT NOT NULL,
    uploaded_at  TEXT NOT NULL,
    status       TEXT NOT NULL
);

-- Импорт: UPLOADED → PARSED | INVALID. Счётчики предпросмотра — JSON в summary.
CREATE TABLE catalog_imports (
    id          TEXT PRIMARY KEY,
    file_id     TEXT NOT NULL UNIQUE REFERENCES files(id),
    status      TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    parsed_at   TEXT,
    summary     TEXT NOT NULL,
    error       TEXT
);

-- Товар импорта: нормализованная запись на один код 1С и строки листа, из
-- которых она собрана. Товары со строками-ошибками сюда не попадают.
CREATE TABLE catalog_import_items (
    import_id TEXT NOT NULL REFERENCES catalog_imports(id),
    sku_1c    TEXT NOT NULL,
    name      TEXT NOT NULL,
    price     INTEGER,
    stock     INTEGER,
    rows      TEXT NOT NULL,
    payload   TEXT NOT NULL,
    PRIMARY KEY (import_id, sku_1c)
);

-- Проблемы файла и строк: ошибки исключают товар, предупреждения — нет.
CREATE TABLE catalog_import_issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id   TEXT NOT NULL REFERENCES catalog_imports(id),
    severity    TEXT NOT NULL,
    code        TEXT NOT NULL,
    message     TEXT NOT NULL,
    sheet       INTEGER NOT NULL DEFAULT 1,
    row_number  INTEGER,
    column_name TEXT,
    sku_1c      TEXT
);

CREATE INDEX catalog_import_issues_import ON catalog_import_issues(import_id, severity);
