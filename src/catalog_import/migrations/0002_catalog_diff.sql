-- EPIC 4, этап 1: diff импорта 1С против текущего каталога (docs/DECISIONS.md, D11).
-- Стиль 0001: даты — TEXT ISO UTC из кода, флаги — INTEGER 0/1, списки — TEXT с JSON,
-- без CHECK. Статусы импорта теперь UPLOADED → PARSED | INVALID → APPROVED →
-- APPLIED, сбой применения — FAILED; перечень живёт в коде (`ImportStatus`).

-- Против какой версии посчитан diff и его отпечаток — одни на весь импорт.
ALTER TABLE catalog_imports ADD COLUMN base_version TEXT;
ALTER TABLE catalog_imports ADD COLUMN diff_fingerprint TEXT;
ALTER TABLE catalog_imports ADD COLUMN diffed_at TEXT;
ALTER TABLE catalog_imports ADD COLUMN approved_by TEXT;
ALTER TABLE catalog_imports ADD COLUMN approved_at TEXT;
-- Версия каталога, которую создал импорт.
ALTER TABLE catalog_imports ADD COLUMN version TEXT;

-- Позиция diff: строка на каждый код импорта и на каждый исчезнувший код.
CREATE TABLE catalog_matches (
    import_id          TEXT NOT NULL REFERENCES catalog_imports(id),
    sku_1c             TEXT NOT NULL,
    -- EXISTING / NEW / MISSING — состояние кода.
    state              TEXT NOT NULL,
    -- NEW / UPDATED / UNCHANGED / REMOVED / AMBIGUOUS.
    diff_status        TEXT NOT NULL,
    changed_fields     TEXT NOT NULL,
    old_name           TEXT,
    new_name           TEXT,
    old_price          INTEGER,
    new_price          INTEGER,
    -- UNCHANGED / INCREASED / DECREASED / NEW / REMOVED.
    price_status       TEXT NOT NULL,
    price_delta        INTEGER,
    price_delta_pct    REAL,
    old_stock          INTEGER,
    new_stock          INTEGER,
    stock_changed      INTEGER NOT NULL,
    match_status       TEXT,
    match_method       TEXT,
    match_confidence   REAL,
    matched_product_id TEXT,
    candidates         TEXT NOT NULL,
    reason_codes       TEXT NOT NULL,
    needs_review       INTEGER NOT NULL,
    recoding           INTEGER NOT NULL,
    row_error          INTEGER NOT NULL,
    -- Код уже был в каталоге раньше. `returning` — ключевое слово SQLite.
    is_returning       INTEGER NOT NULL,
    PRIMARY KEY (import_id, sku_1c)
);

CREATE INDEX catalog_matches_status ON catalog_matches(import_id, diff_status);
