"""Настройки приложения.

Читаются из окружения и файла .env. Ничего не зашито в код: перенос прототипа
с нашего аккаунта Cloud.ru на аккаунт заказчика — это смена переменных окружения.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE = Path(".env")
# Переменные, которые больше не читаются. TELEGRAM_TOKEN заменён на TELEGRAM_BOT_TOKEN
# (NEXT-4.1): бот создаётся заново, и токен прежнего бота не должен подхватиться.
LEGACY_ENV = ("TELEGRAM_TOKEN",)


def load_env(path: Path = ENV_FILE) -> None:
    """Простое чтение .env: без внешних зависимостей и без перезаписи окружения."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    # Каталог
    kb_path: str = "data/kb/products.jsonl"
    storage_path: str = "data/vdm.sqlite3"
    # Импорт выгрузки 1С на проверку (EPIC 2). База отдельная от `storage_path`:
    # запись тысяч товаров не блокирует запись диалогов работающего бота, а
    # коммерческие данные не лежат рядом с персональными (D9).
    catalog_db_path: str = "data/catalog.sqlite3"
    uploads_dir: str = "data/uploads"
    import_max_mb: int = 50
    # Сопоставление позиции с каталогом (EPIC 3, `catalog/matcher.py`). Пороги —
    # доли общих триграмм названия. Автовыбор по похожему названию выключен:
    # в каталоге много почти одинаковых товаров, опечатку от соседа не отличить.
    match_auto_enabled: bool = False
    match_auto_threshold: float = 0.85
    match_review_threshold: float = 0.60
    match_ambiguity_margin: float = 0.05
    # Версии каталога (EPIC 4, D11). Без --force утверждение и откат останавливаются,
    # если исчезает больше доли товаров или цена меняется у большей доли: так ловится
    # обрезанный файл или сдвиг колонок. Процессы бота сверяют указатель раз в
    # CATALOG_RELOAD_SECONDS и перед каждым ходом.
    catalog_max_removed_share: float = 0.10
    catalog_max_price_changed_share: float = 0.30
    catalog_reload_seconds: float = 30.0
    # База ядра (NEXT-1…3): задачи закупки, спецификации, заказы клиентов, предзаказы,
    # сессии Core API. Пусто — файл хранилища бота: предзаказ несёт контакты, и
    # удаляются они там же, где корзина и согласия.
    core_db_path: str = ""
    # Загрузка готового заказа клиента (Excel, Word, PDF, CSV).
    order_upload_max_mb: int = 20
    # Отчёты менеджеру о предзаказах, пока CRM недоступна.
    preorders_dir: str = "data/preorders"
    # Ключ адаптеров к Core API (серверные каналы) и ключ ручных операций менеджера.
    # Пусто — соответствующие ручки отвечают 503: без ключа API не открывается.
    core_api_key: str = ""
    core_manager_key: str = ""

    # Модель. Провайдеров два: Cloud.ru — то, где всё будет работать у заказчика,
    # OpenRouter — то, где диалог можно проверить с машины разработки, когда
    # российское облако с неё не открывается. «auto» пробует их в этом порядке.
    llm_provider: str = "auto"  # auto | cloudru | openrouter
    cloudru_api_key: str = ""
    cloudru_base_url: str = "https://foundation-models.api.cloud.ru/v1"
    cloudru_model: str = "deepseek-ai/DeepSeek-V4-Flash"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "deepseek/deepseek-chat-v3-0324"
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 2000
    # Рубли за миллион токенов — по прайсу провайдера на 01.09.2026. Нужны,
    # чтобы в журнале стояла стоимость хода: выбирать модель по цифрам дешевле,
    # чем по впечатлению. Цены меняются, поэтому это настройка, а не константа.
    cloudru_price_in: float = 18.53
    cloudru_price_out: float = 37.08
    openrouter_price_in: float = 0.0
    openrouter_price_out: float = 0.0

    # Каналы
    # Токен бота от @BotFather — переменная TELEGRAM_BOT_TOKEN. Прежнее имя
    # TELEGRAM_TOKEN не читается: под ним в старых .env лежит токен прежнего бота,
    # и новый бот не должен молча запуститься с ним.
    telegram_token: str = ""
    # Транзит до api.telegram.org: `socks5://логин:пароль@хост:1080` либо `http://…`.
    # Нужен там, где сеть сервера до Telegram не доходит. Транзит передаёт уже
    # зашифрованный трафик: переписку он не видит и расшифровать TLS не может.
    # Пусто — бот идёт к Telegram напрямую, как и раньше.
    telegram_proxy: str = ""
    # Адрес бота для кнопки «Продолжить в Telegram» в виджете: https://t.me/<имя_бота>.
    # Пусто — кнопки в виджете нет. /start с идентификатором сессии сайта переносит
    # корзину из виджета в чат (`TelegramGateway._take_widget_cart`).
    telegram_bot_url: str = ""
    # Устаревшие переменные, заданные в окружении, — чтобы сказать о них при запуске.
    ignored_env: list[str] = field(default_factory=list)
    # Публичный HTTPS-адрес Mini App (…/miniapp). Задан — бот ставит кнопку меню «Приложение».
    telegram_miniapp_url: str = ""
    max_token: str = ""
    site_url: str = "https://vdm.ru"
    manager_contact: str = "+7 (495) 646-01-40, elti@vdm.ru"
    # Условия доставки на сайте заказчика. Бот стоимость и сроки не считает и не
    # называет: лестница тарифов зависит от региона и от того, частное лицо или
    # учреждение, — ошибиться легко, а обещание уже прозвучит. Отвечаем ссылкой
    # и передаём менеджеру.
    delivery_url: str = "https://vdm.ru/usloviya-raboty-/dostavka/"
    # Сумма, с которой заказчик оформляет доставку. Это не порог заказа: заявка
    # уходит менеджеру при любой сумме, а ниже порога к подтверждению добавляется
    # строка про самовывоз и варианты доставки.
    min_delivery_rub: int = 3000
    # Telegram id тестовых аккаунтов автотеста: их предзаказы менеджеру не отправляются.
    qa_user_ids: frozenset[str] = frozenset()

    # Заказы
    order_sink: str = "jsonl"  # jsonl | google_sheets | bitrix24 | smtp
    google_sheets_id: str = ""
    google_credentials_file: str = "secrets/google-service-account.json"
    orders_jsonl_path: str = "data/orders.jsonl"
    # Куда кладётся спецификация заказа в Excel — то, что менеджер заводит в 1С
    # руками, пока интеграции нет.
    orders_xlsx_dir: str = "data/orders"
    # Заявка письмом. Ящик-отправитель заводит заказчик, пароль живёт только в .env
    # на сервере. Получатель — рабочий ящик, куда менеджеры смотрят каждый день.
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    order_email_to: str = ""

    # Фотографии товаров с сайта заказчика.
    # Сайт принадлежит заказчику, бот делается для него же, поэтому сбор включён.
    # Осталось единственное ограничение — частота запросов: это рабочий сайт
    # с живыми покупателями, и класть его своей же выгрузкой незачем.
    media_enabled: bool = True
    # Куда складываются файлы снимков. Telegram не умеет забирать картинку
    # с vdm.ru сам, поэтому байты храним у себя.
    media_dir: str = "data/media"
    media_min_interval: float = 1.0
    media_user_agent: str = ""
    media_respect_robots: bool = False

    # Журнал диалогов (сырьё для доработки промптов)
    dialog_log_enabled: bool = True
    dialog_log_path: str = "data/dialogs"
    # По умолчанию персональные данные в журнал не пишутся. Отключать только
    # с письменного согласия заказчика и под конкретную задачу.
    dialog_log_mask_pdn: bool = True

    # Виджет
    widget_allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:8000"])
    widget_host: str = "0.0.0.0"
    widget_port: int = 8000

    @classmethod
    def from_env(cls) -> Settings:
        load_env()
        env = os.environ
        origins = env.get("WIDGET_ALLOWED_ORIGINS", "http://localhost:8000")
        return cls(
            kb_path=env.get("KB_PATH", cls.kb_path),
            storage_path=env.get("STORAGE_PATH", cls.storage_path),
            catalog_db_path=env.get("CATALOG_DB_PATH", cls.catalog_db_path),
            uploads_dir=env.get("UPLOADS_DIR", cls.uploads_dir),
            import_max_mb=int(env.get("IMPORT_MAX_MB", cls.import_max_mb)),
            # Включается только явным «1/true/yes»: опечатка в значении не должна
            # включать автовыбор, как было бы при проверке «не 0».
            match_auto_enabled=env.get("MATCH_AUTO_ENABLED", "0").strip().lower()
            in {"1", "true", "yes"},
            match_auto_threshold=float(env.get("MATCH_AUTO_THRESHOLD", cls.match_auto_threshold)),
            match_review_threshold=float(
                env.get("MATCH_REVIEW_THRESHOLD", cls.match_review_threshold)
            ),
            match_ambiguity_margin=float(
                env.get("MATCH_AMBIGUITY_MARGIN", cls.match_ambiguity_margin)
            ),
            catalog_max_removed_share=float(
                env.get("CATALOG_MAX_REMOVED_SHARE", cls.catalog_max_removed_share)
            ),
            catalog_max_price_changed_share=float(
                env.get("CATALOG_MAX_PRICE_CHANGED_SHARE", cls.catalog_max_price_changed_share)
            ),
            catalog_reload_seconds=float(
                env.get("CATALOG_RELOAD_SECONDS", cls.catalog_reload_seconds)
            ),
            core_db_path=env.get("CORE_DB_PATH", cls.core_db_path),
            order_upload_max_mb=int(env.get("ORDER_UPLOAD_MAX_MB", cls.order_upload_max_mb)),
            preorders_dir=env.get("PREORDERS_DIR", cls.preorders_dir),
            core_api_key=env.get("CORE_API_KEY", ""),
            core_manager_key=env.get("CORE_MANAGER_KEY", ""),
            llm_provider=env.get("LLM_PROVIDER", cls.llm_provider).strip().lower(),
            cloudru_api_key=env.get("CLOUDRU_API_KEY", ""),
            cloudru_base_url=env.get("CLOUDRU_BASE_URL", cls.cloudru_base_url),
            cloudru_model=env.get("CLOUDRU_MODEL", cls.cloudru_model),
            openrouter_api_key=env.get("OPENROUTER_API_KEY", ""),
            openrouter_base_url=env.get("OPENROUTER_BASE_URL", cls.openrouter_base_url),
            openrouter_model=env.get("OPENROUTER_MODEL", cls.openrouter_model),
            llm_timeout_seconds=float(env.get("LLM_TIMEOUT_SECONDS", cls.llm_timeout_seconds)),
            llm_max_tokens=int(env.get("LLM_MAX_TOKENS", cls.llm_max_tokens)),
            cloudru_price_in=float(env.get("CLOUDRU_PRICE_IN", cls.cloudru_price_in)),
            cloudru_price_out=float(env.get("CLOUDRU_PRICE_OUT", cls.cloudru_price_out)),
            openrouter_price_in=float(env.get("OPENROUTER_PRICE_IN", cls.openrouter_price_in)),
            openrouter_price_out=float(env.get("OPENROUTER_PRICE_OUT", cls.openrouter_price_out)),
            telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_proxy=env.get("TELEGRAM_PROXY", "").strip(),
            telegram_bot_url=env.get("TELEGRAM_BOT_URL", "").strip(),
            ignored_env=[name for name in LEGACY_ENV if env.get(name)],
            telegram_miniapp_url=env.get("TELEGRAM_MINIAPP_URL", ""),
            max_token=env.get("MAX_TOKEN", ""),
            site_url=env.get("SITE_URL", cls.site_url),
            manager_contact=env.get("MANAGER_CONTACT", cls.manager_contact),
            delivery_url=env.get("DELIVERY_URL", cls.delivery_url),
            min_delivery_rub=int(env.get("MIN_DELIVERY_RUB", cls.min_delivery_rub)),
            qa_user_ids=frozenset(part.strip() for part in env.get("QA_USER_IDS", "").split(",") if part.strip()),
            order_sink=env.get("ORDER_SINK", cls.order_sink),
            google_sheets_id=env.get("GOOGLE_SHEETS_ID", ""),
            google_credentials_file=env.get(
                "GOOGLE_CREDENTIALS_FILE", cls.google_credentials_file
            ),
            orders_jsonl_path=env.get("ORDERS_JSONL_PATH", cls.orders_jsonl_path),
            orders_xlsx_dir=env.get("ORDERS_XLSX_DIR", cls.orders_xlsx_dir),
            smtp_host=env.get("SMTP_HOST", ""),
            smtp_port=int(env.get("SMTP_PORT", cls.smtp_port)),
            smtp_user=env.get("SMTP_USER", ""),
            smtp_password=env.get("SMTP_PASSWORD", ""),
            smtp_from=env.get("SMTP_FROM", ""),
            order_email_to=env.get("ORDER_EMAIL_TO", ""),
            media_enabled=env.get("MEDIA_ENABLED", "1") not in {"0", "false", "no"},
            media_dir=env.get("MEDIA_DIR", cls.media_dir),
            media_min_interval=float(env.get("MEDIA_MIN_INTERVAL", cls.media_min_interval)),
            media_user_agent=env.get("MEDIA_USER_AGENT", ""),
            media_respect_robots=env.get("MEDIA_RESPECT_ROBOTS", "0")
            not in {"0", "false", "no"},
            dialog_log_enabled=env.get("DIALOG_LOG_ENABLED", "1") not in {"0", "false", "no"},
            dialog_log_path=env.get("DIALOG_LOG_PATH", cls.dialog_log_path),
            dialog_log_mask_pdn=env.get("DIALOG_LOG_MASK_PDN", "1") not in {"0", "false", "no"},
            widget_allowed_origins=[o.strip() for o in origins.split(",") if o.strip()],
            widget_host=env.get("WIDGET_HOST", cls.widget_host),
            widget_port=int(env.get("WIDGET_PORT", cls.widget_port)),
        )

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Значения, которых не должно быть в журнале (`observability/redact.py`)."""
        values = (
            self.telegram_token,
            self.core_api_key,
            self.core_manager_key,
            self.cloudru_api_key,
            self.openrouter_api_key,
            self.smtp_password,
            # В адресе транзита стоит пароль, а сам адрес попадает в текст сетевой
            # ошибки aiohttp — значит, и в журнал, если его не скрыть.
            self.telegram_proxy,
        )
        return tuple(value for value in values if value)

    @property
    def core_database_path(self) -> str:
        return self.core_db_path or self.storage_path

    @property
    def norm_items_path(self) -> Path:
        """Справочник пунктов приказов лежит рядом с каталогом."""
        return Path(self.kb_path).parent / "norm_items.json"

    @property
    def llm_enabled(self) -> bool:
        """Настроен ли хоть один провайдер, разрешённый текущим LLM_PROVIDER."""
        keys = {"cloudru": self.cloudru_api_key, "openrouter": self.openrouter_api_key}
        if self.llm_provider == "auto":
            return any(keys.values())
        return bool(keys.get(self.llm_provider))
