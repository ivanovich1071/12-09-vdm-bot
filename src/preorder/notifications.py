"""Уведомление менеджера о предзаказе.

`NotificationChannel` — интерфейс: CRM, почта, Telegram, MAX подключаются
реализациями, ядро о них не знает (ТЗ §11). Сейчас включён файловый канал: отчёт
менеджеру в Excel и строка в журнале — так же, как заказы бота пишутся в
`data/orders`, пока CRM недоступна.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from documents.xlsx import BOLD, NUMBER, PLAIN, write_workbook
from preorder.models import Preorder

REPORT_COLUMNS: tuple[tuple[str, float], ...] = (
    ("№", 5),
    ("Исходная позиция", 40),
    ("Артикул в файле", 15),
    ("Код 1С", 15),
    ("Наименование в каталоге", 40),
    ("Кол-во", 8),
    ("Цена в файле", 12),
    ("Текущая цена", 12),
    ("Сумма", 13),
    ("Наличие", 14),
    ("Сопоставление", 16),
    ("Цена", 15),
    ("Норматив", 22),
    ("Расхождения", 40),
)


class NotificationChannel(Protocol):
    name: str

    def send(self, preorder: Preorder) -> None:
        """Отправить. Исключение — «не доставлено, повторить позже»."""
        ...


def manager_report(preorder: Preorder) -> bytes:
    customer = preorder.customer or {}
    rows: list[list[tuple[object, int]]] = [[(f"Предзаказ № {preorder.id}", BOLD)]]
    for label, value in (
        ("Статус", str(preorder.status)),
        ("Источник", f"{preorder.source}: {preorder.source_id}"),
        ("Организация", customer.get("organization", "")),
        ("Контактное лицо", customer.get("name", "")),
        ("Телефон", customer.get("phone", "")),
        ("E-mail", customer.get("email", "")),
        ("Регион", customer.get("region", "")),
        ("Комментарий", preorder.comment or customer.get("comment", "")),
        ("Требует проверки", "да" if preorder.review_required else "нет"),
        ("Версия каталога", preorder.catalog_version),
        ("Версия нормативной базы", preorder.norm_version),
    ):
        if value:
            rows.append([(label, BOLD), (value, PLAIN)])
    rows.append([])
    rows.append([(title, BOLD) for title, _ in REPORT_COLUMNS])
    for item in preorder.items:
        norm = " ".join(filter(None, (item.norm_status, item.norm_document, item.norm_item)))
        rows.append(
            [
                (item.line_no, PLAIN),
                (item.source_name or "", PLAIN),
                (item.source_article or "", PLAIN),
                (item.article or "", PLAIN),
                (item.name, PLAIN),
                (item.quantity, PLAIN),
                (item.document_price, NUMBER),
                (item.unit_price, NUMBER),
                (item.total_price, NUMBER),
                (item.availability, PLAIN),
                (item.match_status, PLAIN),
                (item.price_status, PLAIN),
                (norm, PLAIN),
                (", ".join(item.flags), PLAIN),
            ]
        )
    rows.append([])
    rows.append([("Итого по текущим ценам", BOLD), (preorder.totals.amount, NUMBER)])
    rows.append([("Предварительный заказ: наличие, срок и окончательную цену подтверждает менеджер.", PLAIN)])
    return write_workbook("Предзаказ", rows, [width for _, width in REPORT_COLUMNS])


@dataclass
class FileNotificationChannel:
    directory: Path
    name: str = "file"

    def send(self, preorder: Preorder) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{preorder.id}.xlsx").write_bytes(manager_report(preorder))
        line = {
            "id": preorder.id,
            "status": str(preorder.status),
            "positions": preorder.totals.positions,
            "amount": preorder.totals.amount,
            "review_required": preorder.review_required,
            "catalog_version": preorder.catalog_version,
            "updated_at": preorder.updated_at,
        }
        with (self.directory / "preorders.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
