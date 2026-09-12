# BASELINE — 12-09-vdm-bot

Состояние зафиксировано **12.09.2026, до любых изменений кода**. Задача
документа — дать точку, к которой можно сравнить любой следующий шаг.

Связанные документы: [AUDIT.md](AUDIT.md) · [ARCHITECTURE_CURRENT.md](ARCHITECTURE_CURRENT.md) · [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)

---

## 1. Исходный проект

| Параметр | Значение |
|---|---|
| Проект | `26-08-vdm-bot` — бот-магазин для vdm.ru (АО «ЭЛТИ-КУДИЦ») |
| Локальная папка | `C:\Users\Dell\Documents\GitHub\26-08 vdm` |
| GitHub | https://github.com/ivanovich1071/26-08-vdm-bot (**публичный**) |
| Ветка | `main` |
| Коммит | `6502846506bf419fd51f159d8bfb0f445f33335a`, 2026-09-02 12:44 +0300, «fix: пункт приказа ищется вместе с документом, названные позиции приходят карточками» |
| Совпадение с GitHub | `git ls-remote origin refs/heads/main` == HEAD, проверено 12.09.2026 |
| История | 15 коммитов, с 26.08.2026 по 02.09.2026 |
| Рабочее дерево | чистое, изменённых отслеживаемых файлов нет |
| Отслеживаемых файлов | 101 |

## 2. Клон

| Параметр | Значение |
|---|---|
| Папка | `C:\Users\Dell\Documents\GitHub\12-09-vdm-bot` |
| Способ | `git clone --no-hardlinks "…\26-08 vdm" 12-09-vdm-bot`, история полная |
| Remote | `baseline` → папка исходника, только fetch; push отключён URL-заглушкой `DISABLED_push_to_baseline` |
| Репозиторий на GitHub | **не создавался** — вопрос Q3 в AUDIT |
| `main` | равен `6502846`, не менялся |
| Документы аудита | ветка `epic-0/audit` |

### 2.1 Что скопировано помимо git (всё находится в `.gitignore`)

| Что | Объём | Зачем |
|---|---|---|
| `.env` | 1 файл | тот же конфиг, что у baseline |
| `tests/fixtures/product_card.html` | 190 КБ | без него один тест пропускается |
| `data/kb/` | 4 файла, 15,9 МБ | база знаний, справочник пунктов, реестр 1057, отчёт загрузки |
| `data/raw/` | 7 файлов, 79 МБ | выгрузка 1С `Pricelist20260826.xlsx`, PDF-каталоги, сохранённые страницы сайта |
| Документы из корня | 11 файлов | `ПЛАН_ПОЛНЫЙ.md`, `ВОПРОСЫ_ЗАКАЗЧИКУ.txt`, `НОРМАТИВКА_НА_ПОДТВЕРЖДЕНИЕ.md`, `СЦЕНАРИИ_С_ВОЗРАЖЕНИЯМИ.md`, `баги0209.мд`, два файла вебинаров, PDF приказов 838 и 1057, `Baza-Ivan-25-11-25.pdf`, `Промпт SMAIPL Иван Элти-beta.pdf` |

### 2.2 Что намеренно НЕ скопировано

| Что | Почему | Последствие для клона |
|---|---|---|
| `data/media/` — 7 323 файла, 1,38 ГБ | объём; фото — отдельный ресурс | карточки показываются без локальных снимков, пока папку не скопировать или не направить `MEDIA_DIR` на папку исходника |
| `data/vdm.sqlite3`, `data/orders.jsonl`, `data/orders/`, `data/dialogs/` | рабочее состояние и ПДн клиентов | клон стартует с пустыми корзинами, заказами, согласиями, журналом и кэшем фото |
| `id_rsa` | приватный ключ | — |
| `.venv`, `.claude/`, `*.log`, кэши | окружение | у клона свой `.venv` |

> ⚠️ Клон и исходник используют один `TELEGRAM_TOKEN`. Одновременно запускать
> `run.py telegram` в обеих папках нельзя: два процесса будут отбирать друг у
> друга обновления.

## 3. Окружение

| | Исходник | Клон |
|---|---|---|
| Python | 3.13.4 | 3.13.4, `.venv` создан заново |
| `requires-python` | `>=3.12` | `>=3.12` |
| Docker-образ | `python:3.12-slim` | тот же |
| Пакет проекта | editable-установка из git-URL | `pip install -e ".[dev]"` из папки клона |
| Откуда тесты берут модули | `pythonpath = ["src"]` | `12-09-vdm-bot\src` — проверено импортом |

### 3.1 Зависимости из `pyproject.toml`

- **Runtime:** `aiogram>=3.13`, `fastapi>=0.115`, `uvicorn[standard]>=0.30`,
  `sqlalchemy[asyncio]>=2.0`, `asyncpg>=0.29`, `alembic>=1.13`, `redis>=5.0`,
  `httpx>=0.27`, `openai>=1.40`, `pydantic>=2.8`, `pydantic-settings>=2.4`,
  `gspread>=6.1`, `google-auth>=2.34`, `apscheduler>=3.10`, `tenacity>=9.0`,
  `pypdf>=5.0`.
- **Dev:** `pytest>=8.3`, `pytest-asyncio>=0.24`, `ruff>=0.6`.

Реально импортируются только `aiogram`, `fastapi`, `uvicorn`, `pydantic`,
`gspread`, `pypdf` (AUDIT, R7).

### 3.2 Где версии исходника и клона разошлись

Клон ставился 12.09.2026, и pip выбрал более новые патч- и минор-версии. Все
остальные пакеты совпадают.

| Пакет | Исходник | Клон |
|---|---|---|
| alembic | 1.19.1 | 1.20.0 |
| anyio | 4.14.2 | 4.15.1 |
| google-auth | 2.57.0 | 2.58.0 |
| multidict | 6.7.1 | 6.8.0 |
| openai | 3.5.0 | 3.13.0 |
| pydantic / pydantic_core | 2.13.4 / 2.46.4 | 2.13.5 / 2.46.5 |
| pypdf | 6.16.2 | 6.18.1 |
| ruff | 0.16.4 | 0.16.7 |

Полные списки лежат в `docs/baseline/pip-freeze-source.txt` и
`docs/baseline/pip-freeze-clone.txt`.

## 4. Переменные окружения

Значения не записываются. «Задан» — в `.env` есть непустое значение; «пуст» —
переменная есть, но пустая; «нет» — берётся значение по умолчанию из
`core/config.py`.

| Переменная | Назначение | В `.env` |
|---|---|---|
| `KB_PATH` | база знаний `products.jsonl` | задан |
| `STORAGE_PATH` | SQLite | задан |
| `LLM_PROVIDER` | `auto` \| `cloudru` \| `openrouter` | задан |
| `CLOUDRU_API_KEY` | ключ Cloud.ru Foundation Models | задан |
| `CLOUDRU_BASE_URL`, `CLOUDRU_MODEL` | адрес и модель Cloud.ru | задан |
| `OPENROUTER_API_KEY` | ключ запасного провайдера | задан |
| `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL` | адрес и модель OpenRouter | задан |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_TOKENS` | таймаут и предел ответа | задан |
| `CLOUDRU_PRICE_IN/OUT`, `OPENROUTER_PRICE_IN/OUT` | ₽ за 1 млн токенов, для журнала | нет (18.53 / 37.08 и 0 / 0) |
| `TELEGRAM_TOKEN` | бот Telegram | задан |
| `MAX_TOKEN` | бот MAX | **пуст** |
| `SITE_URL`, `MANAGER_CONTACT` | сайт и контакты менеджера | задан |
| `ORDER_SINK` | `jsonl` \| `google_sheets` \| `bitrix24` | задан |
| `ORDERS_JSONL_PATH` | файл заказов | задан |
| `ORDERS_XLSX_DIR` | папка спецификаций Excel | нет (`data/orders`) |
| `GOOGLE_SHEETS_ID` | таблица заказов | **пуст** |
| `GOOGLE_CREDENTIALS_FILE` | ключ сервисного аккаунта | задан (файл `secrets/` не проверялся) |
| `MEDIA_ENABLED`, `MEDIA_MIN_INTERVAL`, `MEDIA_RESPECT_ROBOTS` | сбор фото с сайта | задан |
| `MEDIA_USER_AGENT` | User-Agent сборщика | пуст |
| `MEDIA_DIR` | папка снимков; читается конфигом, **в `.env.example` отсутствует** | нет (`data/media`) |
| `DIALOG_LOG_ENABLED`, `DIALOG_LOG_PATH`, `DIALOG_LOG_MASK_PDN` | журнал диалогов | задан |
| `WIDGET_ALLOWED_ORIGINS`, `WIDGET_HOST`, `WIDGET_PORT` | виджет | задан |

## 5. Точки входа

| Команда | Что делает |
|---|---|
| `python run.py ingest --source data/raw/<файл>.xlsx` | собирает `data/kb/products.jsonl` и `report.json` из выгрузки 1С |
| `python run.py norms --source Baza-Ivan-25-11-25.pdf` | реестр «пункт 1057 → код 1С» → `data/kb/norms_1057.json` |
| `python run.py acts [--check]` | тексты приказов 838 и 1057 → `data/kb/norm_items.json`, сверка |
| `python run.py widget` | FastAPI: виджет, демо-страница, порт 8000 |
| `python run.py telegram` | Telegram-бот, long polling |
| `python run.py llm` | пошаговая проверка провайдеров модели |
| `python run.py search "<запрос>"` | поиск из консоли |
| `python run.py media [--sync\|--dedupe\|--listing URL\|--cards N]` | фото и характеристики с сайта |
| `python run.py dialogs --last N [--export файл.md]` | просмотр журнала диалогов |

`docker-compose.yml`: сервисы `widget` (порт 8000, healthcheck `/health`),
`telegram` и `ingest` (профиль `tools`).

## 6. Интерфейсы

### 6.1 HTTP (`src/web/app.py`)

| Метод | Путь |
|---|---|
| GET | `/health` |
| POST | `/widget/session` |
| POST | `/widget/message` |
| POST | `/widget/action` |
| GET | `/media/{sku_1c}` |
| GET | `/widget.js` |
| GET | `/demo` |

Swagger и ReDoc отключены. Других HTTP-эндпоинтов нет.

### 6.2 Telegram

- **Меню команд:** `/start`, `/help`, `/cart`, `/order`, `/manager`.
- **Команды без меню, разбирает ядро:** `/menu`, `/my_data`, `/delete_data`.
- **Постоянная клавиатура:** «Каталог», «Моя корзина», «Менеджер».
- **Действия кнопок** — в ARCHITECTURE_CURRENT.md, §3.

### 6.3 Инструменты агента

`search_products`, `find_by_norm_code`, `find_norm_item`, `explain_norm`,
`get_product`, `add_to_cart`, `get_cart`, `handoff_to_manager`.

### 6.4 Схема SQLite (`src/core/storage.py`, создаётся при старте)

| Таблица | Ключ | Содержимое |
|---|---|---|
| `carts` | `user_id` | JSON позиций корзины |
| `orders` | `id` | JSON заказа, статус, попытки доставки, последняя ошибка |
| `consents` | `id` | пользователь, канал, версия текста, `granted` / `revoked`, время |
| `product_media` | `sku_1c` | адреса снимков, источник, ETag, Last-Modified, неудачи |
| `product_attributes` | `sku_1c` | JSON характеристик |
| `telegram_photos` | `path` | `file_id` загруженного в Telegram снимка |
| `dialog_state` | `user_id, channel` | маскированная история, профиль, время |

Миграций нет. Каталог `migrations/` пустой и в git не попадает.

## 7. Структура отслеживаемых файлов

```text
.env.example  .gitignore  Dockerfile  docker-compose.yml  pyproject.toml  run.py
README.md  ПЛАН.md  ДОРОЖНАЯ_КАРТА.md  ЗАПУСК_VSCODE.md  СЦЕНАРИИ_ДИАЛОГА.md
.vscode/{extensions,launch,settings,tasks}.json
docs/cloudru/{foundation_models_openai_compatible.postman_collection.json, model init.txt, openapi__foundation-models.yaml}
src/
├── adapters/{telegram/bot.py, web/ (пусто), max/ (пусто)}
├── agent/{agent.py, client.py, diagnostics.py, providers.py, routing.py, tools.py, verify.py}
│   └── prompts/{common.md, consultant.md, guard.md, router.md, salesman.md}
├── catalog/{models.py, repository.py, search.py, text.py}
├── core/{app.py, config.py, dialog.py, intent.py, models.py, profile.py, storage.py, ui.py}
├── ingest/{build_kb.py, catalog_tree.py, html_text.py, norm_registry.py, xlsx_reader.py}
├── media/{extract.py, fetcher.py, files.py, service.py, sync.py}
├── norms/{documents.py, extract.py, items.py, reference.py}
├── observability/dialog_log.py
├── orders/{service.py, sinks.py}
├── privacy/{consent.py, masking.py}
└── web/{app.py, render.py, static/{demo.html, widget.js}}
tests/  21 файл test_*.py (fixtures/ — в .gitignore)
```

В `src/` 41 модуль Python — около 8 000 непустых строк, без промптов и
статики. Самые крупные файлы (непустых строк): `core/dialog.py` (973),
`agent/agent.py` (512), `adapters/telegram/bot.py` (494), `agent/tools.py`
(458), `catalog/search.py` (424).

## 8. Тесты и линтер

| Прогон | Команда | Результат |
|---|---|---|
| pytest, исходник | `.venv\Scripts\python -m pytest -p no:cacheprovider -q -rs` | **317 passed** за 73,4 с |
| pytest, клон | та же | **317 passed**, 1 warning (DeprecationWarning anyio внутри starlette testclient) за 57,1 с |
| ruff, исходник | `ruff check src tests run.py --no-cache` (0.16.4) | All checks passed |
| ruff, клон | та же (0.16.7) | All checks passed |

Полные выводы: `docs/baseline/pytest-source.txt`, `docs/baseline/pytest-clone.txt`.

Если клонировать с GitHub без `tests/fixtures/`, по коду получится **316 passed,
1 skipped**: `test_real_card_yields_full_size_photos` помечен `skipif`.

Количество тестов по файлам — в AUDIT.md, §6.

## 9. Снимок данных на момент фиксации

**`data/kb/report.json`** (сборка 27.08.2026 из `Pricelist20260826.xlsx`, фото и
реестр добавлены позже):

| Показатель | Значение |
|---|---|
| строк с товаром | 8 486 |
| уникальных товаров | 5 936, из них в нескольких разделах 1 166 |
| с ценой | 5 904 |
| с остатком > 0 | 1 799 |
| с нормативной привязкой | 3 211 |
| с точным пунктом | 2 057 |
| из реестра 1057 | 1 157 |

**`products.jsonl` на 12.09.2026:** с фото 4 869, с характеристиками — по данным
дорожной карты 5 932.

**Корни каталога:** «Оборудование для детского сада» 4 535, «Оборудование для
школы по приказу № 838» 2 104, «Оснащение новостроек» 1 049, «Коррекционная
среда» 780, «Инновационные решения» 18.

**SQLite исходника** (только число строк, содержимое не читалось): `carts` 4,
`orders` 24 (все `sent`), `consents` 6, `product_media` 5 936,
`product_attributes` 5 932, `telegram_photos` 122, `dialog_state` 14.

## 10. Как повторить фиксацию

```powershell
cd "C:\Users\Dell\Documents\GitHub\12-09-vdm-bot"
git rev-parse main                      # 6502846506bf419fd51f159d8bfb0f445f33335a
.venv\Scripts\python -m pytest -q       # 317 passed
.venv\Scripts\ruff check src tests run.py
```
