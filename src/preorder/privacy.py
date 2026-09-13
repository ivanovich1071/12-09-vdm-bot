"""Данные субъекта в ядре: подключаются к выгрузке и удалению `Storage` (ФЗ-152).

- Задачи закупки и спецификации — история запросов: удаляются.
- Загруженные заказы — в файле клиента бывают контакты: удаляются вместе с файлом.
- Предзаказы — контакты удаляются, позиции остаются менеджеру для учёта, как у
  заказов бота.
"""

from __future__ import annotations

from order_import.service import OrderCoreService
from preorder.repository import PreorderRepository
from procurement.repository import ProcurementRepository


class CoreUserData:
    def __init__(
        self,
        procurement: ProcurementRepository,
        orders: OrderCoreService,
        preorders: PreorderRepository,
    ) -> None:
        self.procurement = procurement
        self.orders = orders
        self.preorders = preorders

    def export(self, user_id: str) -> dict[str, object]:
        return {
            "procurement_tasks": [task.to_dict() for task in self.procurement.tasks_of(user_id, 1000)],
            "specifications": [spec.to_dict() for spec in self.procurement.specifications_of(user_id, 1000)],
            "uploaded_orders": [order.to_dict() for order in self.orders.orders_of(user_id, 1000)],
            "preorders": self.preorders.export_owner(user_id),
        }

    def delete(self, user_id: str) -> None:
        self.procurement.delete_owner(user_id)
        self.orders.delete_owner(user_id)
        self.preorders.anonymize_owner(user_id)
