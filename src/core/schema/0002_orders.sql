-- NEXT-2 Order Core: загруженный заказ клиента, его строки и оценки.
-- Файл лежит в `data/uploads` под контрольной суммой; повторная загрузка того же
-- файла тем же пользователем возвращает прежний заказ.
CREATE TABLE uploaded_orders (
    id              TEXT PRIMARY KEY,
    owner           TEXT NOT NULL,
    channel         TEXT NOT NULL,
    status          TEXT NOT NULL,
    filename        TEXT NOT NULL,
    media_type      TEXT NOT NULL,
    size            INTEGER NOT NULL,
    checksum        TEXT NOT NULL,
    storage_path    TEXT NOT NULL,
    parser          TEXT,
    catalog_version TEXT NOT NULL,
    norm_version    TEXT NOT NULL,
    context         TEXT NOT NULL,
    warnings        TEXT NOT NULL,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX uploaded_orders_owner ON uploaded_orders(owner, created_at);
CREATE UNIQUE INDEX uploaded_orders_checksum ON uploaded_orders(owner, checksum);

-- Исходные ячейки (`cells`, `raw`) хранятся рядом с нормализованными полями:
-- из файла ничего не теряется.
CREATE TABLE uploaded_order_items (
    order_id          TEXT NOT NULL REFERENCES uploaded_orders(id),
    line_no           INTEGER NOT NULL,
    source_line       INTEGER NOT NULL,
    source_table      INTEGER NOT NULL,
    raw               TEXT NOT NULL,
    cells             TEXT NOT NULL,
    article           TEXT,
    name              TEXT,
    name_canonical    TEXT,
    manufacturer      TEXT,
    characteristics   TEXT,
    dimensions        TEXT,
    quantity          INTEGER,
    unit              TEXT,
    price             INTEGER,
    total             INTEGER,
    norm_document     TEXT,
    norm_item         TEXT,
    issues            TEXT NOT NULL,
    manual_product_id TEXT,
    manual_by         TEXT,
    PRIMARY KEY (order_id, line_no)
);

CREATE TABLE order_evaluations (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES uploaded_orders(id),
    owner           TEXT NOT NULL,
    status          TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    norm_version    TEXT NOT NULL,
    summary         TEXT NOT NULL,
    items           TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX order_evaluations_order ON order_evaluations(order_id, created_at);
