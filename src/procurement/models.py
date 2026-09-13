"""Доменные объекты закупки: задача, требование, подбор, спецификация.

Не зависят ни от канала, ни от хранилища. Профиль разговора (`core/profile.py`)
остаётся диалогу; задача закупки — отдельная модель с тем же белым списком: только
описание закупки, ничего о человеке.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any

from catalog.models import Availability
from catalog.placement import institution_code
from core.errors import Conflict, Notice
from norms.mapping import NormCheckStatus, NormMapping
from norms.selector import NormResolution


class Stage(StrEnum):
    DISCOVERY = "DISCOVERY"
    SELECTION = "SELECTION"
    PRESENTATION = "PRESENTATION"
    OBJECTION = "OBJECTION"
    REVISION = "REVISION"
    CART = "CART"
    ORDER = "ORDER"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


_TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.DISCOVERY: frozenset({Stage.SELECTION}),
    Stage.SELECTION: frozenset({Stage.PRESENTATION, Stage.DISCOVERY}),
    Stage.PRESENTATION: frozenset({Stage.SELECTION, Stage.OBJECTION, Stage.REVISION, Stage.CART}),
    Stage.OBJECTION: frozenset({Stage.SELECTION, Stage.REVISION, Stage.PRESENTATION, Stage.CART}),
    Stage.REVISION: frozenset({Stage.SELECTION, Stage.PRESENTATION, Stage.CART}),
    Stage.CART: frozenset({Stage.ORDER, Stage.REVISION, Stage.SELECTION, Stage.OBJECTION}),
    Stage.ORDER: frozenset({Stage.COMPLETED, Stage.CART}),
    Stage.COMPLETED: frozenset(),
    Stage.ABANDONED: frozenset(),
}
CLOSED_STAGES = frozenset({Stage.COMPLETED, Stage.ABANDONED})


class QuantitySource(StrEnum):
    NORM = "norm"
    USER = "user"
    CALCULATED = "calculated"
    DEFAULT = "default"
    MANAGER = "manager"


# Кто задаёт количество снаружи ядра. Нормативное, рассчитанное и количество по
# умолчанию определяет только бэкенд: ни модель, ни канал выдать своё число за
# нормативное не могут.
EXTERNAL_QUANTITY_SOURCES = frozenset({QuantitySource.USER, QuantitySource.MANAGER})

QUANTITY_SOURCE_LABELS = {
    QuantitySource.NORM: "по нормативу",
    QuantitySource.USER: "указано клиентом",
    QuantitySource.CALCULATED: "рассчитано",
    QuantitySource.DEFAULT: "по умолчанию",
    QuantitySource.MANAGER: "указано менеджером",
}

OBJECTIONS = ("price", "norm", "trust", "logistics", "docs", "other")


@dataclass
class QuantityChoice:
    quantity: int
    source: QuantitySource


# Поля задачи, которые меняются снаружи, и их типы.
TASK_FIELDS: dict[str, type] = {
    "institution_type": str,
    "institution_name": str,
    "room": str,
    "zone": str,
    "grade": str,
    "age_group": str,
    "goal": str,
    "norm_required": bool,
    "norm_document": str,
    "norm_item": str,
    "budget": int,
    "deadline": str,
    "quantity": int,
}
PREFERENCE_FIELDS: dict[str, type] = {
    "query": str,
    "category": str,
    "available_only": bool,
    "participants": int,
    "groups": int,
}


@dataclass
class ProcurementTask:
    id: str
    owner: str
    channel: str
    created_at: str
    updated_at: str
    # `preschool` / `school`, если распознано; иначе слово пользователя («колледж»).
    institution_type: str | None = None
    institution_name: str | None = None
    room: str | None = None
    zone: str | None = None
    grade: str | None = None
    age_group: str | None = None
    goal: str | None = None
    # `None` — норматив запрошен, если назван документ или пункт.
    norm_required: bool | None = None
    norm_document: str | None = None
    norm_item: str | None = None
    budget: int | None = None
    deadline: str | None = None
    # Сколько штук просил пользователь: «20 штук».
    quantity: int | None = None
    preferences: dict[str, Any] = field(default_factory=dict)
    selected_products: list[str] = field(default_factory=list)
    rejected_products: list[str] = field(default_factory=list)
    shown_products: list[str] = field(default_factory=list)
    quantities: dict[str, QuantityChoice] = field(default_factory=dict)
    objections: list[str] = field(default_factory=list)
    # Последняя выдача: признак требования, версия каталога, позиции и причины подбора.
    offer: dict[str, Any] | None = None
    stage: Stage = Stage.DISCOVERY

    @property
    def audience(self) -> str | None:
        return institution_code(self.institution_type)

    @property
    def is_closed(self) -> bool:
        return self.stage in CLOSED_STAGES

    def can_move(self, stage: Stage) -> bool:
        return stage == self.stage or stage in _TRANSITIONS[self.stage] or (
            stage is Stage.ABANDONED and not self.is_closed
        )

    def path_to(self, stage: Stage) -> list[Stage]:
        """Кратчайший разрешённый путь до этапа; пустой — уже там или пути нет."""
        if stage == self.stage:
            return []
        queue: list[tuple[Stage, list[Stage]]] = [(self.stage, [])]
        seen = {self.stage}
        while queue:
            current, path = queue.pop(0)
            for following in sorted(_TRANSITIONS[current]):
                if following in seen:
                    continue
                if following == stage:
                    return [*path, following]
                seen.add(following)
                queue.append((following, [*path, following]))
        return [stage]  # пути нет — `move_to` откажет с понятной ошибкой

    def move_to(self, stage: Stage) -> None:
        if not self.can_move(stage):
            raise Conflict(
                f"Задача на этапе {self.stage}: переход в {stage} невозможен.",
                code="STAGE_TRANSITION_NOT_ALLOWED",
                details={"from": str(self.stage), "to": str(stage)},
            )
        self.stage = stage

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stage"] = str(self.stage)
        data["quantities"] = {
            sku: {"quantity": choice.quantity, "source": str(choice.source)}
            for sku, choice in self.quantities.items()
        }
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProcurementTask:
        data = dict(raw)
        data["stage"] = Stage(data.get("stage", Stage.DISCOVERY))
        data["quantities"] = {
            sku: QuantityChoice(int(choice["quantity"]), QuantitySource(choice["source"]))
            for sku, choice in (data.get("quantities") or {}).items()
        }
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass(frozen=True)
class UserRequest:
    """Что хочет пользователь — поля задачи и слова запроса."""

    text: str
    institution_type: str | None
    institution_name: str | None
    room: str | None
    zone: str | None
    grade: str | None
    age_group: str | None
    category: str | None
    goal: str | None
    budget: int | None
    deadline: str | None
    quantity: int | None
    available_only: bool
    participants: int | None
    groups: int | None


@dataclass(frozen=True)
class ProcurementRequirement:
    """Требование к подбору: пожелания пользователя отдельно от требований норматива."""

    task_id: str
    user: UserRequest
    # Что требует норматив — только из нормативной базы и привязок каталога.
    norm: NormResolution
    audience: str | None
    # Помещение, выделенное разделом каталога; `None` — фильтра по помещению нет.
    catalog_room: str | None
    exclude: frozenset[str]
    warnings: tuple[Notice, ...] = ()

    @property
    def signature(self) -> str:
        """Признак требования: сменился — прежняя выдача больше не «уже показанное»."""
        user = self.user
        parts = (
            user.text, user.institution_type, user.room, user.zone, user.age_group,
            user.category, user.budget, user.available_only, self.norm.document,
            self.norm.point, str(self.norm.status),
        )
        return "|".join("" if part is None else str(part) for part in parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user": asdict(self.user),
            "norm": self.norm.to_dict(),
            "audience": self.audience,
            "catalog_room": self.catalog_room,
            "warnings": [notice.to_dict() for notice in self.warnings],
        }


class SelectionStatus(StrEnum):
    FOUND = "FOUND"
    EMPTY = "EMPTY"
    # Задача не описана настолько, чтобы искать: нет ни помещения, ни запроса.
    NEEDS_DETAILS = "NEEDS_DETAILS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class Alternative:
    product_id: str
    article: str
    name: str
    price: int | None
    availability: Availability

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "availability": str(self.availability)}


@dataclass(frozen=True)
class SelectionItem:
    product_id: str
    article: str
    name: str
    price: int | None
    currency: str
    availability: Availability
    quantity_available: int | None
    quantity: int
    quantity_source: QuantitySource
    quantity_note: str
    total_price: int | None
    reason: str
    norm_mappings: tuple[NormMapping, ...]
    norm_status: NormCheckStatus
    confidence: float
    alternatives: tuple[Alternative, ...]
    url: str | None
    image: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "article": self.article,
            "name": self.name,
            "price": self.price,
            "currency": self.currency,
            "availability": str(self.availability),
            "quantity_available": self.quantity_available,
            "quantity": self.quantity,
            "quantity_source": str(self.quantity_source),
            "quantity_note": self.quantity_note,
            "total_price": self.total_price,
            "reason": self.reason,
            "norm_mappings": [mapping.to_dict() for mapping in self.norm_mappings],
            "norm_status": str(self.norm_status),
            "confidence": self.confidence,
            "alternatives": [alternative.to_dict() for alternative in self.alternatives],
            "url": self.url,
            "image": self.image,
        }


@dataclass(frozen=True)
class SelectionResult:
    task_id: str
    status: SelectionStatus
    catalog_version: str
    catalog_sha256: str | None
    norm_version: str
    items: tuple[SelectionItem, ...]
    has_more: bool
    remaining: int
    matched: int
    candidates: int
    filters: tuple[dict[str, Any], ...]
    norm: NormResolution
    warnings: tuple[Notice, ...] = ()
    questions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": str(self.status),
            "catalog_version": self.catalog_version,
            "catalog_sha256": self.catalog_sha256,
            "norm_version": self.norm_version,
            "items": [item.to_dict() for item in self.items],
            "has_more": self.has_more,
            "remaining": self.remaining,
            "matched": self.matched,
            "candidates": self.candidates,
            "filters": list(self.filters),
            "norm": self.norm.to_dict(),
            "warnings": [notice.to_dict() for notice in self.warnings],
            "questions": list(self.questions),
        }


class SpecificationStatus(StrEnum):
    DRAFT = "DRAFT"
    # Передана в предзаказ: больше не пересобирается.
    FINAL = "FINAL"
    # Пересобрана по новой версии каталога — действует дочерняя.
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class SpecificationItem:
    line_no: int
    product_id: str
    article: str
    name: str
    quantity: int
    quantity_source: QuantitySource
    quantity_note: str
    unit: str
    unit_price: int | None
    total_price: int | None
    availability: Availability
    norm_document: str | None
    norm_item: str | None
    norm_item_title: str | None
    norm_status: NormCheckStatus
    selection_reason: str
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for name in ("quantity_source", "availability", "norm_status"):
            data[name] = str(data[name])
        return data


@dataclass(frozen=True)
class SpecificationTotals:
    positions: int
    quantity: int
    # Сумма известных цен. `complete` — у всех позиций цена известна.
    amount: int
    complete: bool
    missing_prices: int
    budget: int | None
    over_budget: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Specification:
    id: str
    task_id: str
    owner: str
    status: SpecificationStatus
    created_at: str
    catalog_version: str
    catalog_sha256: str | None
    norm_version: str
    header: dict[str, Any]
    items: tuple[SpecificationItem, ...]
    totals: SpecificationTotals
    warnings: tuple[Notice, ...] = ()
    parent_id: str | None = None

    def with_status(self, status: SpecificationStatus) -> Specification:
        return replace(self, status=status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "status": str(self.status),
            "created_at": self.created_at,
            "catalog_version": self.catalog_version,
            "catalog_sha256": self.catalog_sha256,
            "norm_version": self.norm_version,
            "header": dict(self.header),
            "items": [item.to_dict() for item in self.items],
            "totals": self.totals.to_dict(),
            "warnings": [notice.to_dict() for notice in self.warnings],
            "parent_id": self.parent_id,
        }


class FreshnessStatus(StrEnum):
    CURRENT = "CURRENT"
    CATALOG_CHANGED = "CATALOG_CHANGED"


@dataclass(frozen=True)
class ItemChange:
    line_no: int
    product_id: str
    field: str
    old: Any
    new: Any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SpecificationFreshness:
    """Что изменилось в каталоге после спецификации. Сама спецификация не меняется."""

    specification_id: str
    status: FreshnessStatus
    specification_version: str
    current_version: str
    changes: tuple[ItemChange, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "specification_id": self.specification_id,
            "status": str(self.status),
            "specification_version": self.specification_version,
            "current_version": self.current_version,
            "changes": [change.to_dict() for change in self.changes],
        }
