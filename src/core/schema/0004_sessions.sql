-- NEXT-3 Core API: сессия канала. Персональных данных нет: канал и идентификатор
-- пользователя в канале — тот же ключ, что у корзины и согласия бота.
CREATE TABLE core_sessions (
    id           TEXT PRIMARY KEY,
    channel      TEXT NOT NULL,
    user_ref     TEXT NOT NULL,
    origin       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX core_sessions_user ON core_sessions(channel, user_ref);
