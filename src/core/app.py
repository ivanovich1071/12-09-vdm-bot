"""Сборка приложения: одна точка, где всё соединяется.

Адаптеры получают готовый движок диалога и ничего не знают о том, как он собран,
поэтому замена хранилища, приёмника заказов или модели не расходится по каналам.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.agent import SalesAgent
from agent.providers import build_router, warm_up
from catalog.runtime import CatalogRuntime
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from media.fetcher import DEFAULT_USER_AGENT, PageFetcher
from media.files import PhotoStore
from media.prefetch import MediaPrefetcher
from media.service import MediaService
from observability.dialog_log import DialogLogger
from orders.service import OrderService, build_sink

log = logging.getLogger(__name__)


def build_engine(
    settings: Settings | None = None,
    warm_llm: bool = False,
    watch_catalog: bool | None = None,
) -> DialogEngine:
    """Готовый движок диалога.

    `warm_llm` включают долгоживущие каналы — Telegram и виджет. Разовым
    командам (`run.py search`) прогрев ни к чему: они и так живут секунду.

    Каталог — текущая версия по указателю (`catalog/current.py`); повреждённый
    указатель останавливает старт. Долгоживущие каналы (`watch_catalog`, по
    умолчанию как `warm_llm`) сверяют указатель в фоне и меняют версию без
    перезапуска.
    """
    settings = settings or Settings.from_env()
    runtime = CatalogRuntime.open(settings.kb_path)
    index = runtime.state.index
    storage = Storage(settings.storage_path)
    # Срок хранения переписки соблюдается при каждом старте, а не только при
    # чтении: тот, кто перестал писать, сам за собой не почистит.
    expired = storage.purge_expired_dialogs()
    if expired:
        log.info("Удалено разговоров по сроку хранения: %s", expired)
    orders = OrderService(storage, build_sink(settings))

    router = build_router(settings)
    if warm_llm and router.configured:
        warm_up(router)
        # Проверка заблокированных провайдеров в фоне: блок снимаем сами, раз в
        # 20 секунд, а не чьим-то следующим ходом.
        router.start_pinger()
    agent = None
    if router.configured:
        # Движок агенту нужен, но сам он создаётся ниже — проставим после.
        agent = SalesAgent(engine=None, router=router)

    dialog_log = DialogLogger(
        path=Path(settings.dialog_log_path),
        enabled=settings.dialog_log_enabled,
        mask_personal_data=settings.dialog_log_mask_pdn,
    )
    fetcher = PageFetcher(
        user_agent=settings.media_user_agent or DEFAULT_USER_AGENT,
        min_interval=settings.media_min_interval,
        respect_robots=settings.media_respect_robots,
    )
    media = MediaService(
        storage=storage,
        fetcher=fetcher,
        enabled=settings.media_enabled,
        photos=PhotoStore(fetcher=fetcher, root=Path(settings.media_dir)),
    )
    if settings.media_enabled:
        # Снимки собираются в фоне: ход диалога за ними в сеть не ходит.
        # Поток нужен только долгоживущим каналам — разовая команда успеет
        # закончиться раньше, чем сборщик доберётся до первого товара.
        media.prefetch = MediaPrefetcher(media)
        if warm_llm if watch_catalog is None else watch_catalog:
            media.prefetch.start()
    engine = DialogEngine(
        runtime, storage, orders, settings, agent=agent, dialog_log=dialog_log, media=media
    )
    if agent is not None:
        agent.engine = engine
    if warm_llm if watch_catalog is None else watch_catalog:
        runtime.start_watching(settings.catalog_reload_seconds)
    log.info(
        "Каталог загружен: версия %s, %s позиций, приёмник заказов — %s, журнал диалогов — %s",
        runtime.state.version or "legacy",
        len(index.products),
        getattr(orders.sink, "name", "?"),
        settings.dialog_log_path if settings.dialog_log_enabled else "выключен",
    )
    if not settings.media_enabled:
        log.info(
            "Догрузка фотографий с сайта выключена (MEDIA_ENABLED=0): "
            "показываем только то, что уже собрано в базе знаний."
        )
    return engine
