"""Подбор в диалоге — только через Procurement Core.

Продавец, ответ без модели и подбор вместо ложного обещания берут товары из одного
места: `ProcurementService.select`. Разговор ведёт одну задачу закупки — её номер
хранится в профиле, — и задача уточняется тем, что разговор уже знает: учреждение,
помещение, возраст, документ. Слова запроса и пункт перечня задаёт вызывающий.

Модель в подборе не участвует: она получает `SelectionResult` и может только рассказать
о нём. Товара, которого ядро не вернуло, в карточках быть не может.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core import intent
from core.errors import DomainError
from procurement import discovery
from procurement.models import SelectionItem, SelectionResult
from procurement.service import MAX_TEXT

if TYPE_CHECKING:
    from core.dialog import DialogEngine, Session

log = logging.getLogger(__name__)

# Чего не хватает ядру для подбора — словами для человека.
QUESTIONS = {
    "institution_type": "для детского сада или для школы",
    "room": "для какого помещения или зоны",
}
# Предупреждения ядра, которые стоит сказать человеку. Остальные — служебный отчёт о фильтрах.
# NORM_REVIEW_REQUIRED не для клиента: его расшифровка («Документ назван пользователем…»)
# — служебный отчёт, который 19-20.09 показывался людям как часть ответа.
SPOKEN_WARNINGS = frozenset({"BUDGET_EXCEEDED", "SUBJECT_NOT_FOUND"})
MAX_QUERY = 200


def select(
    engine: DialogEngine,
    session: Session,
    *,
    text: str | None = None,
    query: str | None = None,
    norm_item: str | None = None,
    norm_document: str | None = None,
    available_only: bool | None = None,
    budget: int | None = None,
    limit: int | None = None,
) -> SelectionResult | None:
    """Следующие позиции под задачу разговора. `None` — Procurement Core не подключён.

    `text` — реплика человека: задачу из неё разбирает само ядро (`procurement.discovery`),
    оно знает помещения каталога лучше профиля разговора. Остальные параметры: `None` —
    оставить как было, пустая строка — снять.
    """
    service = engine.procurement_service()
    if service is None:
        return None
    profile = session.profile
    documents = profile.norm_doc_ids
    known = {
        "institution_type": profile.institution,
        "room": profile.room,
        "age_group": profile.age,
        "deadline": profile.deadline,
        "norm_document": norm_document or (documents[0] if len(documents) == 1 else None),
    }
    # Неизвестное профилю не затирает то, что ядро разобрало из реплики.
    fields: dict[str, Any] = {name: value for name, value in known.items() if value}
    if norm_item is not None:
        fields["norm_item"] = norm_item
    if query is not None:
        fields["query"] = query[:MAX_QUERY]
    if available_only is not None:
        fields["available_only"] = available_only or None
    if budget is not None:
        fields["budget"] = budget
    task = _task(engine, session, fields, text)
    # «Дорого», «не то» разговор уже отметил в профиле — ядро не должно предлагать это снова.
    fresh = [sku for sku in profile.rejected if sku not in task.rejected_products]
    if fresh:
        service.reject(task.id, session.user_id, fresh)
    return service.select(task.id, session.user_id, limit=limit)


def more(engine: DialogEngine, session: Session) -> SelectionResult | None:
    """Следующая страница того же подбора, без изменения задачи."""
    service = engine.procurement_service()
    task_id = session.profile.procurement_task_id
    if service is None or not task_id:
        return None
    try:
        return service.select(task_id, session.user_id)
    except DomainError as exc:
        log.info("Подбор разговора не продолжен: %s", exc.code)
        return None


def about_task(text: str) -> bool:
    """Говорит ли реплика о закупке. «Хорошо, что дальше?» — нет: её слова не запрос."""
    return intent.classify(text) in (intent.PRODUCT, intent.NORM_CODE, intent.TASK)


def query_of(text: str) -> str:
    """Слова товара из реплики — для честного ответа о пустом разделе (сц. 12)."""
    return discovery.query_from_text(text)


def citation(item: SelectionItem) -> str | None:
    """Основание позиции так, как его вернуло ядро: пункт перечня и документ.

    Привязка с номером пункта важнее привязки к документу целиком: без неё карточка
    писала «приказ № 1057» у товара, для которого ядро знает позицию 1.5.1.11.
    """
    mappings = item.norm_mappings
    if not mappings:
        return None
    # Первым — пункт, названный в причине подбора: 14.09 текст писал «пункт 1.14.2.7.2.3», а
    # карточка того же фитбола под ним — «позиция 1.5.1.35».
    named = next((m for m in mappings if m.item_code and f"пункт {m.item_code}," in item.reason), None)
    return (named or next((mapping for mapping in mappings if mapping.item_code), mappings[0])).citation


def question(missing: tuple[str, ...] | list[str]) -> str:
    asked = [QUESTIONS.get(name, name) for name in missing]
    return "Чтобы подобрать позиции из каталога, подскажите: " + " и ".join(asked) + "?"


def notes(result: SelectionResult) -> list[str]:
    return [notice.message for notice in result.warnings if notice.code in SPOKEN_WARNINGS]


def _task(engine: DialogEngine, session: Session, fields: dict[str, Any], text: str | None):  # noqa: ANN202
    service = engine.procurement_service()
    profile = session.profile
    text = text[:MAX_TEXT] if text else None
    if profile.procurement_task_id:
        try:
            return service.update_task(profile.procurement_task_id, session.user_id, text=text, fields=fields)
        except DomainError as exc:  # задача закрыта или удалена вместе с данными — начинаем новую
            log.info("Задача закупки разговора не продолжена: %s", exc.code)
    task = service.create_task(session.user_id, session.channel, text=text, fields=fields)
    profile.procurement_task_id = task.id
    return task


__all__ = ["QUESTIONS", "about_task", "citation", "more", "notes", "question", "select"]
