-- EPIC 4, этап 2: версии каталога и история товаров (docs/DECISIONS.md, D11).
-- Стиль 0001: даты — TEXT ISO UTC из кода, флаги — INTEGER 0/1, JSON — TEXT, без CHECK.

-- Версия каталога. Снимок — неизменяемый файл data/kb/versions/<version>/products.jsonl;
-- snapshot_path — относительно корня проекта, без абсолютных путей.
-- source: baseline / 1c / media / registry / rollback; status: READY → APPLIED | FAILED.
CREATE TABLE catalog_versions (
    version        TEXT PRIMARY KEY,
    parent_version TEXT,
    source         TEXT NOT NULL,
    import_id      TEXT,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    created_by     TEXT,
    applied_at     TEXT,
    -- Порядок применения: время в секундах не различает версии одной секунды.
    applied_seq    INTEGER,
    snapshot_path  TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    product_count  INTEGER NOT NULL,
    counters       TEXT NOT NULL,
    inputs         TEXT NOT NULL,
    forced         INTEGER NOT NULL,
    error          TEXT
);

CREATE INDEX catalog_versions_status ON catalog_versions(status, applied_seq);

-- История товара: строка на каждое изменение карточки. Открытая строка (valid_to IS
-- NULL) — последнее состояние. Исчезнувший товар получает строку REMOVED с последней
-- карточкой: история не удаляется. В поиск бота эта таблица не попадает.
CREATE TABLE product_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sku_1c         TEXT NOT NULL,
    version        TEXT NOT NULL REFERENCES catalog_versions(version),
    change_status  TEXT NOT NULL,
    valid_from     TEXT NOT NULL,
    valid_to       TEXT,
    name           TEXT,
    price          INTEGER,
    in_stock       INTEGER,
    changed_fields TEXT NOT NULL,
    card           TEXT NOT NULL
);

CREATE INDEX product_versions_open ON product_versions(sku_1c, valid_to);
CREATE INDEX product_versions_version ON product_versions(version);
