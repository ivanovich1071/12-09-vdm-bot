"""Хранение задач закупки и спецификаций (D4: SQL только здесь)."""

from __future__ import annotations

import json
from typing import Protocol

from catalog.models import Availability
from core.database import CoreDatabase
from core.errors import Notice
from norms.mapping import NormCheckStatus
from procurement.models import (
    ProcurementTask,
    QuantitySource,
    Specification,
    SpecificationItem,
    SpecificationStatus,
    SpecificationTotals,
)


class ProcurementRepository(Protocol):
    def save_task(self, task: ProcurementTask) -> None: ...

    def get_task(self, task_id: str) -> ProcurementTask | None: ...

    def tasks_of(self, owner: str, limit: int = 20) -> list[ProcurementTask]: ...

    def save_specification(self, spec: Specification) -> None: ...

    def get_specification(self, spec_id: str) -> Specification | None: ...

    def specifications_of(self, owner: str, limit: int = 20) -> list[Specification]: ...

    def set_specification_status(self, spec_id: str, status: SpecificationStatus) -> None: ...

    def delete_owner(self, owner: str) -> int: ...


_ITEM_COLUMNS = (
    "specification_id, line_no, product_id, article, name, quantity, quantity_source, "
    "quantity_note, unit, unit_price, total_price, availability, norm_document, norm_item, "
    "norm_item_title, norm_status, selection_reason, url"
)


class SqliteProcurementRepository:
    def __init__(self, db: CoreDatabase) -> None:
        self.db = db

    # --- Задачи ---------------------------------------------------------------

    def save_task(self, task: ProcurementTask) -> None:
        with self.db.write() as db:
            db.execute(
                "INSERT INTO procurement_tasks(id, owner, channel, stage, payload, created_at, "
                "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "stage = excluded.stage, payload = excluded.payload, updated_at = excluded.updated_at",
                (
                    task.id,
                    task.owner,
                    task.channel,
                    str(task.stage),
                    json.dumps(task.to_dict(), ensure_ascii=False),
                    task.created_at,
                    task.updated_at,
                ),
            )

    def get_task(self, task_id: str) -> ProcurementTask | None:
        with self.db.read() as db:
            row = db.execute(
                "SELECT payload FROM procurement_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return ProcurementTask.from_dict(json.loads(row["payload"])) if row else None

    def tasks_of(self, owner: str, limit: int = 20) -> list[ProcurementTask]:
        with self.db.read() as db:
            rows = db.execute(
                "SELECT payload FROM procurement_tasks WHERE owner = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        return [ProcurementTask.from_dict(json.loads(row["payload"])) for row in rows]

    # --- Спецификации ---------------------------------------------------------

    def save_specification(self, spec: Specification) -> None:
        with self.db.write() as db:
            db.execute(
                "INSERT INTO specifications(id, task_id, owner, status, catalog_version, "
                "catalog_sha256, norm_version, parent_id, header, totals, warnings, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spec.id,
                    spec.task_id,
                    spec.owner,
                    str(spec.status),
                    spec.catalog_version,
                    spec.catalog_sha256,
                    spec.norm_version,
                    spec.parent_id,
                    json.dumps(spec.header, ensure_ascii=False),
                    json.dumps(spec.totals.to_dict(), ensure_ascii=False),
                    json.dumps([notice.to_dict() for notice in spec.warnings], ensure_ascii=False),
                    spec.created_at,
                ),
            )
            db.executemany(
                f"INSERT INTO specification_items({_ITEM_COLUMNS}) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        spec.id,
                        item.line_no,
                        item.product_id,
                        item.article,
                        item.name,
                        item.quantity,
                        str(item.quantity_source),
                        item.quantity_note,
                        item.unit,
                        item.unit_price,
                        item.total_price,
                        str(item.availability),
                        item.norm_document,
                        item.norm_item,
                        item.norm_item_title,
                        str(item.norm_status),
                        item.selection_reason,
                        item.url,
                    )
                    for item in spec.items
                ],
            )

    def get_specification(self, spec_id: str) -> Specification | None:
        with self.db.read() as db:
            row = db.execute("SELECT * FROM specifications WHERE id = ?", (spec_id,)).fetchone()
            if row is None:
                return None
            items = db.execute(
                f"SELECT {_ITEM_COLUMNS} FROM specification_items WHERE specification_id = ? "
                "ORDER BY line_no",
                (spec_id,),
            ).fetchall()
        return _specification(row, items)

    def specifications_of(self, owner: str, limit: int = 20) -> list[Specification]:
        with self.db.read() as db:
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM specifications WHERE owner = ? ORDER BY created_at DESC LIMIT ?",
                    (owner, limit),
                ).fetchall()
            ]
        return [spec for spec_id in ids if (spec := self.get_specification(spec_id))]

    def set_specification_status(self, spec_id: str, status: SpecificationStatus) -> None:
        with self.db.write() as db:
            db.execute("UPDATE specifications SET status = ? WHERE id = ?", (str(status), spec_id))

    def delete_owner(self, owner: str) -> int:
        """Задачи и спецификации пользователя — по требованию субъекта ПДн.

        Персональных данных в них нет, но это история его запросов: удаляется вместе
        с перепиской, как требует `/delete_data`.
        """
        with self.db.write() as db:
            specs = [
                row["id"]
                for row in db.execute("SELECT id FROM specifications WHERE owner = ?", (owner,))
            ]
            db.executemany(
                "DELETE FROM specification_items WHERE specification_id = ?", [(s,) for s in specs]
            )
            db.execute("UPDATE specifications SET parent_id = NULL WHERE owner = ?", (owner,))
            db.execute("DELETE FROM specifications WHERE owner = ?", (owner,))
            cursor = db.execute("DELETE FROM procurement_tasks WHERE owner = ?", (owner,))
            return cursor.rowcount or 0


def _specification(row, items) -> Specification:  # noqa: ANN001 — sqlite3.Row
    return Specification(
        id=row["id"],
        task_id=row["task_id"],
        owner=row["owner"],
        status=SpecificationStatus(row["status"]),
        created_at=row["created_at"],
        catalog_version=row["catalog_version"],
        catalog_sha256=row["catalog_sha256"],
        norm_version=row["norm_version"],
        header=json.loads(row["header"]),
        items=tuple(
            SpecificationItem(
                line_no=item["line_no"],
                product_id=item["product_id"],
                article=item["article"],
                name=item["name"],
                quantity=item["quantity"],
                quantity_source=QuantitySource(item["quantity_source"]),
                quantity_note=item["quantity_note"],
                unit=item["unit"],
                unit_price=item["unit_price"],
                total_price=item["total_price"],
                availability=Availability(item["availability"]),
                norm_document=item["norm_document"],
                norm_item=item["norm_item"],
                norm_item_title=item["norm_item_title"],
                norm_status=NormCheckStatus(item["norm_status"]),
                selection_reason=item["selection_reason"],
                url=item["url"],
            )
            for item in items
        ),
        totals=SpecificationTotals(**json.loads(row["totals"])),
        warnings=tuple(Notice(**notice) for notice in json.loads(row["warnings"])),
        parent_id=row["parent_id"],
    )
