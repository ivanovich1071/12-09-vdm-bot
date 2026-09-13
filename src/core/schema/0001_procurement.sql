-- NEXT-1 Procurement Core: задача закупки и спецификация.
-- Персональных данных нет: задача описывает закупку, а не человека.
CREATE TABLE procurement_tasks (
    id         TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    channel    TEXT NOT NULL,
    stage      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX procurement_tasks_owner ON procurement_tasks(owner, updated_at);

-- Спецификация неизменяема: цены и итоги зафиксированы на версии каталога.
-- Пересчёт по новой версии — новая спецификация с `parent_id`.
CREATE TABLE specifications (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES procurement_tasks(id),
    owner           TEXT NOT NULL,
    status          TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    catalog_sha256  TEXT,
    norm_version    TEXT NOT NULL,
    parent_id       TEXT REFERENCES specifications(id),
    header          TEXT NOT NULL,
    totals          TEXT NOT NULL,
    warnings        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX specifications_task ON specifications(task_id, created_at);
CREATE INDEX specifications_owner ON specifications(owner, created_at);

CREATE TABLE specification_items (
    specification_id TEXT NOT NULL REFERENCES specifications(id),
    line_no          INTEGER NOT NULL,
    product_id       TEXT NOT NULL,
    article          TEXT NOT NULL,
    name             TEXT NOT NULL,
    quantity         INTEGER NOT NULL,
    quantity_source  TEXT NOT NULL,
    quantity_note    TEXT NOT NULL,
    unit             TEXT NOT NULL,
    unit_price       INTEGER,
    total_price      INTEGER,
    availability     TEXT NOT NULL,
    norm_document    TEXT,
    norm_item        TEXT,
    norm_item_title  TEXT,
    norm_status      TEXT NOT NULL,
    selection_reason TEXT NOT NULL,
    url              TEXT,
    PRIMARY KEY (specification_id, line_no)
);
