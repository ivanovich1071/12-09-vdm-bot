-- Идемпотентность оформления в базе (план 05-10, шаг 5.3): одна корзина — один
-- предзаказ, в том числе после перезапуска процесса. Прежде отпечаток корзины
-- жил в памяти шлюза, и рестарт плодил копии заявки.
ALTER TABLE preorders ADD COLUMN fingerprint TEXT;
CREATE INDEX preorders_fingerprint ON preorders(owner, fingerprint, status);
