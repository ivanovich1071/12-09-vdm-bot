# AUDIT — 12-09-vdm-bot (EPIC 0, проход 1)

**Дата:** 12.09.2026.
**Предмет:** baseline `26-08-vdm-bot`, коммит
`6502846506bf419fd51f159d8bfb0f445f33335a` (ветка `main`, совпадает с GitHub).
**Режим:** только чтение. Новая функциональность не реализовывалась, исходный
код не менялся.

Связанные документы: [BASELINE.md](BASELINE.md) · [ARCHITECTURE_CURRENT.md](ARCHITECTURE_CURRENT.md) · [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)

---

## 0. Итог в десяти пунктах

1. **Baseline рабочий.** 317 тестов проходят (фактический прогон, а не README),
   `ruff` чистый, клон запускается в собственном окружении. Подробности — в
   BASELINE.md.
2. **Ядро уже канало-независимое.** Telegram и виджет — тонкие адаптеры над
   `DialogEngine`; бизнес-логика в одном месте. Требование ТЗ п. 56 выполнено
   конструкцией, а не обещанием. **REUSE.**
3. **Консультант, продавец, маршрутизатор, защита, профиль задачи, проверка
   цен и нормативных ссылок** есть и покрыты тестами. v2 их расширяет, а не
   переписывает.
4. **838 и 1057 разведены на уровне данных, поиска, инструментов и проверки
   ответа.** Регрессионные тесты есть, но под другими именами: см. §6.
5. **Главного для v2 нет:** версий каталога, diff, сопоставления, предпросмотра
   и утверждения импорта. Сейчас `run.py ingest` перезаписывает базу знаний
   целиком, а бот видит новые цены только после перезапуска.
6. **Нет количества как понятия, спецификации, загрузки файлов, предзаказа,
   пользователей и ролей, админки, MAX, Mini App, базы знаний в Markdown.**
   Всё это **NEW**.
7. **Tilda в проекте не встречается.** Сайт vdm.ru работает на 1С-Битрикс.
   Архитектуру вокруг Tilda не строю, пока нет ответа (вопрос Q1).
8. **Найден риск R1:** через виджет можно действовать от имени
   Telegram-пользователя — корзина, согласие, удаление данных. Не исправлено;
   правка предложена в плане, EPIC 0.5.
9. **Отдельных prompt-файлов для v2 не найдено.** Найдены промпт прежнего бота
   «Иван», промпт-судья и расшифровка вебинара. Разбор — в §8.
10. **Хранилище — SQLite + JSONL, PostgreSQL отложен сознательно.**
    Рекомендация: остаться на SQLite до EPIC 4, решение — в плане, Р1.

---

## 1. Что и как проверено

| Что | Как |
|---|---|
| Git | `git log`, `git status --ignored`, `git ls-remote` — HEAD совпадает с `origin/main` на GitHub |
| Код | прочитан весь `src/`: 41 модуль Python, 5 промптов, `widget.js`, `demo.html` (остальные 13 файлов — пустые `__init__.py`); а также `run.py`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.gitignore`, `.env.example` |
| Тесты | список всех 317 узлов (`--collect-only`), прогон в исходнике и в клоне; прочитан `test_norm_lookup.py`, по остальным — поиск по назначению |
| Данные | только чтение: `data/kb/report.json`, `products.jsonl` (5 936 строк), `norm_items.json`, `norms_1057.json`, SQLite в режиме `mode=ro` — только количество строк в таблицах |
| Документы | `README.md`, `ПЛАН.md`, `ПЛАН_ПОЛНЫЙ.md` (§0), `ДОРОЖНАЯ_КАРТА.md`, `ВОПРОСЫ_ЗАКАЗЧИКУ.txt`, `НОРМАТИВКА_НА_ПОДТВЕРЖДЕНИЕ.md`, `СЦЕНАРИИ_ДИАЛОГА.md` (§6–7), `баги0209.мд` (§6.8–7), заголовки остальных |
| Промпты | 5 файлов `src/agent/prompts/`, `Промпт SMAIPL Иван Элти-beta.pdf` (13 стр., текст извлечён `pypdf`), `Копия Вебинар сокол 23 августа. Промты.md`, начало `Копия Вебинар =сокол=13 июля 2026.md` |
| Секреты | в `.env` проверены только имена переменных и то, заданы ли они. Значения не читались и никуда не записаны |

**Не проверялось:** живой запуск Telegram-бота и виджета против реальных
токенов (клон и исходник делят один токен Telegram — конфликт опроса), обращения
к Cloud.ru и OpenRouter, сайт vdm.ru.

---

## 2. Факты о данных, влияющие на v2

| Факт | Значение | Откуда |
|---|---|---|
| Строк в выгрузке 1С | 8 486 → 5 936 уникальных товаров | `report.json` |
| Товаров в нескольких разделах | 1 166 | `report.json` |
| С ценой / без цены | 5 904 / 32 | `products.jsonl` |
| Остаток 0 или пусто | 4 137 (не различаются) | `products.jsonl` |
| Код 1С: дубли / пустые | 0 / 0 | `products.jsonl` |
| С нормативной привязкой / с точным пунктом | 3 211 / 2 057 | `report.json` |
| Ссылок по документам | 1057 — 3 146; 838 — 2 092; ФГОС ДО — 117; ФОП ДО — 103 | `products.jsonl` |
| Пунктов в текстах приказов | 838 — 1 873; 1057 — 2 600 (с единицей и количеством) | `norm_items.json` |
| Реестр 1057 («Иван») | 1 157 товаров, от 25.11.2025, совпадение с выгрузкой 85,1 % | `norms_1057.json`, дорожная карта |
| Реестр 838 → код 1С | **нет** | дорожная карта, вопрос 11.4 |
| С фото / с характеристиками | 4 869 / 5 932 | `products.jsonl` |
| Колонка «Артикул» в выгрузке | **нет** (в 1С поле есть, в выгрузку не попадает) | дорожная карта, п. 9 |
| Пример числа из ТЗ «Всего строк: 5936» | совпадает с числом уникальных товаров текущей выгрузки | — |

---

## 3. Аудит по областям ТЗ (п. 4)

Статусы: ✅ есть и работает · ⚠️ есть частично или с оговоркой · ❌ нет.

### 3.1 Core

| Что | Статус | Где | Комментарий |
|---|---|---|---|
| dialogue state | ✅ | `core/dialog.py:Session`, `DialogEngine._restore/_remember` | история и профиль переживают перезапуск, TTL 30 дней |
| session | ✅ | `Session` в памяти + `dialog_state` в SQLite | словарь `_sessions` растёт без ограничения (R4) |
| memory / task profile | ⚠️ | `core/profile.py:DialogProfile` | отделён от истории, как требует ТЗ п. 9; полей меньше, чем в модели ТЗ (§5.2) |
| cart | ✅ | `core/models.py:Cart`, `storage.carts`, `dialog._add/_change` | ключ — `user_id` без канала (R1); цена фиксируется при добавлении |
| order | ✅ | `core/models.py:Order`, `orders/service.py` | по смыслу это уже заявка менеджеру, но пользователю пишется «Заказ … принят» (ТЗ п. 43) |
| consent | ✅ | `privacy/consent.py`, `storage.consents`, `dialog._start_checkout` | версионируется, журнал только пополняется, проверка в сервисе, а не в адаптере |
| response primitives | ✅ | `core/ui.py` | нет примитива «документ/файл» |
| reset `/start` | ⚠️ | `dialog._restart` | чистит историю, профиль, **корзину** и незаконченное оформление; заказы и согласия не трогает. Корзину ТЗ п. 10 не упоминает — уточнить |

### 3.2 Agent

| Что | Статус | Где | Комментарий |
|---|---|---|---|
| consultant | ✅ | `prompts/consultant.md`, `agent.ROLE_TOOLS[consult]` | без каталожных инструментов, карточек не показывает (тесты) |
| salesman | ⚠️ | `prompts/salesman.md` | этапы: выяснение, подбор, возражения; режимов SPECIFICATION / ORDER_REVIEW / PREORDER нет |
| router | ✅ | `agent/routing.py`, `core/intent.py` | правила + дешёвый JSON-вызов; нет интентов FILE_UPLOAD / ORDER_REVIEW |
| guard | ✅ | `prompts/guard.md`, `_INJECTION`, ветка `guard` без инструментов | — |
| model provider | ✅ | `agent/client.py`, `providers.py`, `diagnostics.py` | Cloud.ru → OpenRouter, паузы, прогрев, диагностика `run.py llm` |
| tools | ✅ | `agent/tools.py`, 8 инструментов | нет спецификации, проверки заказа, предзаказа |
| verification | ⚠️ | `agent/verify.py`, `agent._verified` | проверяются суммы и ссылки на пункты; количества, итоги, утверждения о наличии и сроках — нет |
| prompts | ⚠️ | 5 файлов | часть бизнес-истины зашита в промпт (§8) |

### 3.3 Catalog

| Что | Статус | Где | Комментарий |
|---|---|---|---|
| import | ⚠️ | `ingest/build_kb.py`, `xlsx_reader.py`, `catalog_tree.py`, `html_text.py` | разбор хороший; нет validate/diff/review/approve/versions; перезапись целиком |
| search | ✅ | `catalog/search.py`, `catalog/text.py` | точный по пункту (ключ «документ + пункт»), BM25, триграммы, синонимы, разнообразие |
| filtering | ✅ | `apply_text_filters`, `CatalogIndex._filter` | наличие, цена от/до, раздел, документ |
| cards | ✅ | `ProductCard`, `dialog._card`, `telegram.render_card`, `web/render.py` | все основания с формулировками пунктов |
| pagination | ⚠️ | `dialog.PAGE_SIZE = 3`, `_more`, `last_hits` | 3 + «Показать ещё» есть. Между разными выдачами товары могут повторяться: `profile.offered` и `rejected` поиск не исключает |
| prices | ⚠️ | `Product.price: int \| None`, `price_text` | копейки отбрасываются округлением; истории нет |
| availability | ⚠️ | `in_stock`, `available`, `stock_text` | «неизвестно» и «нет» не различаются (ТЗ п. 39) |
| article | ⚠️ | `sku_1c` = код 1С | отдельного артикула нет в данных |
| photos | ✅ | `media/*`, `PhotoStore`, `telegram_photos` | уже отделены от цены: переносятся при пересборке, не перекачиваются (ТЗ п. 30) |

### 3.4 Norms

| Что | Статус | Где | Комментарий |
|---|---|---|---|
| 838 | ✅ | `norms/documents.ORDER_838`, `extract` (заголовки, slug URL), `items.parse_838` | привязка только из дерева каталога; 46 % каталога без привязки |
| 1057 | ✅ | `ORDER_1057`, `ingest/norm_registry.py`, `items.parse_1057` | реестр «Иван», для пунктов разобраны единица и количество |
| другие документы | ⚠️ | `FGOS_DO`, `FOP_DO`, `FUNC_KITS` | только упоминания, без пунктов и реестров |
| points | ✅ | `norm_items.json`, `norms/items.ItemIndex` | поиск пункта по словам и по номеру |
| mappings | ⚠️ | `Product.norms` (`NormRef.source`, `confidence`), `norms_1057.json` | статуса REVIEW_REQUIRED нет; маппинг не отделён от товара |
| разведение аудиторий | ✅ | `documents.for_audience`, `DialogProfile.audience`, `CatalogIndex._documents_for` | «спросил о документе» ≠ «закупает по нему» |
| справка по документу | ✅ | `norms/reference.py` | работает без модели, с оговоркой «не юридическое заключение» |

### 3.5 Orders

| Что | Статус | Где | Комментарий |
|---|---|---|---|
| cart → order | ✅ | оформление в `dialog.py` | согласие → 6 полей → проверка → отправка |
| order creation | ✅ | `OrderService.submit` | сохраняется до отправки |
| Excel | ✅ | `orders/sinks.XlsxSink` | простая таблица, без итогов и шапки; без зависимостей |
| manager notification | ⚠️ | `CompositeSink`: Jsonl + Xlsx (+ Google Sheets / Bitrix24-заглушка) | в мессенджер и на почту уведомлений нет; `retry_pending` нигде не вызывается (R2) |
| persistence | ✅ | SQLite `orders` | статусы `new/sent/failed`, версия каталога не хранится |

### 3.6 Adapters

| Что | Статус | Где | Комментарий |
|---|---|---|---|
| Telegram | ✅ | `adapters/telegram/bot.py` | переживает обрывы, повторы, редактирование на месте, `file_id` фото |
| Web | ✅ | `web/app.py`, `render.py`, `static/widget.js` | пакет `adapters/web/` пустой — адаптер живёт в `src/web/` |
| MAX | ❌ | `adapters/max/__init__.py` пустой | есть `MAX_TOKEN` и раскладка кнопок под лимиты MAX |
| другие | ❌ | — | Mini App, админки нет |

---

## 4. Главная таблица: компоненты ТЗ ↔ существующий код

`Reuse`: REUSE — берём как есть · EXTEND — дополняем · REFACTOR — перестраиваем
с сохранением поведения · NEW — пишем с нуля.

| Component | Existing file | Existing function | Status | Reuse | Change |
|---|---|---|---|---|---|
| Единое ядро для каналов | `src/core/dialog.py` | `DialogEngine.handle_text/handle_action` | ✅ | REUSE | новые действия и режимы — в ядре, не в адаптерах |
| Сборка приложения | `src/core/app.py` | `build_engine` | ✅ | EXTEND | подключение версий каталога, KB, уведомлений |
| Настройки | `src/core/config.py` | `Settings.from_env` | ✅ | EXTEND | пороги сопоставления, пути загрузок, админка |
| Хранилище | `src/core/storage.py` | `Storage` (SQLite, схема в коде) | ✅ | EXTEND → REFACTOR | миграции схемы, WAL, блокировка соединения, новые репозитории |
| Task profile | `src/core/profile.py` | `DialogProfile` | ⚠️ | EXTEND | поля ТЗ п. 9, `quantity_source`, режимы; ПДн-политика сохраняется |
| Reset | `src/core/dialog.py` | `_restart` | ✅ | REUSE | уточнить судьбу корзины |
| Корзина | `src/core/models.py`, `storage.py` | `Cart`, `CartItem`, `load_cart/save_cart` | ✅ | EXTEND | `quantity_source`, перепроверка цены по версии |
| Согласие ПДн | `src/privacy/consent.py` | `CONSENT_TEXT`, `CONSENT_VERSION` | ✅ | REUSE | — |
| Маскирование ПДн | `src/privacy/masking.py` | `Masker` | ✅ | REUSE | применять и к загруженным файлам заказа |
| Router | `src/agent/routing.py`, `src/core/intent.py` | `Router.decide`, `by_rules`, `classify` | ✅ | EXTEND | интенты FILE_UPLOAD, ORDER_REVIEW, SPECIFICATION, PREORDER |
| Consultant | `src/agent/prompts/consultant.md` | `ROLE_PARTS/ROLE_TOOLS[consult]` | ✅ | REUSE | пересмотр текста после получения промптов v2 |
| Salesman | `src/agent/prompts/salesman.md` | `ROLE_PARTS/ROLE_TOOLS[sell]` | ⚠️ | EXTEND | режимы ТЗ п. 7, первая спецификация (п. 15) |
| Guard | `src/agent/prompts/guard.md`, `routing.py` | `_INJECTION`, ветка `guard` | ✅ | REUSE | — |
| Гейт карточек | `src/agent/agent.py` | `may_show_cards` | ✅ | REUSE | — |
| Tool-calling | `src/agent/agent.py`, `tools.py` | `SalesAgent._run`, `ToolBox` | ✅ | EXTEND | `build_specification`, `check_order`, `create_preorder` |
| Anti-hallucination | `src/agent/verify.py` | `invented_prices`, `invented_norm_refs` | ✅ | EXTEND | количества, итоги, наличие |
| LLM-провайдеры | `src/agent/client.py`, `providers.py` | `ChatClient`, `LLMRouter`, `warm_up` | ✅ | REUSE | — |
| Product model | `src/catalog/models.py` | `Product`, `NormRef` | ✅ | EXTEND | `article`, `availability`, `sources`, `catalog_version` |
| Catalog search | `src/catalog/search.py` | `CatalogIndex.search` | ✅ | REUSE | горячая замена индекса при новой версии |
| Pagination | `src/core/dialog.py` | `PAGE_SIZE`, `_more` | ⚠️ | EXTEND | не повторять показанное между выдачами |
| CatalogSource | `src/ingest/build_kb.py`, `xlsx_reader.py` | `build`, `XlsxFile` | ⚠️ | EXTEND | обернуть в `OneCXlsxSource` |
| 1C import pipeline | `src/ingest/build_kb.py` | `_read_products`, `_check_headers`, `_to_int` | ⚠️ | EXTEND → NEW | `src/catalog_import/`: validate, match, diff, review, approve, apply |
| Matcher | — (есть `catalog/text.stems`, `trigrams`, `_normalize_name`) | — | ❌ | NEW | общий `catalog/matcher.py` для импорта и заказов |
| Diff / versions / rollback | — | — | ❌ | NEW | `catalog_versions`, `product_versions`, снимки |
| Photos / cards | `src/media/*` | `MediaService`, `PhotoStore`, `sync_to_kb` | ✅ | REUSE | обернуть в `CardSource` |
| Norm documents | `src/norms/documents.py` | `DOCUMENTS`, `for_audience` | ✅ | REUSE | реестр документов позже переедет в KB |
| Norm points | `src/norms/items.py` | `parse_838`, `parse_1057`, `ItemIndex` | ✅ | REUSE | `quantity` для 1057 → `quantity_source = norm` |
| Norm mappings | `src/ingest/norm_registry.py`, `norms/extract.py` | `build`, `extract` | ⚠️ | EXTEND | таблица маппингов со статусом REVIEW_REQUIRED |
| Norm selector | `src/agent/tools.py` | `_find_by_norm_code`, `_find_norm_item` | ⚠️ | EXTEND | `norms/selector.py`: документ → пункт → категория → товары |
| Norm reference | `src/norms/reference.py` | `explain`, `coverage` | ✅ | REUSE → перенос в KB | тексты уходят в Markdown |
| Specification | — | — | ❌ | NEW | `src/procurement/` |
| Excel | `src/orders/sinks.py` | `XlsxSink`, `_write_xlsx` | ✅ | EXTEND | `src/documents/excel.py` с шапкой и итогами |
| Word / PDF | — | — | ❌ | NEW | `python-docx`; PDF на выходе отложить |
| Order import (Excel/CSV/Word/PDF) | — (есть `XlsxFile`, `pypdf`) | — | ❌ | NEW | `src/order_import/` |
| Order evaluation | — | — | ❌ | NEW | `src/procurement/evaluator.py` |
| Order persistence | `src/core/storage.py` | `save_order`, `pending_orders` | ✅ | EXTEND | связь с версией каталога |
| Pre-order | `src/orders/service.py` | `OrderService.submit` | ⚠️ | EXTEND → REFACTOR | статусы п. 42, тексты «предзаказ» |
| Manager notification | `src/orders/sinks.py` | `CompositeSink`, `GoogleSheetsSink` | ⚠️ | EXTEND | `notifications`, `notification_attempts`, периодический повтор |
| CRMAdapter | `src/orders/sinks.py` | `Bitrix24Sink` (заглушка) | ⚠️ | NEW (интерфейс) | Tilda — после ответа Q1 |
| Knowledge Base / Obsidian | — | — | ❌ | NEW | `src/knowledge/obsidian/` |
| Telegram adapter | `src/adapters/telegram/bot.py` | `build_dispatcher`, `send`, `RetryOnNetworkError` | ✅ | REUSE | обработчик документов, отправка файлов |
| Web widget | `src/web/app.py`, `render.py`, `static/widget.js` | `create_app` | ✅ | REUSE | исправить R1; загрузка файла |
| MAX bot | `src/adapters/max/` | пусто | ❌ | NEW | ждёт токен |
| Telegram / MAX Mini App | — | — | ❌ | NEW | общий фронтенд над Core API |
| Admin panel | — | — | ❌ | NEW | `src/admin/`, отдельный сервис |
| Users / roles / auth | — | — | ❌ | NEW | ADMIN, MANAGER |
| Audit log | `src/observability/dialog_log.py` (только диалоги) | `DialogLogger` | ⚠️ | NEW | `audit_log` для действий в админке |
| File storage | — | — | ❌ | NEW | `data/uploads/` + таблица `files` |
| Observability | `src/observability/dialog_log.py`, `run.py dialogs` | `turn`, `read_dialogs` | ✅ | EXTEND | импорт, сопоставление, цены, уведомления |
| Deploy | `Dockerfile`, `docker-compose.yml` | `widget`, `telegram`, `ingest` | ⚠️ | EXTEND | сервис `admin`, выровнять зависимости (R6) |

---

## 5. Расхождения ТЗ и фактического проекта

### 5.1 Требуют ответа — не угадываю

| # | ТЗ говорит | В проекте | Что нужно |
|---|---|---|---|
| Q1 | Tilda — источник карточек, фото и CRM | сайт vdm.ru на 1С-Битрикс; фото и характеристики собираются со страниц Битрикса; Tilda нигде не упоминается | что именно на Tilda (новый сайт? лендинг? Tilda CRM?), есть ли API или экспорт, чей аккаунт |
| Q2 | промпты предоставляются отдельно (п. 76) | новых prompt-файлов v2 не найдено (§8) | где они |
| Q3 | новый проект `12-09-vdm-bot` | локальный клон; репозитория на GitHub нет; исходный репозиторий **публичный** | создавать ли репозиторий и приватный ли он |
| Q4 | выгрузка 1С с артикулом, SKU, единицей, статусом | в выгрузке 7 колонок: код 1С, наименование, URL, розничная цена, доступное количество, короткая ссылка, описание | будет ли новый формат выгрузки; образец |
| Q5 | vault Obsidian | vault нет | где он будет, кто ответственный сотрудник |
| Q6 | MAX Bot | токена нет, верификация юрлица не подтверждена | статус верификации |
| Q7 | загрузка заказа клиента Excel/CSV/Word/PDF | образцов нет | 3–5 реальных файлов (обезличенных) |
| Q8 | админка ADMIN/MANAGER | пользователей нет | список пользователей, нужен ли вход через что-то внешнее |
| Q9 | «institution_name» в task profile | профиль по построению **не принимает** название организации: организация собирается только при оформлении, после согласия на ПДн | допустимо ли хранить название учреждения в профиле до согласия |
| Q10 | `/start` сбрасывает задачу, не трогая историю заказов | `/start` очищает ещё и корзину | оставить или сохранять корзину |

### 5.2 Модель task profile: ТЗ ↔ `DialogProfile`

| Поле ТЗ | Есть | Как называется / что делать |
|---|---|---|
| `institution_type` | ✅ | `institution` |
| `institution_name` | ❌ | Q9 |
| `room` | ✅ | `room` |
| `zone` | ⚠️ | зона и кабинет объединены в `room` |
| `grade` | ⚠️ | классы попадают в `age` («5–9 класс») |
| `age_group` | ✅ | `age` |
| `goal` | ❌ | NEW |
| `norm_document` | ✅ | `norm_doc_ids` (список) + отдельно `asked_about_docs` |
| `norm_item` | ❌ | NEW |
| `budget`, `deadline` | ✅ | строки |
| `quantity` + `quantity_source` | ❌ | NEW (п. 13) |
| `preferences` | ❌ | NEW |
| `selected_products` | ❌ | корзина хранится отдельно |
| `rejected_products` | ✅ | `rejected` (последние 3 показанных при фразе-отказе) |
| `shown_products` | ✅ | `offered` (до 20) |
| `objections` (список) | ⚠️ | одно текущее `objection` + `objection_handled` |
| `offer` | ❌ | NEW |
| `stage` | ⚠️ | `diagnosis \| presentation \| objection \| closing`; в ТЗ режимы `CONSULTATION … PREORDER` |

### 5.3 Прочие расхождения

- **Нумерация этапов.** `ПЛАН.md` описывал 6 этапов CJM, маршрутизатор
  использует 4 стадии, ТЗ — 8 режимов продавца. Нужна одна согласованная модель.
- **Виджет без карточек** — договорённость с заказчиком. ТЗ п. 16 требует
  карточку с фото; в виджете фото уже показывается в подробной карточке, а в
  выдаче — нет.
- **Термин «заказ».** Существующий тест `test_dialog.py:115` проверяет слово
  «принят». Переход на «предзаказ» затронет его — нужен ARCHITECTURE_CHANGE.md.
- **Регрессионные тесты ТЗ** под своими именами отсутствуют, аналоги есть (§6).
- **`ПЛАН.md` обещает архивные товары** («помечаются архивными, не удаляются»),
  код их удаляет при пересборке.
- **README и память проекта.** README честно говорит «317 тестов» — совпадает с
  фактом. Более ранняя цифра «64 теста» устарела.

---

## 6. Тесты

**Факт:** 317 тестов в 21 файле, `317 passed` в исходнике за 73 с. Внешние сети
и данные заказчика тестам не нужны: синтетические товары и `tmp_path`.
Единственная фикстура с настоящей страницей, `tests/fixtures/product_card.html`,
лежит в `.gitignore`. Без неё тест `test_real_card_yields_full_size_photos`
**пропускается** — в чистом клоне с GitHub будет `316 passed, 1 skipped`.

| Файл | Тестов | Что покрывает |
|---|---|---|
| `test_media.py` | 44 | извлечение фото и характеристик, кэш, файлы, обход, перенос в KB |
| `test_routing.py` | 32 | правила маршрутизатора, разбор JSON, гейт карточек, интенты |
| `test_agent.py` | 29 | tool-calling, ПДн не уходят в модель, провайдеры, проверка цен и пунктов |
| `test_norms.py` | 26 | распознавание документов, привязка, аномалии, справка |
| `test_dialog.py` | 25 | корзина, согласие, оформление, удаление данных, профиль после рестарта |
| `test_telegram.py` | 24 | рендер, лимиты, фото, устойчивость к обрывам |
| `test_norm_lookup.py` | 17 | пункт + документ, аудитория, инструменты, справка |
| `test_audience.py` | 13 | сад/школа, основания в карточке |
| `test_search.py` | 13 | точный пункт, BM25, фильтры, морфология |
| `test_dialog_log.py` | 11 | журнал, псевдонимы, маскирование |
| `test_privacy.py` | 10 | маскирование ПДн |
| `test_profile.py` | 10 | профиль задачи |
| `test_cart_view.py` | 10 | корзина, перезапуск, спецификация xlsx |
| `test_html_text.py` | 9 | очистка описаний, состав комплекта |
| `test_norm_items.py` | 7 | разбор текстов приказов |
| `test_search_quality.py` | 7 | качество подбора на живых запросах |
| `test_catalog_tree.py`, `test_norm_registry.py`, `test_agent_cards.py`, `test_usage.py`, `test_web.py` | по 6 | дерево каталога, реестр 1057, сведение карточек, учёт токенов, HTTP виджета |

**Пробелы покрытия, важные для v2:**

- `ingest/xlsx_reader.py` и основной путь `ingest/build_kb.build` отдельными
  тестами не покрыты. Покрыт только перенос фото `_carry_over_collected`.
  Именно их переиспользует импорт 1С — закрыть в EPIC 2.
- `orders/service.retry_pending`, `GoogleSheetsSink` — без тестов.
- `web/app.py`: нет теста, что `/widget/message` отклоняет чужой `session_id` (R1).

**Требования ТЗ п. 12 и 65 → что уже есть:**

| Тест из ТЗ | Существующий аналог |
|---|---|
| `test_norm_838_not_1057` | `test_audience::test_school_gets_its_own_citation`, `test_norm_lookup::test_the_same_code_answers_in_its_own_order` |
| `test_norm_1057_not_838` | `test_norm_lookup::test_code_from_another_order_does_not_answer`, `::test_audience_alone_keeps_the_school_code_away`, `test_audience::test_preschool_does_not_get_school_citation` |
| `test_preschool_selection` | `test_audience::test_preschool_keeps_preschool_document`, `test_search_quality::test_audience_moves_school_items_down` |
| `test_school_selection` | `test_audience::test_school_gets_its_own_citation`, `::test_card_for_school_lists_all_items` |
| `test_existing_telegram` | `test_telegram.py` (24) |
| `test_existing_web` | `test_web.py` (6) |
| `test_consultant` | `test_agent::test_question_gets_the_consultant_not_the_salesman`, `::test_consultant_has_no_catalog_tools`, `test_routing::test_consultant_never_shows_cards` |
| `test_salesman` | `test_routing::test_obvious_replies_are_routed_without_the_model[*-sell]`, `test_agent::test_agent_calls_tool_then_answers` |
| `test_cart` | `test_dialog::test_add_and_quantity_changes_persist`, `test_cart_view.py` |
| `test_order` | `test_dialog::test_full_order_reaches_sink`, `::test_order_is_not_created_without_consent` |
| `test_838`, `test_1057`, `test_other_norm` | частично `test_norms.py`; `test_other_norm` (ФГОС ДО) — только распознавание документа |
| каталог, сопоставление, заказы, предзаказ (п. 65) | нет — это новая функциональность |

---

## 7. Риски и дефекты, найденные при аудите (не исправлены)

| # | Серьёзность | Что | Где | Предложение |
|---|---|---|---|---|
| **R1** | **высокая** | `/widget/message` и `/widget/action` принимают `session_id` любого вида от 8 до 64 символов (проверка hex32 есть только в `/widget/session`). Корзина, согласие, экспорт и удаление данных привязаны к `user_id` **без канала**. Числовой ID Telegram-пользователя (9–10 цифр) проходит проверку: отправив его в виджет, можно увидеть корзину человека, очистить её, выполнить `/delete_data` (удалит его историю и отзовёт согласие) и оформить заказ на его действующем согласии | `web/app.py:39-46`, `core/storage.py` (ключи `user_id`), `dialog._delete_data` | EPIC 0.5: pattern hex32 на `MessageIn`/`ActionIn` + тест. Решение о ключе с каналом — отдельно |
| R2 | средняя | `OrderService.retry_pending` нигде не вызывается, хотя пользователю пишется «мы повторим отправку». На практике смягчено: локальные Jsonl и Xlsx почти всегда срабатывают | `orders/service.py:85`, `dialog._submit` | EPIC 11: периодический повтор |
| R3 | средняя | одно SQLite-соединение на процесс с `check_same_thread=False` и без блокировки, при этом ядро вызывается из пула потоков (Telegram `to_thread`, sync-эндпоинты FastAPI). Возможны ошибки при одновременных ходах | `core/storage.py:91` | EPIC 2: блокировка или соединение на поток, WAL, `busy_timeout` |
| R4 | средняя (для v2 — высокая) | каталог закэширован (`lru_cache`), новая выгрузка видна только после перезапуска; словарь `_sessions` растёт без ограничения | `catalog/repository.py:26`, `core/dialog.py:203` | EPIC 4: горячая замена индекса; вытеснение старых сессий |
| R5 | низкая | `web/app.py` строит движок (загрузка 15 МБ каталога) при импорте модуля: `app = create_app()` | `web/app.py:129` | лениво, при необходимости |
| R6 | низкая | `Dockerfile` ставит свой список пакетов, отличный от `pyproject.toml` (нет `pypdf`, `pydantic` — транзитивно), образ на Python 3.12, разработка на 3.13; в venv исходника пакет стоит editable из git-URL | `Dockerfile` | ставить `pip install .` из `pyproject.toml` |
| R7 | низкая | объявлены, но не импортируются: `sqlalchemy`, `asyncpg`, `alembic`, `redis`, `httpx`, `openai`, `pydantic-settings`, `apscheduler`, `tenacity`; каталог `migrations/` пустой | `pyproject.toml` | решить вместе с Р1 плана |
| R8 | средняя | рядом с проектом лежит приватный ключ `id_rsa` (в `.gitignore`, в историю не попадал — проверено). По документам, в истории публичного репозитория остался отозванный токен Telegram (8 коммитов); клон историю наследует | корень исходной папки | в клон ключ не копировался; перед публикацией нового репозитория — gitleaks, решить судьбу истории |
| R9 | низкая | единственная фикстура с настоящей страницей в `.gitignore` → тест тихо пропускается в чистом клоне | `tests/fixtures/` | заменить синтетической страницей |
| R10 | средняя (для v2) | `ingest` перезаписывает `products.jsonl`: исчезнувшие товары пропадают, истории цен нет, копейки отбрасываются (`round`), пустой остаток = 0 | `ingest/build_kb.py:162, 341, 366` | EPIC 1–4 |
| R11 | низкая | два одновременных хода одного пользователя затирают `usage`/`route` в журнале (известный пробел №7) | `core/dialog.py:253` | — |
| R12 | низкая | номер заказа — 6 hex-символов в пределах суток: коллизии маловероятны, но возможны | `core/models.py:112` | при переходе на предзаказ — последовательность в БД |
| R13 | средняя | у виджета нет ограничения частоты запросов и аутентификации; каждый ход может стоить вызова модели | `web/app.py` | EPIC 5 / прод |

---

## 8. Промпты

### 8.1 Текущие промпты бота

| Файл | Роль | Используется |
|---|---|---|
| `guard.md` | границы, ПДн, запрет выдумывать, служебное не наружу | первым во всех ролях |
| `common.md` | кто мы, инструменты, 7 правил достоверности, 838/1057, правило состояния, приоритеты ответа | консультант и продавец |
| `consultant.md` | объясняет документы и пункты, не предлагает товар | роль `consult` |
| `salesman.md` | выяснение → подбор → возражения, типовые возражения, «кто перед тобой», чего не обещать | роль `sell` |
| `router.md` | JSON: branch, stage, objection, ready_to_see, facts | маршрутизатор на неоднозначных репликах |

### 8.2 Найденные «отдельно загруженные» материалы

| Файл | Что это на самом деле | Значение для v2 |
|---|---|---|
| `Промпт SMAIPL Иван Элти-beta.pdf` | промпт **действующего бота «Иван»** на сайте заказчика: только приказ 1057; данные из Google-таблицы через `get_filtered_google_table` и SQL `LIKE`; 4 колонки — код приказа, наименование по приказу, артикул Элти, название по артикулу; до 5 результатов; цен и наличия нет | источник реестра `Baza-Ivan-25-11-25.pdf`. Подтверждает: у заказчика «артикул Элти» = код 1С (примеры `1217`, `0Э-00001772`). Содержит ссылку на Google-таблицу заказчика — в документы не переносится |
| `Копия Вебинар сокол 23 августа. Промты.md` | промпт-**судья**, сравнивающий два анализа сделки (Luna/Sol) по 6 этапам CJM | не промпт бота. Даёт модель CJM: выявление → подбор → презентация → условия → фиксация шага → альтернатива |
| `Копия Вебинар =сокол=13 июля 2026.md` | расшифровка вебинара: схема «ветка → блок → профайл → финальные агенты» (генератор вопросов, презентатор, закрытие возражений, консультант, защитник) на примере видеостудии | образец, по которому построены текущие маршрутизатор и роли |

**Промпт-файлы v2, которые ТЗ п. 76 обещает «предоставить отдельно», не
найдены** ни в проекте, ни рядом с ним. Статус — UNKNOWN (Q2). По ТЗ промпты
меняются только после согласования архитектуры, поэтому ниже только перечень
конфликтов, без правок.

### 8.3 Конфликты текущих промптов с ТЗ

| # | Промпт | Сейчас | ТЗ | Вывод |
|---|---|---|---|---|
| P1 | `common.md` | зашита бизнес-истина: «Компания 33 года», описание документов и адресатов 838/1057, трактовка ФГОС ДО | п. 76: промпт не хранит бизнес-истину; п. 18: нормы — в KB | перенести в KB (EPIC 12), в промпте оставить ссылку на инструмент |
| P2 | `salesman.md` | «Выясни срок в первых сообщениях — он меняет весь подбор» | п. 14: минимум вопросов, начинать подбор, если можно | смягчить: срок — после первой выдачи |
| P3 | `salesman.md` | количеств и сумм нет, «больше трёх позиций не перечисляй» | п. 15: первая спецификация «позиция, артикул, название, цена, количество, сумма» | нужен инструмент спецификации (EPIC 8); лимит 3 относится к карточкам, а не к строкам спецификации |
| P4 | `salesman.md` | «ставь рядом код 1С `(код 1С 12345)`», перед показом код вырезается | п. 15–16: артикул показывается пользователю | решить, что показываем как «артикул» (Q4) |
| P5 | `router.md` | «сомневаешься между consult и sell — выбирай sell» | п. 8: консультант не подбирает и не продаёт | правило остаётся осмысленным, но смещает спорные вопросы к продавцу — проверить на сценариях |
| P6 | `consultant.md` | заканчивает ответ шагом «скажите, для сада или для школы, и какой кабинет» | п. 8: консультант не собирает задачу ради продажи | формально это переход к продавцу; оставить или убрать — решить вместе с промптами v2 |
| P7 | промпт «Иван» | до 5 результатов, фраза «У нас не представлен такой товар», режим DEBUG | п. 17: 3 позиции; п. 74: UNKNOWN / REVIEW_REQUIRED | если «Иван» заменяется (вопрос 4.3 заказчику), его правила в v2 не переносим |
| P8 | `router.md` / `profile.py` | 4 стадии | п. 7: 8 режимов; п. 77: DISCOVERY → … → PREORDER | одна модель режимов в коде, промпты — под неё |
| P9 | `guard.md` | «Не проси имя, телефон, почту, ИНН и адрес» | ТЗ: `institution_name` в профиле | см. Q9 |

---

## 9. Что не сломать: контрольный список baseline

После каждого EPIC должны проходить все 317 тестов и вручную:

1. Telegram: `/start` → «чем оснастить спортзал в саду» → карточки с фото →
   «В корзину» → `/cart` → «Оформить» → согласие → 6 полей → заказ у менеджера.
2. Виджет `/demo`: тот же путь, корзина переживает перезагрузку страницы.
3. «что значит приказ 838» → справка без товаров, аудитория не меняется.
4. «2.1.14 по приказу 1057» → «такого пункта нет, есть в 838».
5. «кабинет логопеда в детском саду» → ни одного основания из 838.
6. «дорого» после выдачи → карточек нет, пока возражение не снято.
7. Ключи модели пусты → «привет» не даёт товаров, предметный запрос — список и 3 карточки.
8. `/my_data`, `/delete_data` → данные удалены, заказы обезличены.
9. В журнале диалогов нет телефонов и почты.

---

## 10. Следующий шаг

Остановка по условию этапа. Для перехода к EPIC 1 нужны:

1. Ответы на Q1–Q10 (§5.1). Без Q1 (Tilda) и Q2 (промпты) архитектура этих
   частей не проектируется.
2. Согласие с решениями Р1–Р7 из IMPLEMENTATION_PLAN.md или выбор другого
   варианта.
3. Разрешение на EPIC 0.5 — исправление R1 и регрессионные тесты под именами ТЗ.
