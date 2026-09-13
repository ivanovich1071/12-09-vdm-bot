"""Требование к подбору из задачи закупки.

Пожелания пользователя и требования норматива собираются раздельно: первые — из
полей задачи, вторые — только из нормативной базы (`norms/selector.py`). Модель
здесь не участвует и нормативного количества определить не может.
"""

from __future__ import annotations

from collections.abc import Iterable

from catalog.models import Product
from catalog.placement import institution_code, room_in
from core.errors import Notice
from norms.selector import NORM_REASON_LABELS, NormQuery, NormSelector
from procurement.models import ProcurementRequirement, ProcurementTask, UserRequest


class RequirementBuilder:
    def __init__(self, selector: NormSelector) -> None:
        self.selector = selector

    def build(self, task: ProcurementTask, products: Iterable[Product]) -> ProcurementRequirement:
        preferences = task.preferences
        user = UserRequest(
            text=str(preferences.get("query") or "").strip(),
            institution_type=task.institution_type,
            institution_name=task.institution_name,
            room=task.room,
            zone=task.zone,
            grade=task.grade,
            age_group=task.age_group,
            category=preferences.get("category"),
            goal=task.goal,
            budget=task.budget,
            deadline=task.deadline,
            quantity=task.quantity,
            available_only=bool(preferences.get("available_only")),
            participants=preferences.get("participants"),
            groups=preferences.get("groups"),
        )
        norm = self.selector.resolve(
            NormQuery(
                norm_document=task.norm_document,
                norm_item=task.norm_item,
                institution_type=task.institution_type,
                required=task.norm_required,
            ),
            products,
        )

        warnings: list[Notice] = []
        audience = institution_code(task.institution_type)
        if task.institution_type and audience is None:
            warnings.append(
                Notice(
                    "INSTITUTION_NOT_FILTERED",
                    f"Для учреждения «{task.institution_type}» в каталоге нет своего раздела — "
                    "выдача по типу учреждения не сужена.",
                )
            )
        catalog_room = room_in(task.room)
        if task.room and catalog_room is None:
            warnings.append(
                Notice(
                    "ROOM_NOT_IN_CATALOG",
                    f"Помещение «{task.room}» не выделено разделом каталога — ищем по словам.",
                )
            )
        if norm.requires_review:
            warnings.append(
                Notice(
                    "NORM_REVIEW_REQUIRED",
                    "; ".join(NORM_REASON_LABELS[reason] for reason in norm.reasons),
                    {"reasons": [str(reason) for reason in norm.reasons]},
                )
            )

        return ProcurementRequirement(
            task_id=task.id,
            user=user,
            norm=norm,
            audience=audience,
            catalog_room=catalog_room,
            exclude=frozenset(
                [*task.shown_products, *task.rejected_products, *task.selected_products]
            ),
            warnings=tuple(warnings),
        )
