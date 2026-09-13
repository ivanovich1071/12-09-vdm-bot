"""Ответ диалога в нейтральном виде для любого канала.

Виджет рендерит примитивы строками с ценой текстом (`web/render.py`), Telegram —
HTML-карточками. Core API отдаёт числа и коды: как показать, решает клиент.
"""

from __future__ import annotations

from typing import Any

from core.ui import Keyboard, Message, OrderSummary, ProductCard, ProductList, Response


def responses(items: list[Response]) -> list[dict[str, Any]]:
    return [_one(item) for item in items]


def _one(response: Response) -> dict[str, Any]:
    actions = _keyboard(getattr(response, "keyboard", None))
    if isinstance(response, Message):
        return {"type": "text", "text": response.text, "actions": actions, "replace": response.replace}
    if isinstance(response, ProductCard):
        return {
            "type": "product",
            "product": _product(response),
            "quantity_in_cart": response.quantity,
            "citation": response.citation,
            "norms": list(response.norms),
            "actions": actions,
            "replace": response.replace,
        }
    if isinstance(response, ProductList):
        return {
            "type": "product_list",
            "title": response.title,
            "total_found": response.total_found,
            "items": [
                {"product": _product(card), "citation": card.citation, "actions": _keyboard(card.keyboard)}
                for card in response.cards
            ],
            "actions": actions,
        }
    if isinstance(response, OrderSummary):
        return {
            "type": "cart",
            "lines": [
                {
                    "product_id": line.sku_1c,
                    "name": line.name,
                    "quantity": line.quantity,
                    "price": line.price,
                    "total": line.price * line.quantity if line.price is not None else None,
                    "norm_citation": line.norm_citation,
                }
                for line in response.lines
            ],
            "total": response.total,
            "note": response.note,
            "actions": actions,
            "replace": response.replace,
        }
    return {"type": "unknown", "actions": actions}


def _product(card: ProductCard) -> dict[str, Any]:
    product = card.product
    return {
        "id": product.id,
        "article": product.article,
        "name": product.name,
        "price": product.price,
        "availability": str(product.availability),
        "quantity_available": product.quantity_available,
        "url": product.url,
        "image": f"/media/{product.id}" if card.image_path else card.image,
    }


def _keyboard(keyboard: Keyboard | None) -> list[list[dict[str, Any]]]:
    if keyboard is None:
        return []
    return [[{"title": b.title, "action": b.action, "url": b.url} for b in row] for row in keyboard.rows]
