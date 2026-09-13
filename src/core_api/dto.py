"""DTO Core API: запросы и ответы.

Наружу уходят только эти модели. Внутренние объекты (владелец, путь к файлу на
диске, строки базы) в ответ не попадают: у моделей ответа `extra="forbid"`, и
лишнее поле роняет ответ в тестах, а не утекает к клиенту.

Конверт ответа — `envelope`/`error_body` в `core_api/http.py`: `schema`, `status`,
`request_id`, `session_id`, `task_id`, `catalog_version`, `norm_version`, `data`,
`warnings`, `errors`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA = "vdm.core.v1"
HEX32 = r"^[0-9a-f]{32}$"
PRODUCT_ID = r"^[0-9A-Za-zА-Яа-яЁё_\-]{1,64}$"
RESOURCE_ID = r"^[A-Za-z0-9_\-]{1,64}$"


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CredentialsIn(In):
    type: str = Field(pattern=r"^[a-z_]{1,32}$")
    value: str = Field(min_length=1, max_length=4096)


class SessionIn(In):
    channel: str = Field(default="api", pattern=r"^[a-z][a-z0-9_]{1,31}$")
    user_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_:.\-]{1,64}$")
    credentials: CredentialsIn | None = None


class ConsentIn(In):
    granted: bool = True


class MessageIn(In):
    text: str = Field(min_length=1, max_length=2000)


class ActionIn(In):
    action: str = Field(min_length=1, max_length=128)


class TaskIn(In):
    text: str | None = Field(default=None, max_length=2000)
    fields: dict[str, Any] = Field(default_factory=dict)


class SelectIn(In):
    task_id: str = Field(pattern=HEX32)
    restart: bool = False


class ProductsIn(In):
    product_ids: list[str] = Field(min_length=1, max_length=50)


class RejectIn(ProductsIn):
    objection: str | None = Field(default=None, max_length=16)


class QuantityIn(In):
    product_id: str = Field(pattern=PRODUCT_ID)
    quantity: int = Field(ge=1, le=100_000)


class SpecificationItemIn(In):
    product_id: str = Field(pattern=PRODUCT_ID)
    quantity: int | None = Field(default=None, ge=1, le=100_000)


class SpecificationIn(In):
    task_id: str = Field(pattern=HEX32)
    items: list[SpecificationItemIn] | None = Field(default=None, max_length=500)


class CartItemIn(In):
    product_id: str = Field(pattern=PRODUCT_ID)
    # 0 — убрать позицию из корзины.
    quantity: int = Field(ge=0, le=100_000)


class PreorderIn(In):
    source: Literal["specification", "uploaded_order"]
    source_id: str = Field(pattern=RESOURCE_ID)
    comment: str | None = Field(default=None, max_length=1000)


class CustomerIn(In):
    name: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=50)
    email: str = Field(default="", max_length=200)
    organization: str = Field(default="", max_length=300)
    region: str = Field(default="", max_length=200)
    comment: str = Field(default="", max_length=1000)


class SendPreorderIn(In):
    customer: CustomerIn


class ManagerCommentIn(In):
    comment: str | None = Field(default=None, max_length=1000)


class ManagerRejectIn(In):
    reason: str = Field(min_length=1, max_length=1000)


class ManualMatchIn(In):
    product_id: str = Field(pattern=PRODUCT_ID)


class ManagerQuantityIn(In):
    quantity: int = Field(ge=1, le=100_000)


class RecodingIn(In):
    old_sku: str = Field(pattern=PRODUCT_ID)
    new_sku: str = Field(pattern=PRODUCT_ID)
    comment: str | None = Field(default=None, max_length=1000)


# --- Ответы -------------------------------------------------------------------------


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoticeOut(Out):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ConsentOut(Out):
    version: str
    active: bool
    # Текст согласия той редакции, на которую соглашаются: клиент его показывает, а не пишет свой.
    text: str


class SessionOut(Out):
    id: str
    channel: str
    user_ref: str
    origin: str
    created_at: str
    last_seen_at: str
    consent: ConsentOut


class TaskOut(Out):
    id: str
    channel: str
    created_at: str
    updated_at: str
    institution_type: str | None
    institution_name: str | None
    room: str | None
    zone: str | None
    grade: str | None
    age_group: str | None
    goal: str | None
    norm_required: bool | None
    norm_document: str | None
    norm_item: str | None
    budget: int | None
    deadline: str | None
    quantity: int | None
    preferences: dict[str, Any]
    selected_products: list[str]
    rejected_products: list[str]
    shown_products: list[str]
    quantities: dict[str, Any]
    objections: list[str]
    offer: dict[str, Any] | None
    stage: str


class SelectionOut(Out):
    task_id: str
    status: str
    catalog_version: str
    catalog_sha256: str | None
    norm_version: str
    items: list[dict[str, Any]]
    has_more: bool
    remaining: int
    matched: int
    candidates: int
    filters: list[dict[str, Any]]
    norm: dict[str, Any]
    warnings: list[NoticeOut]
    questions: list[str]


class SpecificationOut(Out):
    id: str
    task_id: str
    status: str
    created_at: str
    catalog_version: str
    catalog_sha256: str | None
    norm_version: str
    header: dict[str, Any]
    items: list[dict[str, Any]]
    totals: dict[str, Any]
    warnings: list[NoticeOut]
    parent_id: str | None


class FreshnessOut(Out):
    specification_id: str
    status: str
    specification_version: str
    current_version: str
    changes: list[dict[str, Any]]


class ProductOut(Out):
    id: str
    article: str
    name: str
    price: int | None
    currency: str
    availability: str
    quantity_available: int | None
    description: str
    kit_contents: list[str]
    characteristics: dict[str, str]
    photos: list[str]
    url: str | None
    rooms: list[str]
    institution_types: list[str]
    norm_mappings: list[dict[str, Any]]
    sources: dict[str, Any]


class CartOut(Out):
    items: list[dict[str, Any]]
    count: int
    total: int
    complete: bool


class DialogueOut(Out):
    responses: list[dict[str, Any]]


class OrderOut(Out):
    id: str
    status: str
    source_file: dict[str, Any]
    parser: str | None
    catalog_version: str
    norm_version: str
    context: dict[str, Any]
    created_at: str
    updated_at: str
    items: list[dict[str, Any]]
    warnings: list[NoticeOut]
    error: str | None


class EvaluationOut(Out):
    id: str
    order_id: str
    status: str
    catalog_version: str
    norm_version: str
    created_at: str
    items: list[dict[str, Any]]
    summary: dict[str, Any]


class PreorderOut(Out):
    id: str
    source: str
    source_id: str
    evaluation_id: str | None
    status: str
    is_final_order: bool
    catalog_version: str
    norm_version: str
    review_required: bool
    items: list[dict[str, Any]]
    totals: dict[str, Any]
    warnings: list[NoticeOut]
    customer: dict[str, Any] | None
    comment: str | None
    manager_comment: str | None
    history: list[dict[str, Any]]
    notification: str | None
    created_at: str
    updated_at: str


class PreorderListOut(Out):
    preorders: list[PreorderOut]


class HistoryOut(Out):
    tasks: list[dict[str, Any]]
    specifications: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    preorders: list[dict[str, Any]]


class CatalogStatusOut(Out):
    catalog_version: str
    catalog_sha256: str | None
    products: int
    norm_version: str
    norm_items_loaded: bool


class UserDataOut(Out):
    data: dict[str, Any]


class DecisionsOut(Out):
    decisions: list[dict[str, Any]]


class CountOut(Out):
    count: int


class DecisionOut(Out):
    id: str


class DownloadOut(Out):
    url: str
    expires_in: int
