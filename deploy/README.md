# Развёртывание на сервере

Прототип поднимается двумя контейнерами: `widget` (HTTP, порт 8000 — виджет для
сайта, демо-страница, Core API) и `telegram` (long polling). Каталог товаров и
база лежат в `data/` на диске сервера и монтируются внутрь контейнеров, поэтому
пересборка образа данные не трогает.

Проверено на Ubuntu 22.04 и 24.04. Облако значения не имеет — нужен доступ по SSH
и открытые наружу порты 22 и 8000.

## Что нужно до начала

1. ВМ с Ubuntu, SSH-доступ под `root`.
2. В файрволе облака (у Selectel — правила подсети, у Cloud.ru — Security Group)
   разрешены входящие **22/tcp** и **8000/tcp**.
3. На машине разработки собран каталог: есть `data/kb/current` и папка версии.
4. Токен Telegram-бота и ключ модели — вписываются **только на сервере**, руками.

Чтобы не вводить пароль на каждом шаге, положите ключ:

```bash
ssh-copy-id root@<адрес>
```

## Шаг 1. Привезти код и каталог

С машины разработки (Windows — Git Bash), из корня проекта:

```bash
bash deploy/upload.sh <адрес сервера>
```

Скрипт:
- собирает архив **последнего коммита** текущей ветки (`git archive`) — приватный
  репозиторий на сервере не клонируется и токен GitHub туда не попадает;
- собирает архив каталога: `data/kb/current`, папка текущей версии,
  `norm_items.json`, `norms_1057.json`, реестр версий `data/catalog.sqlite3`;
- кладёт всё в `/opt/vdm-bot` и запускает `deploy/install.sh`.

Наружу **не уезжают**: `.env` и ключи, `data/vdm.sqlite3` (корзины, согласия,
контакты клиентов), журналы диалогов, предзаказы, стенограммы автотеста,
фотографии товаров (1,4 ГБ).

`install.sh` ставит Docker CE + Compose, включает ufw (22 и 8000), выставляет
московское время, создаёт каталоги данных, `.env` из шаблона (chmod 600),
собирает образ, поднимает контейнеры и проверяет `/health`.

## Шаг 2. Вписать ключи

```bash
ssh root@<адрес>
nano /opt/vdm-bot/.env
```

Заполнить:

| Ключ | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
| `CLOUDRU_API_KEY` | ключ Foundation Models (основной провайдер) |
| `OPENROUTER_API_KEY` | запасной; с российского адреса может не открыться |
| `LLM_PROVIDER` | `auto` — сначала Cloud.ru, при отказе OpenRouter |
| `WIDGET_ALLOWED_ORIGINS` | адрес сайта, если виджет встраивается на vdm.ru |
| `QA_USER_IDS` | id тестового аккаунта, если на сервере гоняется автотест |

`TELEGRAM_MINIAPP_URL` оставить пустым: Mini App требует HTTPS и домена, по HTTP
Telegram кнопку не откроет.

Затем:

```bash
cd /opt/vdm-bot && docker compose restart
```

## Шаг 3. Проверить

```bash
cd /opt/vdm-bot
curl -s localhost:8000/health                     # products > 0, catalog_version, llm: true
docker compose --profile tools run --rm catalog llm   # доступ к модели по шагам
docker compose run --rm telegram python run.py telegram --check   # токен живой
docker compose logs -f --tail=100 telegram
```

Демо-страница виджета: `http://<адрес>:8000/demo`.

### Один токен — один опрашивающий процесс

Telegram отдаёт обновления только одному long polling. Если тот же токен уже
опрашивает бот на рабочей машине или идёт автотест, серверный контейнер будет
падать с `Conflict: terminated by other getUpdates request`. Либо остановите
локальный запуск, либо заведите для сервера **отдельного бота** в @BotFather.

## Обновление кода

Тот же `upload.sh` — он идемпотентен: перезальёт исходники, `.env` и `data/`
не тронет, пересоберёт образ и перезапустит контейнеры.

```bash
bash deploy/upload.sh <адрес сервера>
```

Только перезапуск, без нового кода:

```bash
ssh root@<адрес> 'cd /opt/vdm-bot && docker compose restart telegram'
```

## Обновление каталога на сервере

```bash
cd /opt/vdm-bot
docker compose --profile tools run --rm catalog import-1c --file data/raw/<выгрузка>.xlsx
docker compose --profile tools run --rm catalog import-1c --diff <ID>
docker compose --profile tools run --rm catalog catalog approve <ID>
```

Перезапуск не нужен: оба процесса сверяют указатель `data/kb/current` раз в
`CATALOG_RELOAD_SECONDS` и перед каждым ходом.

## Резервная копия

Ценное — `data/`: каталог, корзины, предзаказы, журналы.

```bash
ssh root@<адрес> 'tar -czf - -C /opt/vdm-bot data' > vdm-data-$(date +%F).tgz
```

## Безопасность

- `.env` — `chmod 600`, `secrets/` — `chmod 700`; в git ни то, ни другое не попадает.
- Наружу открыты только 22 и 8000. Порт 8000 — обычный HTTP; перед публикацией
  виджета на сайте нужен домен и HTTPS (nginx + certbot), это отдельный шаг.
- Токен бота, засветившийся где-либо, отзывается в @BotFather (`/revoke`) и
  вписывается в `.env` заново.
