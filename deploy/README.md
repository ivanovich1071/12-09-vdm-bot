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
| `TELEGRAM_PROXY` | транзит до api.telegram.org, если прямого выхода нет (см. ниже) |
| `CLOUDRU_API_KEY` | ключ Foundation Models (основной провайдер) |
| `LLM_PROVIDER` | на российском сервере — `cloudru` |
| `OPENROUTER_API_KEY` | не заполнять для контура заказчика: с российского адреса даёт 403, а в перечне получателей данных он лишний |
| `WIDGET_ALLOWED_ORIGINS` | адрес сайта, если виджет встраивается на vdm.ru |
| `ORDER_SINK` | `smtp`, чтобы заявка уходила письмом менеджерам |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | ящик-отправитель; 465 — SSL, 587 — STARTTLS |
| `ORDER_EMAIL_TO` | рабочий ящик менеджеров |
| `QA_USER_IDS` | id тестового аккаунта, если на сервере гоняется автотест |

Письмо — не единственная копия заявки: jsonl и спецификация в Excel пишутся всегда,
и упавший почтовый сервер заказ не теряет.

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

### Если с сервера не открывается api.telegram.org

Признак: `curl -s -o /dev/null -w '%{http_code} %{time_total}\n' https://api.telegram.org/`
отдаёт `000` через десяток секунд, а Cloud.ru с того же сервера отвечает за пару
секунд. Разбирается по шагам:

```bash
getent hosts api.telegram.org                 # резолвится ли имя
curl -v --max-time 10 https://api.telegram.org/   # где встаёт: DNS, TCP или TLS
```

Дальше — по порядку, от дешёвого к дорогому:

1. запрос в поддержку хостинга: постоянное ли это ограничение для пула и есть ли
   пул, откуда Telegram доступен;
2. проба второго российского провайдера — часовая ВМ и один `curl`;
3. если прямого выхода нет — транзит: SOCKS5 (`dante`, `3proxy`) на отдельной ВМ,
   вход разрешён только с адреса этого сервера, исходящие — только на 443,
   журналы без тела запросов. Адрес вписывается в `TELEGRAM_PROXY`:

```bash
TELEGRAM_PROXY=socks5://логин:пароль@хост:1080
```

TLS при этом остаётся сквозным: транзит передаёт зашифрованные байты и переписки
не видит — подробнее в [docs/ПДн_КОНТУР.md](../docs/ПДн_КОНТУР.md). Telegram-модуль
за границу **не выносится**: иностранный узел, разбирающий `update`, — это уже
обработчик персональных данных.

Проверка после правки `.env`:

```bash
cd /opt/vdm-bot
docker compose run --rm telegram python run.py telegram --check   # покажет хост транзита без пароля
docker compose restart telegram
docker compose logs -f --tail=50 telegram
```

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
