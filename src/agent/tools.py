"""Инструменты агента.

Агент не выдумывает товары, цены и нормативные основания — он получает их только
через эти вызовы. Товары приходят из Procurement Core (`core/selection.py`): подбор,
цена, наличие, количество и причина — из `SelectionResult`, и назвать, показать
карточкой или положить в корзину можно только то, что ядро вернуло в этом разговоре.

Действия, меняющие состояние (корзина, оформление), тоже идут через инструменты,
но оформление заказа агент только начинает: подтверждает его пользователь кнопкой.
"""

from __future__ import annotations

import json
from typing import Any

from core import selection
from core.ui import price_text, stock_text
from norms import documents as norm_docs
from norms import extract as norm_extract
from norms import reference
from norms.extract import document_ids_in_text
from procurement.models import SelectionResult, SelectionStatus

# Инструменты, чьи вызовы попадают в журнал хода: по ним разбирают сбои
# «нашёл, но не то».
_NORM_TOOLS = frozenset({"find_by_norm_code", "find_norm_item", "explain_norm"})
# Инструменты подбора: вызов любого из них — это подбор, а не обещание подбора.
SELECTION_TOOLS = frozenset({"search_products", "find_by_norm_code"})
# Сколько пунктов раздела отдаёт find_norm_item: в «1.5 Спортивный зал» приказа 1057 их 99, и
# консультант составляет по ним полную предварительную комплектацию — обрезать раздел нельзя.
MAX_POSITIONS = 100


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Подбор товаров ядром закупки под задачу разговора: учреждение, помещение и "
                "возраст подставляются сами. Возвращает до трёх позиций с ценой, наличием, "
                "количеством, причиной подбора и нормативным основанием. Называть можно "
                "только эти позиции. Повторный вызов с тем же запросом — следующие позиции. "
                "Используй для любого предметного запроса."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Какой товар, своими словами"},
                    "in_stock_only": {"type": "boolean"},
                    "price_max": {"type": "integer", "description": "Верхняя граница цены, ₽"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_by_norm_code",
            "description": (
                "Подбор ядром закупки по номеру пункта нормативного перечня: «2.1.14», «2.20.63». "
                "Можно указать подраздел целиком — «2.4», тогда вернутся все его позиции. "
                "Документ указывай всегда, когда он известен: один и тот же номер есть "
                "в разных приказах и означает в них разное."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "document": {
                        "type": "string",
                        "description": (
                            "order_838, order_1057 — либо просто «838», «1057». "
                            "Не указан — берётся из разговора."
                        ),
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_norm_item",
            "description": (
                "Пункт перечня по смыслу или по номеру — из текста самого приказа. "
                "Вызывай, прежде чем называть номер пункта: «какой пункт про спортивное "
                "оборудование в приказе 1057» вернёт 1.5.1, а «2.1.14 в 1057» честно "
                "ответит, что такого пункта в этом приказе нет. Формулировку пункта "
                "бери отсюда, а не по памяти. `path` — разделы, в которых стоит пункт "
                "(помещение, возраст): по нему видно, к чему пункт относится. По номеру "
                "раздела («1.5») возвращает его состав — `positions` с количеством по перечню."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Слова или номер пункта: «спортивный инвентарь», «1.5.1»",
                    },
                    "document": {
                        "type": "string",
                        "description": "order_838, order_1057 — либо просто «838», «1057»",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_norm",
            "description": (
                "Справка по нормативному документу: что это, кого касается, как устроен "
                "перечень и сколько позиций каталога к нему привязано. Вызывай, когда "
                "спрашивают про сам документ — «что значит приказ 838», «на основании "
                "чего обязаны укомплектовать». Отвечай текстом справки, не пересказывая "
                "документ по памяти."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document": {
                        "type": "string",
                        "description": (
                            "order_838, order_1057, fgos_do, fop_do, func_kits — "
                            "либо просто «838», «1057», «ФГОС ДО»"
                        ),
                    }
                },
                "required": ["document"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": (
                "Полная карточка товара по коду 1С, включая состав комплекта. Только для "
                "позиций, которые вернул подбор, или для товаров из корзины."
            ),
            "parameters": {
                "type": "object",
                "properties": {"sku_1c": {"type": "string"}},
                "required": ["sku_1c"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": (
                "Добавить в корзину позицию из подбора. Только после согласия пользователя."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku_1c": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1},
                },
                "required": ["sku_1c"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cart",
            "description": "Что сейчас в корзине пользователя и на какую сумму.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_to_manager",
            "description": (
                "Позвать живого менеджера: вопрос вне каталога, нестандартные условия, "
                "нужен расчёт или документы."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


class ToolBox:
    """Исполнение инструментов поверх каталога и корзины пользователя."""

    def __init__(self, engine, session) -> None:  # noqa: ANN001 — циклический импорт
        self.engine = engine
        self.session = session
        self.shown_skus: list[str] = []
        self.handoff_reason: str | None = None
        # Все суммы, которые инструменты вернули за этот ход. По ним проверяется
        # ответ модели: цены, которой здесь нет, в ответе быть не должно.
        self.prices: set[int] = set()
        # То же для нормативных оснований: пары «приказ, пункт». Цены проверялись
        # с самого начала, а основания доходили до человека непроверенными — так
        # и прошло «Спортивный комплекс соответствует пункту 2.20.63 приказа
        # 1057», где 2.20.63 это фрезерный станок из приказа 838.
        self.norm_refs: set[tuple[str, str]] = set()
        # Нормативные вызовы этого хода — для журнала. «Нашёл, но не то» иначе не
        # отличить от нормального ответа: в записи видно только текст, а по нему
        # не понять, какой приказ спрашивали и что вернул поиск.
        self.norm_lookups: list[dict[str, Any]] = []
        # Был ли в этом ходе подбор ядром. Ответ «сейчас подберу» без него — ложное обещание.
        self.selected = False
        # Основание каждой подобранной позиции — как его вернуло ядро. Из него карточка.
        self.citations: dict[str, str] = {}

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return json.dumps({"error": f"неизвестный инструмент {name}"}, ensure_ascii=False)
        try:
            result = handler(**arguments)
        except TypeError as exc:
            return json.dumps({"error": f"неверные аргументы: {exc}"}, ensure_ascii=False)
        self._remember_lookup(name, arguments, result)
        return json.dumps(result, ensure_ascii=False)

    def _remember_lookup(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        """Нормативный вызов — в журнал: что спросили и что вернулось."""
        if name not in _NORM_TOOLS:
            return
        self.norm_lookups.append(
            {
                "tool": name,
                "code": arguments.get("code") or arguments.get("query"),
                "document": arguments.get("document"),
                "found": result.get("found") if isinstance(result, dict) else None,
            }
        )

    # --- Реализация инструментов ------------------------------------------

    def _search_products(
        self,
        query: str,
        in_stock_only: bool = False,
        price_max: int | None = None,
        limit: int | None = None,  # прежний параметр: ядро само отдаёт до трёх позиций
    ) -> dict[str, Any]:
        # Учреждение, помещение и возраст ядро берёт из задачи разговора: без них подбор
        # для детского сада поднимал школьные позиции и обосновывал их школьным приказом.
        result = selection.select(
            self.engine,
            self.session,
            query=str(query or ""),
            # Поиск по словам — не по пункту перечня: прежний пункт выдачу не сужает.
            norm_item="",
            available_only=bool(in_stock_only),
            budget=_whole(price_max),
        )
        return self._selection(result)

    def _find_by_norm_code(self, code: str, document: str | None = None) -> dict[str, Any]:
        code = norm_extract.normalize_code(code)
        doc_id = self._document_for(document)
        # Пункта нет в запрошенном приказе — так и говорим. Раньше поиск шёл по
        # голому номеру, и «2.1.14 по приказу 1057» возвращал школьную речевую
        # игру: номер совпал, приказ — нет. Человек видел садовскую рекомендацию
        # со школьным основанием и справедливо считал, что бот всё перепутал.
        elsewhere = self._where_code_lives(code)
        if doc_id and elsewhere and doc_id not in elsewhere:
            return self._code_not_in_document(code, doc_id, elsewhere)

        result = selection.select(
            self.engine, self.session, query="", norm_item=code, norm_document=doc_id
        )
        answer = self._selection(result)
        norm = result.norm if result is not None else None
        # Как пункт называется в самом перечне — и в каком именно перечне. Голая
        # формулировка без имени документа однажды уже привела к тому, что текст из
        # приказа 838 был выдан пользователю за пункт 1057.
        if norm is not None and norm.point and norm.document in norm_docs.DOCUMENTS:
            answer["norm_item_document"] = norm_docs.get(norm.document).short_name
            if norm.point_title:
                answer["norm_item_title"] = norm.point_title
                self.norm_refs.add((norm.document, norm.point))
        return answer

    def _selection(self, result: SelectionResult | None) -> dict[str, Any]:
        """Результат ядра для модели. Других товаров у модели нет."""
        if result is None:
            return {"found": 0, "error": "подбор недоступен — предложи связаться с менеджером"}
        self.selected = True
        if result.status is SelectionStatus.NEEDS_DETAILS:
            return {
                "found": 0,
                "needs_details": [selection.QUESTIONS.get(name, name) for name in result.questions],
                "note": "Для подбора не хватает данных — задай клиенту один вопрос, товары не называй.",
            }
        if not result.items:
            return {
                "found": 0,
                "note": "Ядро подбора ничего не нашло по этой задаче. Скажи это честно, товары не называй.",
            }
        answer: dict[str, Any] = {
            "found": len(result.items),
            "more_available": result.has_more,
            "products": [self._item(item) for item in result.items],
        }
        spoken = selection.notes(result)
        if spoken:
            answer["notes"] = spoken
        return answer

    def _item(self, item) -> dict[str, Any]:  # noqa: ANN001 — procurement.models.SelectionItem
        product = self.engine.index.get(item.product_id)
        self.shown_skus.append(item.product_id)
        self._remember_price(item.price)
        self._remember_price(item.total_price)
        for mapping in item.norm_mappings:
            if mapping.item_code:
                self.norm_refs.add((mapping.doc_id, mapping.item_code))
        cited = selection.citation(item)
        if cited:
            self.citations[item.product_id] = cited
        return {
            "sku_1c": item.product_id,
            "name": item.name,
            "price": price_text(item.price),
            "stock": stock_text(product) if product is not None else str(item.availability),
            "quantity": item.quantity,
            "quantity_note": item.quantity_note,
            "reason": item.reason,
            "norm": cited,
            "url": item.url,
        }

    def _offered(self) -> set[str]:
        """Товары, о которых модели позволено говорить: подобранные ядром в разговоре и из корзины."""
        cart = self.engine.storage.load_cart(self.session.user_id)
        return {*self.shown_skus, *self.session.profile.offered, *(item.sku_1c for item in cart.items)}

    def _find_norm_item(self, query: str, document: str | None = None) -> dict[str, Any]:
        index = self.engine.norm_texts
        if not index.loaded:
            return {"error": "тексты приказов не загружены, формулировку пункта уточнит менеджер"}

        doc_id = self._document_for(document)
        code = norm_extract.codes_in_query(query)
        if code:
            return self._norm_item_by_code(code[0], doc_id)

        found = index.search(query, doc_id, limit=5)
        if not found:
            return {"found": 0, "note": "В текстах приказов ничего похожего не нашлось."}
        return {"found": len(found), "items": [self._item_brief(item) for item in found]}

    def _norm_item_by_code(self, code: str, doc_id: str | None) -> dict[str, Any]:
        index = self.engine.norm_texts
        homes = index.documents_with(code)
        if doc_id and doc_id not in homes:
            answer: dict[str, Any] = {
                "found": 0,
                "note": (
                    f"Пункта {code} нет: {norm_docs.get(doc_id).short_name} такого номера не содержит."
                ),
            }
            if homes:
                other = index.get(homes[0], code)
                answer["also_in"] = self._item_brief(other) if other else None
            return answer
        for home in [doc_id] if doc_id else homes:
            item = index.get(home, code) if home else None
            if item is not None:
                return {"found": 1, "items": [self._item_brief(item, with_positions=True)]}
        return {"found": 0, "note": f"Пункта {code} нет ни в одном из разобранных приказов."}

    def _item_brief(self, item, with_positions: bool = False) -> dict[str, Any]:  # noqa: ANN001 — norms.items.NormItem
        """Пункт с разделами, в которых он стоит, а по номеру — и с составом раздела.

        14.09 на «оснастить спортзал по 1057» консультант перечислил «1.14.2.7.2 Спортивный
        инвентарь»: без пути не видно, что это групповые помещения для детей до года, а не
        спортзал. Состав раздела нужен на «приведи списком оборудование по приказу».
        """
        self.norm_refs.add((item.doc_id, item.code))
        brief = {
            "code": item.code,
            "title": item.title,
            "document": norm_docs.get(item.doc_id).short_name,
            "document_id": item.doc_id,
        }
        if item.section:
            brief["section"] = item.section
        index = self.engine.norm_texts
        path = index.parents(item.doc_id, item.code)
        if path:
            brief["path"] = " → ".join(f"{parent.code} {parent.title}" for parent in path)
        if with_positions:
            positions = index.children(item.doc_id, item.code)
            if positions:
                brief["positions_total"] = len(positions)
                brief["positions"] = [self._position(child) for child in positions[:MAX_POSITIONS]]
        return brief

    def _position(self, item) -> dict[str, Any]:  # noqa: ANN001 — norms.items.NormItem
        self.norm_refs.add((item.doc_id, item.code))
        position = {"code": item.code, "title": item.title}
        if item.quantity:
            position["norm_quantity"] = f"{item.quantity} {item.unit or ''}".strip()
        return position

    def _where_code_lives(self, code: str) -> list[str]:
        """Приказы, в которых такой пункт есть, — по текстам и по каталогу."""
        from_texts = self.engine.norm_texts.documents_with(code)
        return from_texts or self.engine.index.documents_with_code(code)

    def _code_not_in_document(
        self, code: str, doc_id: str, elsewhere: list[str]
    ) -> dict[str, Any]:
        names = ", ".join(norm_docs.get(other).short_name for other in elsewhere)
        answer: dict[str, Any] = {
            "found": 0,
            "note": (
                f"Пункта {code} нет: {norm_docs.get(doc_id).short_name} такого номера не содержит. "
                f"Такой номер есть в другом документе — {names}."
            ),
        }
        item = self.engine.norm_texts.get(elsewhere[0], code)
        if item is not None:
            answer["also_in"] = self._item_brief(item)
        return answer

    def _document_for(self, document: str | None) -> str | None:
        """Приказ, по которому подбираем: названный моделью или взятый из разговора."""
        if document:
            return _document_id(document)
        named = self.session.profile.norm_doc_ids
        return named[0] if len(named) == 1 else None

    def _explain_norm(self, document: str) -> dict[str, Any]:
        doc_id = _document_id(document)
        if doc_id is None:
            return {
                "error": f"документ «{document}» не распознан",
                "known": reference.known_documents(),
            }
        return {
            "document": doc_id,
            "reference": reference.explain(
                doc_id,
                reference.coverage(
                    self.engine.index, doc_id, self.engine.norm_texts.count(doc_id)
                ),
            ),
        }

    def _get_product(self, sku_1c: str) -> dict[str, Any]:
        if sku_1c not in self._offered():
            return {"error": f"товара {sku_1c} не было в подборе — сначала подбери через search_products"}
        product = self.engine.index.get(sku_1c)
        if product is None:
            return {"error": f"товара с кодом {sku_1c} нет в каталоге"}
        self.shown_skus.append(sku_1c)
        self._remember_price(product.price)
        self._remember_norms(product)
        norm = product.norm_for(self.session.profile.audience, self.session.profile.room or "")
        return {
            "sku_1c": product.sku_1c,
            "name": product.name,
            "price": price_text(product.price),
            "stock": stock_text(product),
            "url": product.url,
            "category": product.category_paths[0] if product.category_paths else [],
            "kit_contents": product.kit_contents[:20],
            "description": product.description[:1200],
            "norm": norm.citation if norm else None,
        }

    def _add_to_cart(self, sku_1c: str, quantity: int = 1) -> dict[str, Any]:
        if sku_1c not in self._offered():
            return {"error": f"товара {sku_1c} не было в подборе — в корзину кладётся только подобранное"}
        product = self.engine.index.get(sku_1c)
        if product is None:
            return {"error": f"товара с кодом {sku_1c} нет в каталоге"}
        self.engine._add(self.session, sku_1c, max(1, quantity))
        cart = self.engine.storage.load_cart(self.session.user_id)
        return {"added": product.name, "cart_count": cart.count, "cart_total": cart.total}

    def _get_cart(self) -> dict[str, Any]:
        cart = self.engine.storage.load_cart(self.session.user_id)
        self._remember_price(cart.total)
        for item in cart.items:
            self._remember_price(item.price)
            self._remember_price(item.total)
        return {
            "items": [
                {"sku_1c": i.sku_1c, "name": i.name, "quantity": i.quantity, "sum": i.total}
                for i in cart.items
            ],
            "total": cart.total,
        }

    def _handoff_to_manager(self, reason: str) -> dict[str, Any]:
        self.handoff_reason = reason
        return {"ok": True, "contact": self.engine.settings.manager_contact}

    def _remember_norms(self, product) -> None:  # noqa: ANN001 — catalog.models.Product
        """Основания показанного товара — то, на что модель вправе сослаться."""
        for ref in product.norms:
            if ref.item_code:
                self.norm_refs.add((ref.doc_id, ref.item_code))

    def _remember_price(self, price: int | None) -> None:
        if price:
            self.prices.add(int(price))
        # Цена в ответе почти всегда стоит с разделителем разрядов, а бывает —
        # округлённой до тысяч. Оба написания читаются как одна и та же сумма,
        # и придирка к формату превратила бы проверку в источник ложных тревог.




def _whole(value: Any) -> int | None:
    """Граница цены от модели: число, строка с числом или ничего."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _document_id(name: str) -> str | None:
    """Идентификатор документа по тому, как его назвала модель.

    Модель зовёт документ и кодом, и номером, и словами — принимаем всё,
    иначе инструмент отвечает ошибкой на осмысленный вызов.
    """
    raw = (name or "").strip().lower()
    if raw in norm_docs.DOCUMENTS:
        return raw
    found = document_ids_in_text(raw) or document_ids_in_text(f"приказ {raw}")
    return found[0] if found else None
