"""Количество позиции и его источник.

Порядок источников: менеджер → пользователь → норматив → расчёт → по умолчанию.
Нормативное количество берётся только из текста приказа (сейчас оно есть в 1057,
в 838 количеств нет). Рассчитанное или количество по умолчанию нормативным не
называется: у каждого решения свой источник и пояснение (ТЗ §6).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from catalog.models import Product
from norms import documents as docs
from norms.mapping import MappingStatus, NormMapping
from norms.repository import NormRepository, QuantityRule, norm_quantity, quantity_rule
from norms.selector import NormResolution
from procurement.models import ProcurementTask, QuantitySource

DEFAULT_QUANTITY = 1


@dataclass(frozen=True)
class QuantityDecision:
    quantity: int
    source: QuantitySource
    note: str
    # Количество по перечню, даже если выбрано другое: видно, насколько отличается.
    norm_quantity: int | None = None


class QuantityResolver:
    def __init__(self, repository: NormRepository) -> None:
        self.repository = repository

    def resolve(
        self,
        task: ProcurementTask,
        product: Product,
        mappings: Sequence[NormMapping],
        resolution: NormResolution | None,
    ) -> QuantityDecision:
        norm_value, norm_note, rule, rule_code = self._from_norm(mappings, resolution)
        chosen = task.quantities.get(product.id)
        if chosen is not None and chosen.source is QuantitySource.MANAGER:
            return QuantityDecision(chosen.quantity, QuantitySource.MANAGER, "задано менеджером", norm_value)
        if chosen is not None:
            return QuantityDecision(chosen.quantity, QuantitySource.USER, "задано пользователем", norm_value)
        if task.quantity:
            return QuantityDecision(task.quantity, QuantitySource.USER, "количество из задачи", norm_value)
        if norm_value:
            return QuantityDecision(norm_value, QuantitySource.NORM, norm_note, norm_value)

        participants = task.preferences.get("participants")
        groups = task.preferences.get("groups")
        if rule is QuantityRule.PER_CHILD and participants:
            note = f"по числу детей ({participants}) — правило пункта {rule_code}"
            return QuantityDecision(int(participants), QuantitySource.CALCULATED, note)
        if rule is QuantityRule.PER_GROUP and groups:
            note = f"по одной на группу ({groups}) — правило пункта {rule_code}"
            return QuantityDecision(int(groups), QuantitySource.CALCULATED, note)
        return QuantityDecision(
            DEFAULT_QUANTITY,
            QuantitySource.DEFAULT,
            "количество не определено данными — по умолчанию 1, уточните",
        )

    def _from_norm(
        self, mappings: Sequence[NormMapping], resolution: NormResolution | None
    ) -> tuple[int | None, str, QuantityRule | None, str | None]:
        document = resolution.document if resolution else None
        point = resolution.point if resolution else None
        usable = [
            mapping
            for mapping in mappings
            if mapping.item_code
            and mapping.status is MappingStatus.APPROVED
            and (document is None or mapping.doc_id == document)
        ]
        # Сначала пункт, о котором спросили, потом остальные пункты товара.
        usable.sort(key=lambda mapping: mapping.item_code != point)
        rule: QuantityRule | None = None
        rule_code: str | None = None
        for mapping in usable:
            item = self.repository.item(mapping.doc_id, mapping.item_code or "")
            value = norm_quantity(item)
            if value:
                name = docs.get(mapping.doc_id).short_name if mapping.doc_id in docs.DOCUMENTS else mapping.doc_id
                unit = f" {item.unit}" if item and item.unit else ""
                return value, f"пункт {mapping.item_code}, {name}: {value}{unit}", None, None
            if rule is None and (found := quantity_rule(item)):
                rule, rule_code = found, mapping.item_code
        return None, "", rule, rule_code
