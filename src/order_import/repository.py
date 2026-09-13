"""Хранение загруженных заказов и их оценок (D4: SQL только здесь)."""

from __future__ import annotations

import json
from typing import Protocol

from core.database import CoreDatabase
from core.errors import Notice
from order_import.evaluation import EvaluationStatus, OrderEvaluation, OrderEvaluationItem
from order_import.models import (
    OrderContext,
    SourceFile,
    UploadedOrder,
    UploadedOrderItem,
    UploadStatus,
)

_ITEM_COLUMNS = (
    "order_id, line_no, source_line, source_table, raw, cells, article, name, name_canonical, "
    "manufacturer, characteristics, dimensions, quantity, unit, price, total, norm_document, "
    "norm_item, issues, manual_product_id, manual_by"
)


class OrderRepository(Protocol):
    def find_by_checksum(self, owner: str, checksum: str) -> UploadedOrder | None: ...

    def save_order(self, order: UploadedOrder) -> None: ...

    def get_order(self, order_id: str) -> UploadedOrder | None: ...

    def orders_of(self, owner: str, limit: int = 20) -> list[UploadedOrder]: ...

    def set_status(self, order_id: str, status: UploadStatus, updated_at: str) -> None: ...

    def set_manual_match(self, order_id: str, line_no: int, product_id: str, actor: str, updated_at: str) -> bool: ...

    def save_evaluation(self, evaluation: OrderEvaluation) -> None: ...

    def latest_evaluation(self, order_id: str) -> OrderEvaluation | None: ...

    def delete_owner(self, owner: str) -> list[str]: ...

    def is_file_used(self, storage_path: str) -> bool: ...


class SqliteOrderRepository:
    def __init__(self, db: CoreDatabase) -> None:
        self.db = db

    def find_by_checksum(self, owner: str, checksum: str) -> UploadedOrder | None:
        with self.db.read() as db:
            row = db.execute(
                "SELECT id FROM uploaded_orders WHERE owner = ? AND checksum = ?", (owner, checksum)
            ).fetchone()
        return self.get_order(row["id"]) if row else None

    def save_order(self, order: UploadedOrder) -> None:
        source = order.source_file
        with self.db.write() as db:
            db.execute(
                "INSERT INTO uploaded_orders(id, owner, channel, status, filename, media_type, size, "
                "checksum, storage_path, parser, catalog_version, norm_version, context, warnings, "
                "error, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order.id,
                    order.owner,
                    order.channel,
                    str(order.status),
                    source.filename,
                    source.media_type,
                    source.size,
                    source.checksum,
                    source.storage_path,
                    order.parser,
                    order.catalog_version,
                    order.norm_version,
                    json.dumps(order.context.to_dict(), ensure_ascii=False),
                    json.dumps([notice.to_dict() for notice in order.warnings], ensure_ascii=False),
                    order.error,
                    order.created_at,
                    order.updated_at,
                ),
            )
            db.executemany(
                f"INSERT INTO uploaded_order_items({_ITEM_COLUMNS}) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        order.id,
                        item.line_no,
                        item.source_line,
                        item.source_table,
                        json.dumps(item.raw, ensure_ascii=False),
                        json.dumps(list(item.cells), ensure_ascii=False),
                        item.article,
                        item.name,
                        item.name_canonical,
                        item.manufacturer,
                        item.characteristics,
                        item.dimensions,
                        item.quantity,
                        item.unit,
                        item.price,
                        item.total,
                        item.norm_document,
                        item.norm_item,
                        json.dumps(list(item.issues)),
                        item.manual_product_id,
                        item.manual_by,
                    )
                    for item in order.items
                ],
            )

    def get_order(self, order_id: str) -> UploadedOrder | None:
        with self.db.read() as db:
            row = db.execute("SELECT * FROM uploaded_orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                return None
            items = db.execute(
                f"SELECT {_ITEM_COLUMNS} FROM uploaded_order_items WHERE order_id = ? ORDER BY line_no",
                (order_id,),
            ).fetchall()
        return UploadedOrder(
            id=row["id"],
            owner=row["owner"],
            channel=row["channel"],
            status=UploadStatus(row["status"]),
            source_file=SourceFile(
                row["filename"], row["media_type"], row["size"], row["checksum"], row["storage_path"]
            ),
            parser=row["parser"],
            catalog_version=row["catalog_version"],
            norm_version=row["norm_version"],
            context=OrderContext(**json.loads(row["context"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            items=tuple(
                UploadedOrderItem(
                    line_no=item["line_no"],
                    source_line=item["source_line"],
                    source_table=item["source_table"],
                    raw=json.loads(item["raw"]),
                    cells=tuple(json.loads(item["cells"])),
                    article=item["article"],
                    name=item["name"],
                    name_canonical=item["name_canonical"],
                    manufacturer=item["manufacturer"],
                    characteristics=item["characteristics"],
                    dimensions=item["dimensions"],
                    quantity=item["quantity"],
                    unit=item["unit"],
                    price=item["price"],
                    total=item["total"],
                    norm_document=item["norm_document"],
                    norm_item=item["norm_item"],
                    issues=tuple(json.loads(item["issues"])),
                    manual_product_id=item["manual_product_id"],
                    manual_by=item["manual_by"],
                )
                for item in items
            ),
            warnings=tuple(Notice(**notice) for notice in json.loads(row["warnings"])),
            error=row["error"],
        )

    def orders_of(self, owner: str, limit: int = 20) -> list[UploadedOrder]:
        with self.db.read() as db:
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM uploaded_orders WHERE owner = ? ORDER BY created_at DESC LIMIT ?",
                    (owner, limit),
                )
            ]
        return [order for order_id in ids if (order := self.get_order(order_id))]

    def set_status(self, order_id: str, status: UploadStatus, updated_at: str) -> None:
        with self.db.write() as db:
            db.execute(
                "UPDATE uploaded_orders SET status = ?, updated_at = ? WHERE id = ?",
                (str(status), updated_at, order_id),
            )

    def set_manual_match(self, order_id: str, line_no: int, product_id: str, actor: str, updated_at: str) -> bool:
        with self.db.write() as db:
            cursor = db.execute(
                "UPDATE uploaded_order_items SET manual_product_id = ?, manual_by = ? "
                "WHERE order_id = ? AND line_no = ?",
                (product_id, actor, order_id, line_no),
            )
            db.execute("UPDATE uploaded_orders SET updated_at = ? WHERE id = ?", (updated_at, order_id))
            return bool(cursor.rowcount)

    def save_evaluation(self, evaluation: OrderEvaluation) -> None:
        with self.db.write() as db:
            db.execute(
                "INSERT INTO order_evaluations(id, order_id, owner, status, catalog_version, "
                "norm_version, summary, items, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evaluation.id,
                    evaluation.order_id,
                    evaluation.owner,
                    str(evaluation.status),
                    evaluation.catalog_version,
                    evaluation.norm_version,
                    json.dumps(evaluation.summary, ensure_ascii=False),
                    json.dumps([item.to_dict() for item in evaluation.items], ensure_ascii=False),
                    evaluation.created_at,
                ),
            )

    def latest_evaluation(self, order_id: str) -> OrderEvaluation | None:
        with self.db.read() as db:
            row = db.execute(
                "SELECT * FROM order_evaluations WHERE order_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return OrderEvaluation(
            id=row["id"],
            order_id=row["order_id"],
            owner=row["owner"],
            status=EvaluationStatus(row["status"]),
            catalog_version=row["catalog_version"],
            norm_version=row["norm_version"],
            created_at=row["created_at"],
            items=tuple(OrderEvaluationItem.from_dict(item) for item in json.loads(row["items"])),
            summary=json.loads(row["summary"]),
        )

    def delete_owner(self, owner: str) -> list[str]:
        """Заказы пользователя целиком: в файле клиента бывают его контакты. Возвращает пути файлов."""
        with self.db.write() as db:
            rows = db.execute(
                "SELECT id, storage_path FROM uploaded_orders WHERE owner = ?", (owner,)
            ).fetchall()
            ids = [(row["id"],) for row in rows]
            db.executemany("DELETE FROM order_evaluations WHERE order_id = ?", ids)
            db.executemany("DELETE FROM uploaded_order_items WHERE order_id = ?", ids)
            db.execute("DELETE FROM uploaded_orders WHERE owner = ?", (owner,))
        return [row["storage_path"] for row in rows]

    def is_file_used(self, storage_path: str) -> bool:
        with self.db.read() as db:
            row = db.execute(
                "SELECT 1 FROM uploaded_orders WHERE storage_path = ? LIMIT 1", (storage_path,)
            ).fetchone()
        return row is not None
