"""NEXT-2: предзаказ — создание, передача менеджеру, уведомления, решения менеджера, ПДн."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from catalog.runtime import CatalogRuntime
from core.database import CoreDatabase
from core.errors import Conflict, Forbidden, InvalidRequest, NotFound
from core.models import Customer
from core.storage import Storage
from core_fixtures import Clock, norm_items, state
from documents.xlsx import BOLD  # noqa: F401 — отчёт менеджера пишется тем же writer
from ingest.xlsx_reader import XlsxFile
from norms.repository import FileNormRepository
from order_import.models import OrderContext
from order_import.repository import SqliteOrderRepository
from order_import.service import OrderCoreService
from preorder.models import NotificationStatus, PreorderSource, PreorderStatus
from preorder.notifications import FileNotificationChannel
from preorder.privacy import CoreUserData
from preorder.repository import SqlitePreorderRepository
from preorder.service import PreorderService
from privacy.consent import CONSENT_VERSION
from procurement.models import SpecificationStatus, Stage
from procurement.repository import SqliteProcurementRepository
from procurement.service import ProcurementService
from test_order_core import HEADER, xlsx

OWNER = "user-1"
CUSTOMER = Customer(name="Контактное лицо", phone="+7 900 000-00-00", organization="МБДОУ № 5")


class Recorder:
    name = "recorder"

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.sent: list[str] = []

    def send(self, preorder) -> None:  # noqa: ANN001
        if self.failures:
            self.failures -= 1
            raise ConnectionError("CRM недоступна")
        self.sent.append(preorder.id)


@dataclass
class Env:
    runtime: CatalogRuntime
    storage: Storage
    procurement: ProcurementService
    orders: OrderCoreService
    preorders: PreorderService
    repository: SqlitePreorderRepository
    notifier: Recorder
    uploads: Path


def build(tmp_path, notifier=None) -> Env:
    runtime = CatalogRuntime(state())
    path = tmp_path / "vdm.sqlite3"
    storage = Storage(path)
    db = CoreDatabase(path)
    norms = FileNormRepository(norm_items())
    procurement = ProcurementService(SqliteProcurementRepository(db), runtime, norms, clock=Clock())
    orders = OrderCoreService(SqliteOrderRepository(db), runtime, norms, tmp_path / "uploads", clock=Clock())
    repository = SqlitePreorderRepository(db)
    notifier = notifier or Recorder()
    preorders = PreorderService(repository, runtime, procurement, orders, storage.active_consent, notifier, clock=Clock())
    storage.add_user_data_hook(CoreUserData(SqliteProcurementRepository(db), orders, repository))
    return Env(runtime, storage, procurement, orders, preorders, repository, notifier, tmp_path / "uploads")


@pytest.fixture
def env(tmp_path):
    return build(tmp_path)


def specification(env: Env):
    task = env.procurement.create_task(OWNER, "test", text="Детский сад, по приказу 1057 пункт 1.5.1")
    result = env.procurement.select(task.id, OWNER)
    env.procurement.choose(task.id, OWNER, [item.product_id for item in result.items])
    return env.procurement.build_specification(task.id, OWNER)


def evaluated_order(env: Env, rows):
    order = env.orders.upload(OWNER, "test", "order.xlsx", xlsx(rows), OrderContext("preschool", "order_1057"))
    env.orders.evaluate(order.id, OWNER)
    return order


def consent(env: Env) -> None:
    env.storage.record_consent(OWNER, "test", CONSENT_VERSION, "granted")


def statuses(preorder):
    return [event.status for event in preorder.history]


# --- Создание -----------------------------------------------------------------------


def test_preorder_from_specification(env):
    spec = specification(env)
    preorder = env.preorders.create_from_specification(spec.id, OWNER, "test")
    assert preorder.status is PreorderStatus.READY_FOR_MANAGER and preorder.source is PreorderSource.SPECIFICATION
    assert statuses(preorder) == [PreorderStatus.DRAFT, PreorderStatus.PRICE_CHECKED, PreorderStatus.READY_FOR_MANAGER]
    assert preorder.totals.amount == spec.totals.amount and preorder.catalog_version == spec.catalog_version
    assert preorder.to_dict()["is_final_order"] is False and not preorder.review_required
    assert env.procurement.get_specification(spec.id, OWNER).status is SpecificationStatus.FINAL
    assert env.procurement.get_task(spec.task_id, OWNER).stage is Stage.ORDER
    assert env.storage.orders_of(OWNER) == []  # предзаказ — не заказ бота
    assert env.preorders.get(preorder.id, OWNER) == preorder


def test_price_change_after_specification_is_visible(env):
    spec = specification(env)
    env.runtime.replace(state("2026-09-13-002", B2={"price": 9000}))
    preorder = env.preorders.create_from_specification(spec.id, OWNER, "test")
    mat = next(item for item in preorder.items if item.product_id == "B2")
    assert (mat.price_status, mat.unit_price, mat.document_price) == ("PRICE_CHANGED", 9000, 8164)
    assert "PRICE_CHANGED" in mat.flags and preorder.catalog_version == "2026-09-13-002"
    assert [notice.code for notice in preorder.warnings] == ["CATALOG_CHANGED_SINCE_SPECIFICATION"]


def test_superseded_specification_is_refused(env):
    spec = specification(env)
    env.runtime.replace(state("2026-09-13-002", B2={"price": 9000}))
    env.procurement.revise_specification(spec.id, OWNER)
    with pytest.raises(Conflict) as error:
        env.preorders.create_from_specification(spec.id, OWNER, "test")
    assert error.value.code == "SPECIFICATION_SUPERSEDED"


def test_preorder_from_order_needs_current_evaluation(env):
    order = env.orders.upload(OWNER, "test", "o.xlsx", xlsx([HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "908"]]))
    with pytest.raises(Conflict) as error:
        env.preorders.create_from_order(order.id, OWNER, "test")
    assert error.value.code == "EVALUATION_REQUIRED"
    env.orders.evaluate(order.id, OWNER)
    env.runtime.replace(state("2026-09-13-002"))
    with pytest.raises(Conflict) as error:
        env.preorders.create_from_order(order.id, OWNER, "test")
    assert error.value.code == "EVALUATION_OUTDATED"
    env.orders.evaluate(order.id, OWNER)
    preorder = env.preorders.create_from_order(order.id, OWNER, "test")
    assert statuses(preorder) == [
        PreorderStatus.DRAFT,
        PreorderStatus.IMPORTED,
        PreorderStatus.MATCHED,
        PreorderStatus.PRICE_CHECKED,
        PreorderStatus.READY_FOR_MANAGER,
    ]
    assert preorder.catalog_version == "2026-09-13-002" and preorder.items[0].product_id == "B1"


def test_rejected_order_is_refused(env):
    order = evaluated_order(env, [HEADER, ["1", "", "Телескоп космический", "1", "1"]])
    with pytest.raises(Conflict) as error:
        env.preorders.create_from_order(order.id, OWNER, "test")
    assert error.value.code == "ORDER_REJECTED"


# --- Передача менеджеру -------------------------------------------------------------


def test_sending_requires_consent_and_contacts(env):
    preorder = env.preorders.create_from_specification(specification(env).id, OWNER, "test")
    with pytest.raises(Forbidden) as error:
        env.preorders.send_to_manager(preorder.id, OWNER, CUSTOMER)
    assert error.value.code == "CONSENT_REQUIRED"
    consent(env)
    with pytest.raises(InvalidRequest):
        env.preorders.send_to_manager(preorder.id, OWNER, Customer(name="Без телефона"))
    sent = env.preorders.send_to_manager(preorder.id, OWNER, CUSTOMER)
    assert sent.status is PreorderStatus.SENT_TO_MANAGER and sent.notification is NotificationStatus.SENT
    assert env.notifier.sent == [preorder.id] and sent.customer["phone"] == CUSTOMER.phone and sent.consent_id
    assert env.preorders.send_to_manager(preorder.id, OWNER, CUSTOMER).status is PreorderStatus.SENT_TO_MANAGER


def test_failed_notification_keeps_preorder_and_retries(tmp_path):
    env = build(tmp_path, Recorder(failures=1))
    consent(env)
    preorder = env.preorders.create_from_specification(specification(env).id, OWNER, "test")
    failed = env.preorders.send_to_manager(preorder.id, OWNER, CUSTOMER)
    assert failed.status is PreorderStatus.READY_FOR_MANAGER
    assert (failed.notification, failed.notification_error) == (NotificationStatus.FAILED, "CRM недоступна")
    assert env.preorders.retry_notifications() == 1
    retried = env.preorders.get(preorder.id, OWNER)
    assert retried.status is PreorderStatus.SENT_TO_MANAGER and retried.notification is NotificationStatus.SENT
    assert env.preorders.retry_notifications() == 0


def test_file_channel_writes_manager_report(env, tmp_path):
    consent(env)
    order = evaluated_order(env, [HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "800"]])
    preorder = env.preorders.create_from_order(order.id, OWNER, "test")
    env.repository.save(preorder)
    channel = FileNotificationChannel(tmp_path / "preorders")
    from dataclasses import asdict, replace

    channel.send(replace(preorder, customer=asdict(CUSTOMER)))
    with XlsxFile(tmp_path / "preorders" / f"{preorder.id}.xlsx") as book:
        rows = list(book.rows(0))
    header = next(row for row in rows if row.get("H") == "Текущая цена")
    data = rows[rows.index(header) + 1]
    assert (data["D"], data["G"], data["H"], data["L"]) == ("B1", "800", "908", "PRICE_CHANGED")
    assert any(row.get("B") == CUSTOMER.phone for row in rows)
    assert (tmp_path / "preorders" / "preorders.jsonl").read_text(encoding="utf-8").count(preorder.id) == 1


# --- Менеджер ------------------------------------------------------------------------


def test_manager_review_flow(env):
    consent(env)
    order = evaluated_order(env, [HEADER, ["1", "", "Мат гимнастический складной", "1", "8000"], ["2", "B1", "Мяч баскетбольный № 3", "", "908"]])
    preorder = env.preorders.create_from_order(order.id, OWNER, "test")
    assert preorder.review_required
    env.preorders.send_to_manager(preorder.id, OWNER, CUSTOMER)
    assert [p.id for p in env.preorders.manager_queue()] == [preorder.id]
    env.preorders.start_review(preorder.id, "manager")
    with pytest.raises(Conflict) as error:
        env.preorders.confirm(preorder.id, "manager")
    assert error.value.code == "PREORDER_HAS_UNRESOLVED_LINES"
    matched = env.preorders.manual_match(preorder.id, 1, "B2", "manager")
    assert (matched.items[0].product_id, matched.items[0].unit_price, matched.items[0].total_price) == ("B2", 8164, 8164)
    updated = env.preorders.set_quantity(preorder.id, 2, 3, "manager")
    assert (updated.items[1].quantity, updated.items[1].quantity_source, updated.items[1].total_price) == (3, "manager", 2724)
    confirmed = env.preorders.confirm(preorder.id, "manager", "Согласовано")
    assert confirmed.status is PreorderStatus.CONFIRMED and confirmed.manager_comment == "Согласовано"
    assert [e.status for e in confirmed.history][-2:] == [PreorderStatus.MANAGER_REVIEW, PreorderStatus.CONFIRMED]
    assert {d["kind"] for d in env.preorders.decisions()} == {"match", "quantity", "approval"}


def test_transitions_are_enforced(env):
    preorder = env.preorders.create_from_specification(specification(env).id, OWNER, "test")
    with pytest.raises(Conflict) as error:
        env.preorders.confirm(preorder.id, "manager")
    assert error.value.code == "PREORDER_TRANSITION_NOT_ALLOWED"
    with pytest.raises(Conflict):
        env.preorders.manual_match(preorder.id, 1, "B2", "manager")
    with pytest.raises(InvalidRequest):
        env.preorders.reject(preorder.id, "manager", " ")
    assert env.preorders.reject(preorder.id, "manager", "Нет бюджета").status is PreorderStatus.REJECTED


def test_recoding_is_recorded_not_applied(env):
    decision = env.preorders.record_recoding("OLD-1", "B2", "manager", "перекодировка в 1С")
    stored = env.preorders.decisions("recoding")[0]
    assert stored["id"] == decision and stored["status"] == "PROPOSED" and stored["payload"]["new_sku"] == "B2"
    with pytest.raises(InvalidRequest):
        env.preorders.record_recoding("B2", "B2", "manager")


def test_owner_isolation(env):
    preorder = env.preorders.create_from_specification(specification(env).id, OWNER, "test")
    with pytest.raises(NotFound):
        env.preorders.get(preorder.id, "someone-else")


# --- Права субъекта ПДн ---------------------------------------------------------------


def test_personal_data_export_and_delete_cover_core(env):
    consent(env)
    order = evaluated_order(env, [HEADER, ["1", "B1", "Мяч баскетбольный № 3", "2", "908"]])
    preorder = env.preorders.create_from_order(order.id, OWNER, "test")
    env.preorders.send_to_manager(preorder.id, OWNER, CUSTOMER)
    specification(env)

    exported = env.storage.export_user_data(OWNER)
    assert exported["preorders"][0]["customer"]["phone"] == CUSTOMER.phone
    assert exported["uploaded_orders"][0]["id"] == order.id and exported["procurement_tasks"]

    stored_file = Path(env.orders.repository.get_order(order.id).source_file.storage_path)
    assert stored_file.exists()
    env.storage.delete_user_data(OWNER, "test")
    kept = env.repository.get(preorder.id)
    assert kept.customer is None and kept.owner == "deleted" and kept.items
    assert env.orders.repository.get_order(order.id) is None and not stored_file.exists()
    assert env.storage.export_user_data(OWNER)["procurement_tasks"] == []
    assert env.storage.active_consent(OWNER) is None
