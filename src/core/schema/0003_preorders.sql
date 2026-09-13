-- NEXT-2: предзаказ — не заказ, а заявка на проверку менеджером.
CREATE TABLE preorders (
    id              TEXT PRIMARY KEY,
    owner           TEXT NOT NULL,
    channel         TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    evaluation_id   TEXT,
    status          TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    norm_version    TEXT NOT NULL,
    review_required INTEGER NOT NULL,
    totals          TEXT NOT NULL,
    warnings        TEXT NOT NULL,
    -- Контакты клиента — только после согласия на обработку ПДн (consent_id).
    customer        TEXT,
    consent_id      TEXT,
    comment         TEXT,
    manager_comment TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX preorders_owner ON preorders(owner, created_at);
CREATE INDEX preorders_status ON preorders(status, updated_at);

CREATE TABLE preorder_items (
    preorder_id TEXT NOT NULL REFERENCES preorders(id),
    line_no     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (preorder_id, line_no)
);

-- История статусов: кто, когда, с каким комментарием.
CREATE TABLE preorder_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    preorder_id TEXT NOT NULL REFERENCES preorders(id),
    status      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    comment     TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX preorder_events_preorder ON preorder_events(preorder_id, id);

-- Сбой уведомления не теряет предзаказ: попытка записана, повтор возможен.
CREATE TABLE preorder_notifications (
    preorder_id TEXT NOT NULL REFERENCES preorders(id),
    channel     TEXT NOT NULL,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (preorder_id, channel)
);

-- Ручные решения: сопоставление, количество, перекодировка, подтверждение.
-- Данные каталога они не меняют — это журнал для менеджера и будущей админки.
CREATE TABLE manual_decisions (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    subject    TEXT NOT NULL,
    payload    TEXT NOT NULL,
    actor      TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX manual_decisions_kind ON manual_decisions(kind, created_at);
