"""Сборка ядра поверх движка диалога: одна точка, где соединяются домены.

Каталог — тот же `CatalogRuntime`, что у бота: API и диалог в одном процессе видят
одну версию и одинаково переживают горячую замену. Хранилище, согласия и удаление
данных — те же `Storage`; модули ядра подключаются к ним, а не заводят свои.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.config import Settings
from core.database import CoreDatabase
from core.dialog import DialogEngine
from core_api.sessions import SessionService, SessionUserData, SqliteSessionRepository
from norms.repository import FileNormRepository, NormRepository
from order_import.repository import SqliteOrderRepository
from order_import.service import MB, OrderCoreService
from preorder.notifications import FileNotificationChannel, NotificationChannel
from preorder.privacy import CoreUserData
from preorder.repository import SqlitePreorderRepository
from preorder.service import PreorderService
from procurement.repository import SqliteProcurementRepository
from procurement.service import ProcurementService


@dataclass
class CoreServices:
    settings: Settings
    engine: DialogEngine
    db: CoreDatabase
    norms: NormRepository
    procurement: ProcurementService
    orders: OrderCoreService
    preorders: PreorderService
    sessions: SessionService


def build_core(
    settings: Settings,
    engine: DialogEngine,
    *,
    norms: NormRepository | None = None,
    notifier: NotificationChannel | None = None,
) -> CoreServices:
    # Без CORE_DB_PATH — файл хранилища бота, даже если он подменён (тесты, другой STORAGE_PATH).
    db = CoreDatabase(settings.core_db_path or engine.storage.path)
    norms = norms or FileNormRepository.from_file(settings.norm_items_path)
    runtime = engine.runtime
    procurement_repository = SqliteProcurementRepository(db)
    procurement = ProcurementService(procurement_repository, runtime, norms)
    orders = OrderCoreService(
        SqliteOrderRepository(db),
        runtime,
        norms,
        Path(settings.uploads_dir) / "orders",
        max_bytes=settings.order_upload_max_mb * MB,
    )
    preorder_repository = SqlitePreorderRepository(db)
    preorders = PreorderService(
        preorder_repository,
        runtime,
        procurement,
        orders,
        engine.storage.active_consent,
        notifier or FileNotificationChannel(Path(settings.preorders_dir)),
    )
    sessions = SessionService(SqliteSessionRepository(db))
    engine.storage.add_user_data_hook(CoreUserData(procurement_repository, orders, preorder_repository))
    engine.storage.add_user_data_hook(SessionUserData(sessions))
    return CoreServices(settings, engine, db, norms, procurement, orders, preorders, sessions)
