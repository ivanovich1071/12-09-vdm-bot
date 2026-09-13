# Ядро без каналов — NEXT-1…4

Порядок работ — D12: Procurement Core → Order Core → Core API → Telegram + Mini App.
Ядро не знает о Telegram, MAX и виджете: это проверяет тест
`test_procurement_core_does_not_know_channels`.

```text
Telegram · MAX · Web Widget · Mini Apps  →  Core API  →  Dialogue · Procurement · Order · Preorder
                                                            ↓
                                     Catalog (версии, EPIC 4) · Norms · Privacy · Storage
```

## NEXT-1. Procurement Core

**Путь:** запрос → задача → норматив → требования → подбор → количество →
спецификация → Excel / Word.

| Модуль | Что делает |
|---|---|
| `norms/repository.py` | `NormRepository`: документы и пункты приказов, количество по перечню, версия нормативной базы |
| `norms/selector.py` | `NormSelector`: документ и пункт по задаче, `REVIEW_REQUIRED` при неопределённости |
| `norms/mapping.py` | `NormMappingService`: привязки «пункт → товар» из снимка каталога, проверка `NORM_OK / NORM_MISMATCH / NORM_UNKNOWN / REVIEW_REQUIRED` |
| `procurement/models.py` | `ProcurementTask`, этапы, `QuantitySource`, `ProcurementRequirement`, `SelectionResult`, `Specification` |
| `procurement/discovery.py` | Разбор реплики в поля задачи — правилами профиля диалога и разделов каталога |
| `procurement/requirements.py` | Требование: пожелания пользователя отдельно от требований норматива |
| `procurement/selector.py` | Подбор: фильтры каталога → ранжирование → 3 позиции, замены, причина и уверенность |
| `procurement/quantity.py` | Количество и его источник: менеджер → пользователь → норматив → расчёт → по умолчанию |
| `procurement/specification.py` | Сборка, проверка целостности, сравнение с текущим каталогом |
| `procurement/repository.py`, `core/database.py`, `core/schema/0001_procurement.sql` | Задачи и спецификации в SQLite |
| `procurement/service.py` | `ProcurementService` — единая точка для каналов и Core API |
| `documents/` | `SpecificationExporter`: `ExcelExporter`, `WordExporter`, шаблоны `templates/` |

### Решения

- **Норматив.**
  - Цепочка ДОКУМЕНТ → ПУНКТ → КАТЕГОРИЯ → ТОВАР. Документ и пункт — из
    `norm_items.json`, привязки к товарам — из снимка каталога.
  - Школа → 838, детский сад → 1057. Чужой документ не подменяется своим:
    садик с приказом 838 получает `REVIEW_REQUIRED`
    (`DOCUMENT_INSTITUTION_CONFLICT`).
  - Пункт в двух документах без учреждения, пункт вне документа, пункт без
    товаров — `REVIEW_REQUIRED` с кодом причины.
  - Нормативный фильтр сужает выдачу, только когда норматив запрошен (назван
    документ, пункт или «по приказу») и однозначен. Без запроса документ по типу
    учреждения лишь называет основания товаров.
  - Отсутствие привязки — `NORM_UNKNOWN`, а не несоответствие: у половины
    каталога привязок нет. `NORM_MISMATCH` — когда данные говорят «другой пункт»
    или «чужой перечень».
- **Версия нормативной базы** `norms-<sha12>` — отпечаток реестра документов и
  справочника пунктов. Версия привязок — версия каталога.
- **Подбор.**
  - Жёсткие фильтры `CatalogService` — до ранжирования.
  - Три позиции за показ, показанное и отклонённое не повторяется. Сменилась
    задача — начинается новый подбор.
  - Искать можно, если известно помещение, слова о товаре, раздел, возраст или
    пункт. Одного типа учреждения мало: тогда вопрос `room`.
  - Модель (`GuardedRanker`) видит не больше 30 отфильтрованных кандидатов без
    описаний и может только переставить их.
  - При указанном возрасте первой идёт своя группа: фильтр пропускает и соседнюю.
- **Количество.**
  - Источник: `manager / user / norm / calculated / default`.
  - Нормативное — только целым числом из текста приказа (1057). Рассчитанное —
    по правилу пункта («по количеству детей в группе») и числу детей или групп.
  - Снаружи задаются только `user` и `manager`, `manager` — только менеджером.
- **Спецификация.**
  - Модель и канал передают коды и количества. Цену, наличие, пункт и итоги
    считает бэкенд из закреплённой версии каталога.
  - Фиксирует `catalog_version` и `norm_version`. После смены каталога
    `check_specification` показывает изменения, цены не пересчитываются.
    `revise_specification` — новая спецификация с `parent_id`, прежняя
    `SUPERSEDED`.
  - Без цены позиция не обнуляется: итог помечен неполным
    (`complete=false`, `missing_prices`).
- **Документы.** Excel и Word пишутся без новых зависимостей (zip + XML), колонки
  общие (`templates/specification.json`). Перед выгрузкой спецификация
  проверяется: суммы строк, итоги, номера, коды, версии.
- **Хранение.** `core/database.py` — база ядра, по умолчанию файл хранилища бота
  (`CORE_DB_PATH` пусто): так удаление данных субъекта проходит в одном файле.
  Миграции — `core/schema/`.

### Проверено на реальном каталоге (5 936 товаров, `data/kb` только чтение)

| Сценарий | Результат |
|---|---|
| Школа + кабинет информатики | 3 из 12 товаров раздела, «Показать ещё» без повторов, пункты 838 `NORM_OK` |
| ДОУ + группа 3–4 лет | 334 товара после фильтров, первыми — «Групповые помещения для детей 3–4 лет» |
| Детский сад, приказ 1057, пункт 1.5.1 | 48 товаров; количество по перечню: 6, 4, 2 шт. (`norm`) |
| Мячи для спортзала школы без норматива | 43 товара, нормативный фильтр не применён |
| Детский сад, спортзал, бюджет 15 000 ₽ | дороже бюджета исключено 25, предупреждение о сумме показа |
| Спецификация → Excel и Word | суммы строк и итог сходятся с `Specification`, артикулы и версии в документе |

Подбор после загрузки каталога — 0,02–0,24 с.

## NEXT-2. Order Core

**Путь:** файл → разбор → нормализация → сопоставление → текущий каталог → цена →
наличие → норматив → оценка → предзаказ → менеджер.

| Модуль | Что делает |
|---|---|
| `order_import/parsers.py` | `OrderParser`: `ExcelOrderParser`, `WordOrderParser`, `PdfOrderParser`, `CsvOrderParser` — только строки и ячейки |
| `order_import/normalizer.py` | `OrderNormalizer`: строка заголовка, колонки по точному названию, артикул, название, производитель, характеристики, размеры, количество, цена, норматив; исходные значения рядом |
| `order_import/matching.py` | `OrderMatcher` поверх `catalog/matcher.py` (EPIC 3), ручное сопоставление, подсказка модели не выше `MATCHED_REVIEW` |
| `order_import/evaluation.py` | `OrderEvaluation`: цена `PRICE_OK / PRICE_CHANGED / PRICE_NOT_FOUND`, наличие `AVAILABLE / NOT_AVAILABLE / UNKNOWN`, норматив, ошибки и предупреждения по строке, итог `READY / READY_WITH_WARNINGS / REVIEW_REQUIRED / REJECTED` |
| `order_import/service.py`, `repository.py`, `core/schema/0002_orders.sql` | `OrderCoreService`: загрузка с проверками, оценка на закреплённой версии каталога |
| `preorder/` | `Preorder` и статусы `DRAFT … CONFIRMED / REJECTED`, `PreorderService`, `NotificationChannel` и отчёт менеджеру в Excel, `CoreUserData` для ФЗ-152, `core/schema/0003_preorders.sql` |

### Решения

- **Загрузка.**
  - Принимаются `.xlsx`, `.docx`, `.pdf`, `.csv` до `ORDER_UPLOAD_MAX_MB`
    (20 МБ); сигнатура содержимого должна совпадать с расширением.
  - Файл хранится в `data/uploads` под sha256. Тот же файл того же пользователя —
    прежний заказ.
  - Нечитаемый файл сохраняется со статусом `FAILED` и текстом ошибки; скан PDF без
    текста — предупреждение `PDF_NO_TEXT` и `REJECTED` на оценке. OCR не делается.
- **Новых зависимостей нет:** xlsx и docx — zip + XML, PDF — уже подключённый
  `pypdf`. Word на входе читается без `python-docx`.
- **Колонки** узнаются по точному названию после чистки знаков: «Источник
  количества» — не количество, «Основание подбора» — не пункт. Прочерк — пустая
  ячейка. Нет количества — `None` и ошибка строки, количество не выдумывается.
- **Сопоставление.** Строгий порядок matcher EPIC 3. `MATCHED_REVIEW` и `AMBIGUOUS`
  не превращаются в соответствие: строка `REVIEW_REQUIRED`. Подсказка модели
  (`MatchAssistant`) принимается только из кандидатов matcher и только как
  `MATCHED_REVIEW`. Ручное сопоставление менеджера — `MATCHED_EXACT`, метод `manual`.
- **Цена** всегда из текущего каталога: исходная, текущая, разница в рублях и
  процентах. Нет цены в файле — `PRICE_OK` с предупреждением
  `DOCUMENT_PRICE_MISSING`; нет цены в каталоге — `PRICE_NOT_FOUND` и проверка.
- **Наличие.** `UNKNOWN` — отдельное предупреждение, никогда не `NOT_AVAILABLE`;
  количество больше остатка — `INSUFFICIENT_STOCK`.
- **Норматив.** Проверка только по привязкам (`NormMappingService.check`).
  Документ и пункт — из строки файла или из контекста заказа (учреждение,
  документ). Не запрошен — не проверяется.
- **Оценка** записывает `catalog_version` и `norm_version`. Все строки не найдены или
  нет строк — `REJECTED`.
- **Предзаказ.**
  - Не заказ бота: в таблицу заказов не пишется, `is_final_order = false`.
  - Из спецификации — цены сверяются с текущим каталогом, расхождение видно
    (`PRICE_CHANGED`, `CATALOG_CHANGED_SINCE_SPECIFICATION`); спецификация становится
    `FINAL`, задача — `ORDER`. Заменённую спецификацию взять нельзя.
  - Из заказа — только по оценке на текущей версии каталога
    (`EVALUATION_OUTDATED`), `REJECTED` не принимается.
  - Передача менеджеру — только с действующим согласием (`Storage.active_consent`)
    и контактами. Сбой уведомления: предзаказ остаётся `READY_FOR_MANAGER`, попытка
    записана, `retry_notifications` повторяет (до 5 раз).
- **Ручные операции (domain API для будущей админки):** начать проверку,
  сопоставить строку, задать количество, подтвердить, отклонить, записать
  перекодировку. Каждая пишется в `manual_decisions`; перекодировка каталог не
  меняет (`PROPOSED`).
- **ФЗ-152.** `Storage.add_user_data_hook`: `/my_data` и `/delete_data` охватывают ядро.
  Задачи, спецификации и загруженные заказы с файлами удаляются. У предзаказов
  удаляются контакты, позиции остаются для учёта — как у заказов бота.

### Проверено на реальном каталоге

`tests/test_core_real_catalog.py` — пропускается без `data/kb`. Товары выбираются по
свойствам, не по кодам.

| Проверка | Результат |
|---|---|
| Строки: код 1С, точное название, цена −10 %, опечатка, выдуманный товар, без количества | Excel, Word, PDF — одинаково: `MATCHED_EXACT`, `MATCHED_HIGH`, `PRICE_CHANGED`, `MATCHED_REVIEW`, `NOT_FOUND`, `QUANTITY_UNKNOWN`; итог `REVIEW_REQUIRED` |
| Спецификация 1057 → Excel → загрузка → оценка → предзаказ → менеджер | все строки `MATCHED_EXACT`, `PRICE_OK`, `NORM_OK`, сумма совпадает; `SENT_TO_MANAGER`, отчёт менеджеру записан |
| Время | разбор файла 0,02–0,03 с; первая оценка 0,6 с (индекс названий на версию), следующие 0,01–0,02 с |

## NEXT-3. Core API

Единый API между каналами и ядром. Два входа, один контракт:

- **в процессе** — `core_api/facade.py`, класс `CoreApi`. Им пользуется Telegram-адаптер;
- **по HTTP** — `core_api/http.py`, маршруты `/api/*` в том же приложении, что виджет
  (`web/app.py`). Одна версия каталога, одно хранилище.

Ядро для `/api` собирается при первом обращении (`core_api/composition.py`). Запуск
виджета и прежние ручки от него не зависят.

| Модуль | Что делает |
|---|---|
| `core_api/composition.py` | `build_core`: база ядра, нормативная база, сервисы закупки, заказов, предзаказов, сессий; подключает данные ядра к `Storage` (ФЗ-152) |
| `core_api/sessions.py`, `core/schema/0004_sessions.sql` | `CoreSession`, `SessionService`, `IdentityVerifier` |
| `core_api/dto.py` | Модели запросов и ответов, `extra="forbid"` |
| `core_api/render.py` | Ответ диалога в нейтральном виде: числа и коды, а не текст для конкретного канала |
| `core_api/facade.py` | `CoreApi` — все операции ядра |
| `core_api/http.py` | Маршруты, конверт ответа, формат ошибок, идентификатор запроса |

### Контракт

**Успех:**

```json
{"schema": "vdm.core.v1", "status": "ok", "request_id": "…", "session_id": "…",
 "task_id": "…", "catalog_version": "2026-09-13-001", "norm_version": "norms-…",
 "data": {}, "warnings": [{"code": "…", "message": "…", "details": {}}], "errors": []}
```

**Ошибка:**

```json
{"schema": "vdm.core.v1", "status": "error", "request_id": "…",
 "error": {"code": "SPECIFICATION_NOT_DRAFT", "message": "…", "details": {}}}
```

| HTTP | Когда |
|---|---|
| 400 | `InvalidRequest`: неизвестный товар, неверное количество, неподдерживаемый файл |
| 401 | нет сессии, неверный ключ адаптера, не прошла подпись канала |
| 403 | нет согласия на ПДн, неверный ключ менеджера |
| 404 | ресурса нет или он чужой — ответ одинаковый |
| 409 | переход статуса невозможен, оценка устарела, спецификация заменена |
| 422 | `VALIDATION_ERROR`: поля запроса, `details.fields` |
| 503 | ключ API или менеджера не настроен |
| 500 | `INTERNAL_ERROR`, без трассировки |

Формат ошибок действует только на `/api`: `/widget/*`, `/health`, `/media/*` отвечают
как раньше. Отдельного `/ask` в проекте нет: совместимый контракт каналов —
`/widget/*`, он не изменён.

### Доступ

| Кто | Как |
|---|---|
| Анонимный клиент | `POST /api/sessions` без тела — `user_ref` выдаёт сервер |
| Серверный адаптер (бот) | `X-Core-Api-Key` = `CORE_API_KEY`, в теле `channel` и `user_ref` |
| Публичный клиент канала (Mini App) | `credentials: {type, value}`; подпись проверяет `IdentityVerifier`, подключённый адаптером канала |
| Все запросы сессии | заголовок `X-Session-Id` |
| Менеджер | `X-Manager-Key` = `CORE_MANAGER_KEY`, `X-Manager-Actor` — логин латиницей |

`user_ref` — тот же ключ, что у корзины, согласия и данных субъекта в `Storage`.
Пользователь бота и его сессия Core API видят одну корзину, `/delete_data` удаляет
всё сразу. В коде API нет ни одной проверки канала: это проверяет
`test_core_api_has_no_channel_logic`.

### Маршруты

| Группа | Маршруты |
|---|---|
| Служебные | `GET /api/health`, `GET /api/catalog/status` |
| Сессии и ПДн | `POST /api/sessions`, `GET /api/sessions/{id}`, `POST /api/sessions/{id}/consent`, `GET` и `DELETE /api/sessions/{id}/data` |
| Диалог | `POST /api/dialogue/message`, `POST /api/dialogue/action` |
| Закупка | `POST /api/procurement/tasks`, `GET` и `PATCH /api/procurement/tasks/{id}`, `…/choose`, `…/reject`, `…/quantity`, `POST /api/procurement/select`, `POST /api/procurement/specification`, `GET /api/procurement/specifications/{id}`, `…/check`, `POST …/revise`, `GET …/export?format=xlsx\|docx` |
| Товар и корзина | `GET /api/products/{id}`, `GET /api/cart`, `POST /api/cart/items`, `DELETE /api/cart`, `POST /api/cart/specification` |
| Заказ и предзаказ | `POST /api/orders/upload?filename=…` (тело — байты файла), `GET /api/orders/{id}`, `POST …/evaluate`, `GET …/evaluation`, `POST /api/preorders`, `GET /api/preorders/{id}`, `POST …/send`, `GET /api/history` |
| Менеджер | `GET /api/manager/preorders?status=`, `GET /api/manager/preorders/{id}`, `POST …/review`, `…/confirm`, `…/reject`, `…/items/{line}/match`, `…/items/{line}/quantity`, `POST /api/manager/orders/{id}/items/{line}/match`, `POST /api/manager/recodings`, `GET /api/manager/decisions`, `POST /api/manager/notifications/retry` |

Загрузка файла идёт телом запроса, а не `multipart`: `python-multipart` не нужен.

### Версии

Всё, что связано с каталогом, возвращает `catalog_version`: подбор, товар,
спецификация (и заголовок `X-Catalog-Version` у выгрузки), заказ, оценка,
предзаказ. Нормативные операции — ещё и `norm_version`.

### ПДн

Отдельного механизма в API нет:

- согласие пишется в журнал `Storage.record_consent`;
- выгрузка и удаление — `Storage.export_user_data` и команда `/delete_data` диалога;
- модули ядра подключены к ним через `Storage.add_user_data_hook`, включая сессии API;
- контакты клиента принимаются только при передаче предзаказа менеджеру и только
  с действующим согласием.

## Финальная проверка ядра (контрольная точка 4)

- **Сквозной сценарий без Telegram — только через `/api`:** `tests/test_core_e2e.py`.
  - Путь: задача → `ProcurementTask` → норматив → подбор → спецификация →
    Excel и Word → загрузка → сопоставление → цена → наличие → норматив → оценка →
    предзаказ → согласие → менеджер → `CONFIRMED`.
  - Версия каталога одна от подбора до подтверждения.
- **Изоляция каналов:**
  - в отдельном процессе импорт `procurement`, `order_import`, `preorder`, `norms`,
    `documents`, `core_api` не загружает ни `aiogram`, ни `adapters`, ни `web`;
  - статические проверки импортов и отсутствия ветвлений по каналу —
    `test_procurement_core_does_not_know_channels`, `test_core_api_has_no_channel_logic`.
- **Регрессия прежних контрактов:** `/widget/*`, `/health`, `/media`, Telegram-рендер,
  диалог, корзина, заказ, 838 / 1057 — прежние тесты зелёные без изменений.
- **gitleaks** по коммитам NEXT-1…3 — утечек нет.
