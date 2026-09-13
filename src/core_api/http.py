"""HTTP-слой Core API: `/api/*` поверх `CoreApi`.

Контракт:

- **успех** — `{"schema", "status": "ok", "request_id", "session_id", "task_id",
  "catalog_version", "norm_version", "data", "warnings", "errors": []}`;
- **ошибка** — `{"schema", "status": "error", "request_id", "error": {"code",
  "message", "details"}}`. Клиент смотрит на `code`, текст не разбирает;
- сессия — заголовок `X-Session-Id`; адаптер с ключом — `X-Core-Api-Key`;
  менеджер — `X-Manager-Key` и `X-Manager-Actor`.

Формат ошибок действует только на `/api`: `/widget/*`, `/health`, `/media/*`
отвечают как раньше.
"""

from __future__ import annotations

import hmac
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.config import Settings
from core.errors import (
    Conflict,
    DomainError,
    Forbidden,
    InvalidRequest,
    NotFound,
    Unauthorized,
    Unavailable,
)
from core_api import dto
from core_api.facade import CoreApi, Result
from core_api.sessions import CoreSession
from order_import.models import OrderContext

log = logging.getLogger(__name__)

PREFIX = "/api"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_ACTOR = re.compile(r"^[\w .@\-]{1,64}$")

_STATUS: tuple[tuple[type[DomainError], int], ...] = (
    (NotFound, 404),
    (Unauthorized, 401),
    (Forbidden, 403),
    (Conflict, 409),
    (Unavailable, 503),
    (InvalidRequest, 400),
)


def envelope(request: Request, result: Result) -> dict[str, Any]:
    return {
        "schema": dto.SCHEMA,
        "status": "ok",
        "request_id": _request_id(request),
        "session_id": result.session_id,
        "task_id": result.task_id,
        "catalog_version": result.catalog_version,
        "norm_version": result.norm_version,
        "data": result.data.model_dump(mode="json"),
        "warnings": result.warnings,
        "errors": [],
    }


def error_body(request: Request, code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": dto.SCHEMA,
        "status": "error",
        "request_id": _request_id(request),
        "error": {"code": code, "message": message, "details": details or {}},
    }


def ok(request: Request, result: Result, status_code: int = 200) -> JSONResponse:
    return JSONResponse(envelope(request, result), status_code=status_code)


def install(app: FastAPI, get_core: Callable[[], CoreApi], settings: Settings) -> None:
    """Подключить `/api` к приложению: маршруты, идентификатор запроса, формат ошибок."""

    @app.middleware("http")
    async def request_id(request: Request, call_next):  # noqa: ANN001, ANN202
        if not request.url.path.startswith(PREFIX):
            return await call_next(request)
        incoming = request.headers.get("X-Request-Id", "")
        request.state.request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        status = next((code for kind, code in _STATUS if isinstance(exc, kind)), 400)
        return JSONResponse(error_body(request, exc.code, exc.message, exc.details), status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        if not request.url.path.startswith(PREFIX):
            return await request_validation_exception_handler(request, exc)
        details = {
            "fields": [
                {"location": [str(part) for part in error["loc"]], "message": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ]
        }
        return JSONResponse(error_body(request, "VALIDATION_ERROR", "Запрос не прошёл проверку.", details), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if not request.url.path.startswith(PREFIX):
            return await http_exception_handler(request, exc)
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(error_body(request, code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        if not request.url.path.startswith(PREFIX):
            return PlainTextResponse("Internal Server Error", status_code=500)
        # Подробности — в журнал сервера, клиенту только код: трассировка наружу не уходит.
        log.exception("Core API: необработанная ошибка %s %s", request.method, request.url.path)
        return JSONResponse(
            error_body(request, "INTERNAL_ERROR", "Внутренняя ошибка. Повторите запрос позже."), status_code=500
        )

    app.include_router(create_router(get_core, settings))


def create_router(get_core: Callable[[], CoreApi], settings: Settings) -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    def core() -> CoreApi:
        return get_core()

    def session(request: Request, api: CoreApi = Depends(core)) -> CoreSession:
        return api.session(request.headers.get("X-Session-Id", ""))

    def trusted(request: Request) -> bool:
        key = request.headers.get("X-Core-Api-Key")
        if key is None:
            return False
        if not settings.core_api_key:
            raise Unavailable("Ключ Core API не настроен на сервере.", code="CORE_API_KEY_NOT_CONFIGURED")
        if not hmac.compare_digest(key.encode(), settings.core_api_key.encode()):
            raise Unauthorized("Неверный ключ Core API.", code="INVALID_API_KEY")
        return True

    def manager(request: Request) -> str:
        if not settings.core_manager_key:
            raise Unavailable("Ключ менеджера не настроен на сервере.", code="MANAGER_KEY_NOT_CONFIGURED")
        key = request.headers.get("X-Manager-Key", "")
        if not hmac.compare_digest(key.encode(), settings.core_manager_key.encode()):
            raise Forbidden("Неверный ключ менеджера.", code="INVALID_MANAGER_KEY")
        actor = request.headers.get("X-Manager-Actor", "manager").strip()
        if not _ACTOR.match(actor):
            raise InvalidRequest("Недопустимое имя менеджера.", code="INVALID_ACTOR")
        return actor

    # --- Служебное -------------------------------------------------------------------

    @router.get("/health")
    def health(api: CoreApi = Depends(core)) -> dict[str, Any]:
        return {"schema": dto.SCHEMA, **api.health()}

    @router.get("/catalog/status")
    def catalog_status(request: Request, api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.catalog_status())

    # --- Сессии ------------------------------------------------------------------------

    @router.post("/sessions")
    def open_session(
        request: Request, body: dto.SessionIn | None = None, api: CoreApi = Depends(core), is_trusted: bool = Depends(trusted)
    ) -> JSONResponse:
        body = body or dto.SessionIn()
        result = api.open_session(channel=body.channel, user_ref=body.user_ref, credentials=body.credentials, trusted=is_trusted)
        return ok(request, result, 201)

    def by_path(session_id: str, api: CoreApi = Depends(core)) -> CoreSession:
        return api.session(session_id)

    @router.get("/sessions/{session_id}")
    def get_session(request: Request, current: CoreSession = Depends(by_path), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.get_session(current))

    @router.post("/sessions/{session_id}/consent")
    def consent(request: Request, body: dto.ConsentIn, current: CoreSession = Depends(by_path), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.consent(current, body.granted))

    @router.get("/sessions/{session_id}/data")
    def export_data(request: Request, current: CoreSession = Depends(by_path), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.export_data(current))

    @router.delete("/sessions/{session_id}/data")
    def delete_data(request: Request, current: CoreSession = Depends(by_path), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.delete_data(current))

    # Текущая сессия — по заголовку `X-Session-Id`. Идентификатор сессии и есть пропуск, а
    # адрес запроса оседает в журналах сервера и прокси: публичному клиенту (Mini App)
    # класть его в путь незачем. Маршруты с `{session_id}` оставлены для совместимости.

    @router.get("/session")
    def current_session(request: Request, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.get_session(current))

    @router.post("/session/consent")
    def current_consent(request: Request, body: dto.ConsentIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.consent(current, body.granted))

    @router.get("/session/data")
    def current_export(request: Request, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.export_data(current))

    @router.delete("/session/data")
    def current_delete(request: Request, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.delete_data(current))

    # --- Диалог -------------------------------------------------------------------------

    @router.post("/dialogue/message")
    def message(request: Request, body: dto.MessageIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.message(current, body.text))

    @router.post("/dialogue/action")
    def action(request: Request, body: dto.ActionIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.action(current, body.action))

    # --- Закупка -------------------------------------------------------------------------

    @router.post("/procurement/tasks")
    def create_task(request: Request, body: dto.TaskIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.create_task(current, body.text, body.fields), 201)

    @router.get("/procurement/tasks/{task_id}")
    def get_task(request: Request, task_id: str, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.get_task(current, task_id))

    @router.patch("/procurement/tasks/{task_id}")
    def update_task(request: Request, task_id: str, body: dto.TaskIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.update_task(current, task_id, body.text, body.fields))

    @router.post("/procurement/tasks/{task_id}/choose")
    def choose(request: Request, task_id: str, body: dto.ProductsIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.choose(current, task_id, body.product_ids))

    @router.post("/procurement/tasks/{task_id}/reject")
    def reject(request: Request, task_id: str, body: dto.RejectIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.reject(current, task_id, body.product_ids, body.objection))

    @router.post("/procurement/tasks/{task_id}/quantity")
    def quantity(request: Request, task_id: str, body: dto.QuantityIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.set_quantity(current, task_id, body.product_id, body.quantity))

    @router.post("/procurement/select")
    def select(request: Request, body: dto.SelectIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.select(current, body.task_id, body.restart))

    @router.post("/procurement/specification")
    def specification(request: Request, body: dto.SpecificationIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.build_specification(current, body.task_id, body.items), 201)

    @router.get("/procurement/specifications/{spec_id}")
    def get_specification(request: Request, spec_id: str, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.get_specification(current, spec_id))

    @router.get("/procurement/specifications/{spec_id}/check")
    def check_specification(request: Request, spec_id: str, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.check_specification(current, spec_id))

    @router.post("/procurement/specifications/{spec_id}/revise")
    def revise_specification(request: Request, spec_id: str, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.revise_specification(current, spec_id), 201)

    @router.get("/procurement/specifications/{spec_id}/export")
    def export_specification(
        spec_id: str,
        format: str = Query(default="xlsx", pattern=r"^[a-z]{3,5}$"),  # noqa: A002 — имя параметра запроса
        current: CoreSession = Depends(session),
        api: CoreApi = Depends(core),
    ) -> Response:
        return file_response(*api.export_specification(current, spec_id, format))

    @router.post("/procurement/specifications/{spec_id}/export-link")
    def export_link(
        request: Request,
        spec_id: str,
        format: str = Query(default="xlsx", pattern=r"^[a-z]{3,5}$"),  # noqa: A002 — имя параметра запроса
        current: CoreSession = Depends(session),
        api: CoreApi = Depends(core),
    ) -> JSONResponse:
        return ok(request, api.export_link(current, spec_id, format), 201)

    @router.get("/downloads/{token}")
    def download(token: str, api: CoreApi = Depends(core)) -> Response:
        return file_response(*api.download(token))

    # --- Товар и корзина -------------------------------------------------------------------

    @router.get("/products/{product_id}")
    def product(
        request: Request,
        product_id: str,
        task_id: str | None = Query(default=None, pattern=dto.HEX32),
        current: CoreSession = Depends(session),
        api: CoreApi = Depends(core),
    ) -> JSONResponse:
        return ok(request, api.product(current, product_id, task_id))

    @router.get("/cart")
    def cart(request: Request, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.cart(current))

    @router.post("/cart/items")
    def cart_item(request: Request, body: dto.CartItemIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.set_cart_item(current, body.product_id, body.quantity))

    @router.delete("/cart")
    def clear_cart(request: Request, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.clear_cart(current))

    @router.post("/cart/specification")
    def cart_specification(request: Request, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.cart_specification(current), 201)

    # --- Заказы и предзаказы -----------------------------------------------------------------

    @router.post("/orders/upload")
    async def upload_order(
        request: Request,
        filename: str = Query(min_length=1, max_length=200),
        institution_type: str | None = Query(default=None, max_length=50),
        norm_document: str | None = Query(default=None, max_length=50),
        norm_item: str | None = Query(default=None, max_length=30),
        task_id: str | None = Query(default=None, pattern=dto.HEX32),
        current: CoreSession = Depends(session),
        api: CoreApi = Depends(core),
    ) -> JSONResponse:
        limit = settings.order_upload_max_mb * 1024 * 1024
        declared = request.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > limit:
            raise InvalidRequest(
                f"Файл больше {settings.order_upload_max_mb} МБ.", code="UPLOAD_REJECTED", details={"reason": "too_large"}
            )
        content = await request.body()
        context = OrderContext(institution_type, norm_document, norm_item, task_id)
        result = await run_in_threadpool(api.upload_order, current, filename, content, context)
        return ok(request, result, 201)

    @router.get("/orders/{order_id}")
    def get_order(request: Request, order_id: str, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.get_order(current, order_id))

    @router.post("/orders/{order_id}/evaluate")
    def evaluate(request: Request, order_id: str, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.evaluate_order(current, order_id))

    @router.get("/orders/{order_id}/evaluation")
    def evaluation(request: Request, order_id: str, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.order_evaluation(current, order_id))

    @router.post("/preorders")
    def create_preorder(request: Request, body: dto.PreorderIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.create_preorder(current, body.source, body.source_id, body.comment), 201)

    @router.get("/preorders/{preorder_id}")
    def get_preorder(request: Request, preorder_id: str, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.get_preorder(current, preorder_id))

    @router.post("/preorders/{preorder_id}/send")
    def send_preorder(request: Request, preorder_id: str, body: dto.SendPreorderIn, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.send_preorder(current, preorder_id, body.customer))

    @router.get("/history")
    def history(request: Request, current: CoreSession = Depends(session), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.history(current))

    # --- Менеджер ------------------------------------------------------------------------------

    @router.get("/manager/preorders")
    def manager_queue(request: Request, status: str = Query(default="SENT_TO_MANAGER"), actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_queue(status))

    @router.get("/manager/preorders/{preorder_id}")
    def manager_preorder(request: Request, preorder_id: str, actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_preorder(preorder_id))

    @router.post("/manager/preorders/{preorder_id}/review")
    def manager_review(request: Request, preorder_id: str, actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_review(preorder_id, actor))

    @router.post("/manager/preorders/{preorder_id}/confirm")
    def manager_confirm(request: Request, preorder_id: str, body: dto.ManagerCommentIn, actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_confirm(preorder_id, actor, body.comment))

    @router.post("/manager/preorders/{preorder_id}/reject")
    def manager_reject(request: Request, preorder_id: str, body: dto.ManagerRejectIn, actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_reject(preorder_id, actor, body.reason))

    @router.post("/manager/preorders/{preorder_id}/items/{line_no}/match")
    def manager_match(request: Request, preorder_id: str, line_no: int, body: dto.ManualMatchIn, actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_match(preorder_id, line_no, body.product_id, actor))

    @router.post("/manager/preorders/{preorder_id}/items/{line_no}/quantity")
    def manager_quantity(request: Request, preorder_id: str, line_no: int, body: dto.ManagerQuantityIn, actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_quantity(preorder_id, line_no, body.quantity, actor))

    @router.post("/manager/orders/{order_id}/items/{line_no}/match")
    def manager_order_match(request: Request, order_id: str, line_no: int, body: dto.ManualMatchIn, actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_match_order_line(order_id, line_no, body.product_id, actor))

    @router.post("/manager/recodings")
    def manager_recoding(request: Request, body: dto.RecodingIn, actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_recoding(body.old_sku, body.new_sku, actor, body.comment), 201)

    @router.get("/manager/decisions")
    def manager_decisions(request: Request, kind: str | None = Query(default=None, pattern=r"^[a-z_]{1,32}$"), actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_decisions(kind))

    @router.post("/manager/notifications/retry")
    def manager_retry(request: Request, actor: str = Depends(manager), api: CoreApi = Depends(core)) -> JSONResponse:
        return ok(request, api.manager_retry_notifications())

    return router


def file_response(document, catalog_version: str, norm_version: str) -> Response:  # noqa: ANN001 — ExportedDocument
    return Response(
        document.content,
        media_type=document.media_type,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(document.filename),
            "X-Catalog-Version": catalog_version,
            "X-Norm-Version": norm_version,
        },
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid.uuid4().hex
