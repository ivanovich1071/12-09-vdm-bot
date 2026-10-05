"""Procurement Core: запрос → задача → норматив → требования → подбор → количество → спецификация.

Сервис не знает о каналах — ни о Telegram, ни о виджете, ни о MAX. Вызывающий
передаёт владельца (идентификатор пользователя канала) и получает доменные объекты.

Каждая операция закрепляет одну версию каталога (`CatalogRuntime.turn`): подбор,
цены и спецификация внутри вызова — из одного снимка, а версия записывается в
результат.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from catalog.placement import institution_code
from catalog.runtime import CatalogRuntime, CatalogRuntimeState
from core.errors import Conflict, Forbidden, InvalidRequest, NotFound
from documents.exporters import ExportedDocument, export_specification
from norms.mapping import NormMappingService
from norms.repository import NormRepository
from norms.selector import NormSelector
from procurement import discovery
from procurement.models import (
    EXTERNAL_QUANTITY_SOURCES,
    OBJECTIONS,
    PREFERENCE_FIELDS,
    TASK_FIELDS,
    ProcurementRequirement,
    ProcurementTask,
    QuantityChoice,
    QuantitySource,
    SelectionResult,
    SelectionStatus,
    Specification,
    SpecificationFreshness,
    SpecificationStatus,
    Stage,
)
from procurement.quantity import QuantityResolver
from procurement.repository import ProcurementRepository
from procurement.requirements import RequirementBuilder
from procurement.selector import CandidateRanker, ProcurementSelector
from procurement.specification import SpecificationBuilder, SpecificationLine, compare

MAX_TEXT = 2000
MAX_FIELD = 200


class ProcurementService:
    def __init__(
        self,
        repository: ProcurementRepository,
        runtime: CatalogRuntime,
        norms: NormRepository,
        *,
        ranker: CandidateRanker | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self.norms = norms
        self.mapping = NormMappingService(norms)
        self.norm_selector = NormSelector(norms, self.mapping)
        self.quantities = QuantityResolver(norms)
        self.requirements = RequirementBuilder(self.norm_selector)
        self.selector = ProcurementSelector(self.mapping, self.quantities, ranker)
        self.builder = SpecificationBuilder(self.mapping, self.quantities)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._vocab: frozenset[str] | None = None

    @property
    def vocab(self) -> frozenset[str]:
        """Основы слов каталога — названия товаров и разделы.

        Второй фильтр запроса (К2.1): слово остаётся в поиске, только если оно вообще
        встречается в каталоге. «Менеджера», «времени», «примерно» отпадают сами, а
        новое слово-товар не теряется. Строится один раз на версию каталога.
        """
        if self._vocab is None:
            from catalog import text as catalog_text

            state = self.runtime.state
            words: set[str] = set()
            for product in state.index.products:
                words.update(catalog_text.stems(product.name))
                for placement in product.placements:
                    for section in placement.sections:
                        words.update(catalog_text.stems(section))
            self._vocab = frozenset(words)
        return self._vocab

    def names_catalog_item(self, text: str) -> bool:
        """Реплика называет предмет, который вообще есть в каталоге, — по основам слов.

        Классификатор «тактильные дорожки 3 шт. срок 4 недели» товаром не считает
        (нет ни просьбы, ни слов задачи), а словарь каталога в «дорожках» предмет
        видит. Запасному пути этого достаточно, чтобы искать дальше, а не отвечать
        заглушкой (прогон 05.10, СЦ2/СЦ10).
        """
        return bool(discovery.subject_words(text or "", self.vocab))

    # --- Задача ---------------------------------------------------------------

    def create_task(
        self,
        owner: str,
        channel: str,
        *,
        text: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> ProcurementTask:
        now = self._now()
        task = ProcurementTask(id=uuid.uuid4().hex, owner=owner, channel=channel, created_at=now, updated_at=now)
        self._apply(task, text, fields)
        self.repository.save_task(task)
        return task

    def update_task(
        self,
        task_id: str,
        owner: str,
        *,
        text: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> ProcurementTask:
        task = self._open_task(task_id, owner)
        if text and discovery.is_rejection(text) and task.shown_products:
            # «Дорого», «не то» относится к последней выдаче.
            last = [item["product_id"] for item in (task.offer or {}).get("items", [])]
            self._reject(task, last, discovery.objection_of(text))
        self._apply(task, text, fields)
        self._save(task)
        return task

    def get_task(self, task_id: str, owner: str) -> ProcurementTask:
        task = self.repository.get_task(task_id)
        if task is None or task.owner != owner:
            raise NotFound("Задача закупки не найдена.", code="TASK_NOT_FOUND", details={"task_id": task_id})
        return task

    def abandon(self, task_id: str, owner: str) -> ProcurementTask:
        task = self._open_task(task_id, owner)
        task.move_to(Stage.ABANDONED)
        self._save(task)
        return task

    def enter_order(self, task_id: str, owner: str) -> ProcurementTask:
        """Спецификация ушла в предзаказ: задача на этапе ORDER."""
        task = self.get_task(task_id, owner)
        if task.is_closed or task.stage is Stage.ORDER:
            return task
        self._advance(task, Stage.ORDER)
        self._save(task)
        return task

    def requirement(self, task_id: str, owner: str) -> ProcurementRequirement:
        task = self.get_task(task_id, owner)
        with self.runtime.turn() as state:
            return self.requirements.build(task, state.index.products)

    # --- Подбор ---------------------------------------------------------------

    def select(
        self, task_id: str, owner: str, *, restart: bool = False, limit: int | None = None
    ) -> SelectionResult:
        """Следующие позиции под задачу: три, а по списку «N позиций» — `limit`. Показанное не повторяется.

        Сменилась задача (помещение, документ, запрос) — прежняя выдача больше не
        считается показанной: это уже другой подбор.
        """
        task = self._open_task(task_id, owner)
        with self.runtime.turn() as state:
            requirement = self.requirements.build(task, state.index.products)
            previous = (task.offer or {}).get("signature")
            if restart or (previous is not None and previous != requirement.signature):
                task.shown_products = []
                requirement = self.requirements.build(task, state.index.products)
            if limit:
                result = self.selector.select(task, requirement, state, limit=limit)
            else:
                result = self.selector.select(task, requirement, state)

        if result.status is not SelectionStatus.NEEDS_DETAILS:
            for item in result.items:
                if item.product_id not in task.shown_products:
                    task.shown_products.append(item.product_id)
            reasons = dict((task.offer or {}).get("reasons", {}))
            reasons.update({item.product_id: item.reason for item in result.items})
            task.offer = {
                "signature": requirement.signature,
                "catalog_version": result.catalog_version,
                "items": [
                    {
                        "product_id": item.product_id,
                        "quantity": item.quantity,
                        "quantity_source": str(item.quantity_source),
                    }
                    for item in result.items
                ],
                "reasons": reasons,
                "at": self._now(),
            }
            self._advance(task, Stage.SELECTION, Stage.PRESENTATION)
        self._save(task)
        return result

    def choose(self, task_id: str, owner: str, product_ids: Sequence[str]) -> ProcurementTask:
        task = self._open_task(task_id, owner)
        with self.runtime.turn() as state:
            self._require_products(state, product_ids)
        for product_id in product_ids:
            if product_id not in task.selected_products:
                task.selected_products.append(product_id)
            if product_id in task.rejected_products:
                task.rejected_products.remove(product_id)
        self._advance(task, Stage.CART)
        self._save(task)
        return task

    def reject(
        self, task_id: str, owner: str, product_ids: Sequence[str], objection: str | None = None
    ) -> ProcurementTask:
        task = self._open_task(task_id, owner)
        if objection is not None and objection not in OBJECTIONS:
            raise InvalidRequest(
                f"Возражение «{objection}» не из списка {OBJECTIONS}.", code="INVALID_OBJECTION"
            )
        self._reject(task, product_ids, objection)
        self._save(task)
        return task

    def set_quantity(
        self,
        task_id: str,
        owner: str,
        product_id: str,
        quantity: int,
        *,
        source: QuantitySource = QuantitySource.USER,
        manager: bool = False,
    ) -> ProcurementTask:
        """Количество снаружи: от пользователя или менеджера. Нормативное задаёт только бэкенд."""
        if source not in EXTERNAL_QUANTITY_SOURCES:
            raise InvalidRequest(
                f"Источник количества «{source}» задаёт только бэкенд.", code="QUANTITY_SOURCE_NOT_ALLOWED"
            )
        if source is QuantitySource.MANAGER and not manager:
            raise Forbidden("Количество от менеджера задаёт только менеджер.", code="MANAGER_ONLY")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise InvalidRequest("Количество — целое от 1.", code="INVALID_QUANTITY")
        task = self._open_task(task_id, owner)
        with self.runtime.turn() as state:
            self._require_products(state, [product_id])
        task.quantities[product_id] = QuantityChoice(quantity, source)
        self._save(task)
        return task

    # --- Спецификация -----------------------------------------------------------

    def build_specification(
        self, task_id: str, owner: str, lines: Sequence[SpecificationLine] | None = None
    ) -> Specification:
        task = self._open_task(task_id, owner)
        reasons = (task.offer or {}).get("reasons", {})
        if lines:
            prepared = [
                SpecificationLine(line.product_id, line.quantity, line.source, line.reason or reasons.get(line.product_id, ""))
                for line in lines
            ]
        else:
            prepared = [
                SpecificationLine(product_id, reason=reasons.get(product_id, ""))
                for product_id in task.selected_products
            ]
        if not prepared:
            raise InvalidRequest(
                "Не выбрано ни одной позиции: сначала выберите товары из подбора.",
                code="EMPTY_SPECIFICATION",
            )
        with self.runtime.turn() as state:
            spec = self._build(task, prepared, state)
        # Количество, заданное в спецификации, остаётся за задачей: пересборка его не теряет.
        for item in spec.items:
            if item.product_id not in task.selected_products:
                task.selected_products.append(item.product_id)
            if item.quantity_source in EXTERNAL_QUANTITY_SOURCES:
                task.quantities[item.product_id] = QuantityChoice(item.quantity, item.quantity_source)
        self._advance(task, Stage.CART)
        self.repository.save_specification(spec)
        self._save(task)
        return spec

    def get_specification(self, spec_id: str, owner: str) -> Specification:
        spec = self.repository.get_specification(spec_id)
        if spec is None or spec.owner != owner:
            raise NotFound(
                "Спецификация не найдена.", code="SPECIFICATION_NOT_FOUND", details={"specification_id": spec_id}
            )
        return spec

    def check_specification(self, spec_id: str, owner: str) -> SpecificationFreshness:
        spec = self.get_specification(spec_id, owner)
        with self.runtime.turn() as state:
            return compare(spec, state)

    def revise_specification(self, spec_id: str, owner: str) -> Specification:
        """Явная пересборка по текущей версии каталога. Прежняя становится SUPERSEDED."""
        old = self.get_specification(spec_id, owner)
        if old.status is not SpecificationStatus.DRAFT:
            raise Conflict(
                f"Спецификация {spec_id} в статусе {old.status} — пересобрать нельзя.",
                code="SPECIFICATION_NOT_DRAFT",
            )
        task = self.get_task(old.task_id, owner)
        lines = [
            SpecificationLine(
                product_id=item.product_id,
                quantity=item.quantity if item.quantity_source in EXTERNAL_QUANTITY_SOURCES else None,
                source=item.quantity_source if item.quantity_source in EXTERNAL_QUANTITY_SOURCES else None,
                reason=item.selection_reason,
            )
            for item in old.items
        ]
        with self.runtime.turn() as state:
            spec = self._build(task, lines, state, parent_id=old.id)
        self.repository.save_specification(spec)
        self.repository.set_specification_status(old.id, SpecificationStatus.SUPERSEDED)
        return spec

    def finalize_specification(self, spec_id: str, owner: str) -> Specification:
        spec = self.get_specification(spec_id, owner)
        if spec.status is SpecificationStatus.SUPERSEDED:
            raise Conflict("Спецификация заменена новой.", code="SPECIFICATION_SUPERSEDED")
        if spec.status is SpecificationStatus.DRAFT:
            self.repository.set_specification_status(spec.id, SpecificationStatus.FINAL)
            spec = spec.with_status(SpecificationStatus.FINAL)
        return spec

    def export_specification(self, spec_id: str, owner: str, fmt: str) -> ExportedDocument:
        return export_specification(self.get_specification(spec_id, owner), fmt)

    # --- Внутреннее ---------------------------------------------------------------

    def _build(
        self,
        task: ProcurementTask,
        lines: Sequence[SpecificationLine],
        state: CatalogRuntimeState,
        parent_id: str | None = None,
    ) -> Specification:
        requirement = self.requirements.build(task, state.index.products)
        now = self._clock()
        return self.builder.build(
            spec_id=f"SP-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            task=task,
            lines=lines,
            state=state,
            resolution=requirement.norm,
            created_at=now.isoformat(timespec="seconds"),
            parent_id=parent_id,
        )

    def _apply(self, task: ProcurementTask, text: str | None, fields: dict[str, Any] | None) -> None:
        if text is not None:
            if len(text) > MAX_TEXT:
                raise InvalidRequest(f"Текст длиннее {MAX_TEXT} символов.", code="TEXT_TOO_LONG")
            discovery.apply_text(task, text, vocab=self.vocab)
        for name, value in (fields or {}).items():
            if name in TASK_FIELDS:
                setattr(task, name, _checked(name, value, TASK_FIELDS[name]))
            elif name in PREFERENCE_FIELDS:
                checked = _checked(name, value, PREFERENCE_FIELDS[name])
                if checked is None:
                    task.preferences.pop(name, None)
                else:
                    task.preferences[name] = checked
            else:
                raise InvalidRequest(f"Поля «{name}» у задачи нет.", code="UNKNOWN_FIELD", details={"field": name})
        if task.institution_type:
            task.institution_type = institution_code(task.institution_type) or task.institution_type

    def _reject(self, task: ProcurementTask, product_ids: Sequence[str], objection: str | None) -> None:
        for product_id in product_ids:
            if product_id not in task.rejected_products:
                task.rejected_products.append(product_id)
            if product_id in task.selected_products:
                task.selected_products.remove(product_id)
        if objection:
            task.objections.append(objection)
        if task.stage in (Stage.PRESENTATION, Stage.CART, Stage.OBJECTION):
            task.move_to(Stage.OBJECTION)

    def _advance(self, task: ProcurementTask, *stages: Stage) -> None:
        """Перевести задачу через этапы. До цели идём кратчайшим разрешённым путём:
        спецификация из готового списка кодов не обязана проходить показ."""
        for stage in stages:
            for step in task.path_to(stage):
                task.move_to(step)

    def _open_task(self, task_id: str, owner: str) -> ProcurementTask:
        task = self.get_task(task_id, owner)
        if task.is_closed:
            raise Conflict(f"Задача закрыта ({task.stage}).", code="TASK_CLOSED", details={"stage": str(task.stage)})
        return task

    def _require_products(self, state: CatalogRuntimeState, product_ids: Sequence[str]) -> None:
        missing = [pid for pid in product_ids if state.index.get(pid) is None]
        if missing:
            raise InvalidRequest(
                f"Товаров нет в каталоге версии {state.label}: {', '.join(missing)}.",
                code="UNKNOWN_PRODUCT",
                details={"product_ids": missing, "catalog_version": state.label},
            )

    def _save(self, task: ProcurementTask) -> None:
        task.updated_at = self._now()
        self.repository.save_task(task)

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")


def _checked(name: str, value: Any, kind: type) -> Any:
    if value is None:
        return None
    if kind is bool:
        if not isinstance(value, bool):
            raise InvalidRequest(f"«{name}» — да или нет.", code="INVALID_FIELD", details={"field": name})
        return value
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidRequest(f"«{name}» — целое неотрицательное число.", code="INVALID_FIELD", details={"field": name})
        if name in ("quantity", "participants", "groups") and value == 0:
            return None
        return value
    if not isinstance(value, str):
        raise InvalidRequest(f"«{name}» — строка.", code="INVALID_FIELD", details={"field": name})
    text = " ".join(value.split())
    if len(text) > MAX_FIELD:
        raise InvalidRequest(f"«{name}» длиннее {MAX_FIELD} символов.", code="INVALID_FIELD", details={"field": name})
    return text or None
