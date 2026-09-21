"""Core API как объект: одна точка входа для HTTP-слоя и адаптеров в том же процессе.

HTTP (`core_api/http.py`) и Telegram вызывают одни и те же методы и получают одни и
те же DTO. Здесь нет ни одной проверки канала: канал — просто поле сессии.

Каждая операция закрепляет версию каталога (`CatalogRuntime.turn`); вложенные
вызовы доменных сервисов берут ту же версию, и она же попадает в ответ.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.errors import InvalidRequest, NotFound, Unauthorized
from core.models import CartItem, Customer
from core.ui import Response
from core_api import dto, render
from core_api.composition import CoreServices
from core_api.sessions import ADAPTER, ANONYMOUS, CoreSession, IdentityVerifier
from documents.exporters import ExportedDocument
from order_import.models import OrderContext
from preorder.models import PreorderStatus
from privacy.consent import CONSENT_TEXT, CONSENT_VERSION
from procurement.specification import SpecificationLine

DOWNLOAD_TTL = 300


@dataclass
class Result:
    data: BaseModel
    session_id: str | None = None
    task_id: str | None = None
    catalog_version: str | None = None
    norm_version: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)


class CoreApi:
    def __init__(self, services: CoreServices, verifiers: Mapping[str, IdentityVerifier] | None = None) -> None:
        self.services = services
        self.verifiers = dict(verifiers or {})
        self._downloads: dict[str, tuple[float, str, str, str]] = {}
        self._file_downloads: dict[str, tuple[float, str, bytes]] = {}
        self._downloads_lock = threading.Lock()

    @property
    def runtime(self):  # noqa: ANN201 — catalog.runtime.CatalogRuntime
        return self.services.engine.runtime

    @property
    def storage(self):  # noqa: ANN201 — core.storage.Storage
        return self.services.engine.storage

    # --- Служебное ----------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        state = self.runtime.state
        return {"status": "ok", "catalog_version": state.label, "products": len(state.index.products)}

    def catalog_status(self) -> Result:
        with self.runtime.turn() as state:
            data = dto.CatalogStatusOut(
                catalog_version=state.label,
                catalog_sha256=state.sha256,
                products=len(state.index.products),
                norm_version=self.services.norms.version,
                norm_items_loaded=self.services.norms.loaded,
            )
            return Result(data, catalog_version=state.label, norm_version=self.services.norms.version)

    # --- Сессии и ПДн --------------------------------------------------------------

    def open_session(
        self,
        *,
        channel: str,
        user_ref: str | None = None,
        credentials: dto.CredentialsIn | None = None,
        trusted: bool = False,
    ) -> Result:
        origin = ANONYMOUS
        if credentials is not None:
            verifier = self.verifiers.get(credentials.type)
            if verifier is None:
                raise InvalidRequest(
                    f"Способ входа «{credentials.type}» не подключён.", code="UNSUPPORTED_CREDENTIALS"
                )
            user_ref, channel, origin = verifier.verify(credentials.value), verifier.channel, credentials.type
        elif user_ref is not None:
            if not trusted:
                raise Unauthorized(
                    "Пользователя канала называет только адаптер с ключом Core API.", code="API_KEY_REQUIRED"
                )
            origin = ADAPTER
        session = self.services.sessions.open(channel, user_ref, origin)
        return Result(self._session_out(session), session_id=session.id)

    def session(self, session_id: str) -> CoreSession:
        return self.services.sessions.get(session_id)

    def get_session(self, session: CoreSession) -> Result:
        return Result(self._session_out(session), session_id=session.id)

    def consent(self, session: CoreSession, granted: bool) -> Result:
        """Согласие — в тот же журнал, что у бота: версия текста и действие."""
        self.storage.record_consent(session.user_ref, session.channel, CONSENT_VERSION, "granted" if granted else "revoked")
        return Result(self._session_out(session), session_id=session.id)

    def export_data(self, session: CoreSession) -> Result:
        return Result(dto.UserDataOut(data=self.storage.export_user_data(session.user_ref)), session_id=session.id)

    def delete_data(self, session: CoreSession) -> Result:
        # Та же команда, что у бота: удаление в хранилище, в памяти диалога и в модулях ядра.
        self.services.engine.handle_text(session.user_ref, session.channel, "/delete_data")
        return Result(dto.UserDataOut(data=self.storage.export_user_data(session.user_ref)), session_id=session.id)

    # --- Диалог ---------------------------------------------------------------------

    def message(self, session: CoreSession, text: str) -> Result:
        with self.runtime.turn() as state:
            replies = self.message_primitives(session, text)
            return Result(dto.DialogueOut(responses=render.responses(replies)), session.id, catalog_version=state.label)

    def action(self, session: CoreSession, action: str) -> Result:
        verb, _, arg = action.partition(":")
        if verb == "export" and arg in {"xlsx", "docx"}:
            # «Скачать Excel/Word» из Mini App: в ядре на это действие заглушка «пришлю
            # в Telegram-боте», а файлу анонимной сессии в Telegram уходить некуда.
            # Отдаём одноразовой ссылкой — как спецификацию (`export_link`); Telegram
            # до этого места не доходит, у адаптера свои файлы.
            return self._export_action(session, arg)
        with self.runtime.turn() as state:
            replies = self.action_primitives(session, action)
            return Result(dto.DialogueOut(responses=render.responses(replies)), session.id, catalog_version=state.label)

    def _export_action(self, session: CoreSession, fmt: str) -> Result:
        from core.ui import Button, Keyboard, Message

        file = self.export_dialog_list(session, fmt)
        if file is None:
            replies = [
                Message(
                    "Сохранять пока нечего: сначала соберём комплектацию или подберём позиции.",
                    keyboard=Keyboard().row(Button("Меню", "menu")),
                )
            ]
        else:
            # Подпись файла адресована чату («пришлите его боту»); в Mini App путь
            # обратно — своя вкладка загрузки, без неё пользователь упирался в
            # «нечего отправить» (21.09).
            text = (
                f"{file.caption}\n"
                "Заполненный файл можно вернуть прямо здесь: вкладка «Загрузка заказа» "
                "проверит его по каталогу и соберёт предзаказ."
            )
            replies = [
                Message(
                    text,
                    keyboard=Keyboard().row(Button("Скачать файл", "noop", url=self._remember_file(file.filename, file.content))),
                )
            ]
        return Result(dto.DialogueOut(responses=render.responses(replies)), session.id)

    def _remember_file(self, filename: str, content: bytes) -> str:
        token = secrets.token_urlsafe(24)
        with self._downloads_lock:
            now = time.monotonic()
            self._file_downloads = {key: entry for key, entry in self._file_downloads.items() if entry[0] > now}
            self._file_downloads[token] = (now + DOWNLOAD_TTL, filename, content)
        return f"/api/downloads/{token}"

    def message_primitives(self, session: CoreSession, text: str) -> list[Response]:
        """Ответ диалога примитивами `core.ui` — для адаптеров в том же процессе.

        HTTP отдаёт те же ответы в JSON (`render.responses`). Примитивы не знают канала:
        Telegram рисует их карточками, виджет — строками.
        """
        return self.services.engine.handle_text(session.user_ref, session.channel, text)

    def action_primitives(self, session: CoreSession, action: str) -> list[Response]:
        return self.services.engine.handle_action(session.user_ref, session.channel, action)

    def order_context(self, session: CoreSession) -> OrderContext:
        """Что диалог уже знает о закупке — учреждение и документ — для проверки загруженного заказа."""
        profile = self.services.engine.session(session.user_ref, session.channel).profile
        return OrderContext(
            institution_type=profile.institution,
            norm_document=profile.norm_doc_ids[0] if profile.norm_doc_ids else None,
        )

    def export_dialog_list(self, session: CoreSession, fmt: str):  # noqa: ANN201 — core.exports.ExportFile | None
        """Комплектация или список разговора файлом — кнопки «Скачать Excel» и «Скачать Word»."""
        from core import exports

        engine = self.services.engine
        with self.runtime.turn():
            return exports.build(engine, engine.session(session.user_ref, session.channel), fmt)

    def note_dialog(
        self,
        session: CoreSession,
        text: str,
        order: dto.OrderOut | None = None,
        evaluation: dto.EvaluationOut | None = None,
    ) -> None:
        """Ответ, сыгранный мимо диалога (проверка файла заказа), — в историю разговора.

        Проверенный заказ — в профиль: строки с пунктом перечня и найденным товаром. По ним бот
        отвечает на «подбери по этому заказу» и «из наличия 30 позиций» без модели.
        """
        remembered = None
        if order is not None and evaluation is not None:
            found = {"MATCHED_EXACT", "MATCHED_HIGH", "MATCHED_REVIEW"}
            remembered = {
                "id": order.id,
                "file": order.source_file.get("filename"),
                "positions": [
                    {
                        "point": item.get("norm_item"),
                        "name": item.get("source_name"),
                        "quantity": item.get("quantity"),
                        "sku": item.get("product_id") if item.get("match_status") in found else None,
                    }
                    for item in evaluation.items[:200]
                ],
            }
        self.services.engine.note(session.user_ref, session.channel, text, remembered)

    # --- Закупка -------------------------------------------------------------------------

    def create_task(self, session: CoreSession, text: str | None, fields: dict[str, Any]) -> Result:
        task = self.services.procurement.create_task(session.user_ref, session.channel, text=text, fields=fields)
        return self._task(session, task)

    def get_task(self, session: CoreSession, task_id: str) -> Result:
        return self._task(session, self.services.procurement.get_task(task_id, session.user_ref))

    def update_task(self, session: CoreSession, task_id: str, text: str | None, fields: dict[str, Any]) -> Result:
        task = self.services.procurement.update_task(task_id, session.user_ref, text=text, fields=fields)
        return self._task(session, task)

    def select(self, session: CoreSession, task_id: str, restart: bool) -> Result:
        with self.runtime.turn():
            result = self.services.procurement.select(task_id, session.user_ref, restart=restart)
        data = dto.SelectionOut.model_validate(result.to_dict())
        return Result(
            data,
            session.id,
            task_id,
            result.catalog_version,
            result.norm_version,
            [notice.to_dict() for notice in result.warnings],
        )

    def choose(self, session: CoreSession, task_id: str, product_ids: Sequence[str]) -> Result:
        return self._task(session, self.services.procurement.choose(task_id, session.user_ref, product_ids))

    def reject(self, session: CoreSession, task_id: str, product_ids: Sequence[str], objection: str | None) -> Result:
        return self._task(session, self.services.procurement.reject(task_id, session.user_ref, product_ids, objection))

    def set_quantity(self, session: CoreSession, task_id: str, product_id: str, quantity: int) -> Result:
        return self._task(session, self.services.procurement.set_quantity(task_id, session.user_ref, product_id, quantity))

    def build_specification(
        self, session: CoreSession, task_id: str, items: Sequence[dto.SpecificationItemIn] | None
    ) -> Result:
        lines = [SpecificationLine(item.product_id, item.quantity) for item in items] if items else None
        spec = self.services.procurement.build_specification(task_id, session.user_ref, lines)
        return self._specification(session, spec)

    def get_specification(self, session: CoreSession, spec_id: str) -> Result:
        return self._specification(session, self.services.procurement.get_specification(spec_id, session.user_ref))

    def check_specification(self, session: CoreSession, spec_id: str) -> Result:
        freshness = self.services.procurement.check_specification(spec_id, session.user_ref)
        return Result(dto.FreshnessOut.model_validate(freshness.to_dict()), session.id, catalog_version=freshness.current_version)

    def revise_specification(self, session: CoreSession, spec_id: str) -> Result:
        return self._specification(session, self.services.procurement.revise_specification(spec_id, session.user_ref))

    def export_specification(self, session: CoreSession, spec_id: str, fmt: str) -> tuple[ExportedDocument, str, str]:
        spec = self.services.procurement.get_specification(spec_id, session.user_ref)
        document = self.services.procurement.export_specification(spec_id, session.user_ref, fmt)
        return document, spec.catalog_version, spec.norm_version

    def export_link(self, session: CoreSession, spec_id: str, fmt: str) -> Result:
        """Одноразовая ссылка на файл — для клиентов, которые не могут скачать с заголовком.

        Mini App открывает файл ссылкой: секрет сессии в адрес не кладём, ссылка живёт
        `DOWNLOAD_TTL` секунд и срабатывает один раз.
        """
        self.services.procurement.export_specification(spec_id, session.user_ref, fmt)  # права и формат — сразу
        token = secrets.token_urlsafe(24)
        with self._downloads_lock:
            now = time.monotonic()
            self._downloads = {key: entry for key, entry in self._downloads.items() if entry[0] > now}
            self._downloads[token] = (now + DOWNLOAD_TTL, session.user_ref, spec_id, fmt)
        return Result(dto.DownloadOut(url=f"/api/downloads/{token}", expires_in=DOWNLOAD_TTL), session.id)

    def download(self, token: str) -> tuple[ExportedDocument, str, str]:
        now = time.monotonic()
        with self._downloads_lock:
            entry = self._downloads.pop(token, None)
            file_entry = self._file_downloads.pop(token, None)
        if entry is not None and entry[0] < now:
            entry = None
        if file_entry is not None and file_entry[0] < now:
            file_entry = None
        if entry is None and file_entry is None:
            raise NotFound("Ссылка недействительна или истекла.", code="DOWNLOAD_NOT_FOUND")
        if file_entry is not None:
            # Файл списка разговора уже собран (`exports.build`) — отдаём как есть.
            _, filename, content = file_entry
            media_type = {
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }.get(Path(filename).suffix.lower(), "application/octet-stream")
            return ExportedDocument(filename, media_type, content), "", ""
        _, owner, spec_id, fmt = entry
        spec = self.services.procurement.get_specification(spec_id, owner)
        document = self.services.procurement.export_specification(spec_id, owner, fmt)
        return document, spec.catalog_version, spec.norm_version

    # --- Товар и корзина --------------------------------------------------------------------

    def product(self, session: CoreSession, product_id: str, task_id: str | None = None) -> Result:
        audience = (
            self.services.procurement.get_task(task_id, session.user_ref).audience
            if task_id
            else self.services.engine.session(session.user_ref, session.channel).profile.audience
        )
        with self.runtime.turn() as state:
            product = state.index.get(product_id)
            if product is None or not product.is_active:
                raise NotFound(f"Товара {product_id} нет в каталоге.", code="PRODUCT_NOT_FOUND", details={"product_id": product_id})
            photos = list(product.images)
            if self.services.engine.photo_path(product):
                photos.insert(0, f"/media/{product.id}")
            card = product.card
            data = dto.ProductOut(
                id=product.id,
                article=product.article,
                name=product.name,
                price=product.price,
                currency=product.currency,
                availability=str(product.availability),
                quantity_available=product.quantity_available,
                description=product.description,
                kit_contents=list(product.kit_contents),
                characteristics=dict(product.characteristics),
                photos=photos,
                url=product.url,
                rooms=sorted(product.rooms),
                institution_types=sorted(product.institution_types),
                norm_mappings=[m.to_dict() for m in self.services.procurement.mapping.mappings(product, audience)],
                sources={name: {"kind": ref.kind, "origin": ref.origin} for name, ref in card.sources.items()},
            )
            return Result(data, session.id, task_id, state.label, self.services.norms.version)

    def cart(self, session: CoreSession) -> Result:
        with self.runtime.turn() as state:
            return Result(self._cart_out(session), session.id, catalog_version=state.label)

    def set_cart_item(self, session: CoreSession, product_id: str, quantity: int) -> Result:
        with self.runtime.turn() as state:
            product = state.index.get(product_id)
            if product is None or not product.is_active:
                raise NotFound(f"Товара {product_id} нет в каталоге.", code="PRODUCT_NOT_FOUND", details={"product_id": product_id})
            cart = self.storage.load_cart(session.user_ref)
            if cart.find(product_id) is not None:
                cart.set_quantity(product_id, quantity)
            elif quantity > 0:
                profile = self.services.engine.session(session.user_ref, session.channel).profile
                norm = product.norm_for(profile.audience, profile.room or "")
                cart.add(CartItem(product.sku_1c, product.name, product.price, quantity, product.url, norm.citation if norm else None))
            self.storage.save_cart(cart)
            return Result(self._cart_out(session), session.id, catalog_version=state.label)

    def clear_cart(self, session: CoreSession) -> Result:
        cart = self.storage.load_cart(session.user_ref)
        cart.clear()
        self.storage.save_cart(cart)
        return self.cart(session)

    def cart_specification(self, session: CoreSession) -> Result:
        """Спецификация из корзины — в задаче закупки, которую вёл диалог.

        Продавец подбирает через эту же задачу (`core/selection.py`), и причины подбора
        доходят до строк спецификации. Задачи нет — собираем её из того, что диалог знает.
        """
        cart = self.storage.load_cart(session.user_ref)
        if cart.is_empty:
            raise InvalidRequest("Корзина пуста — сначала добавьте товары из подбора.", code="EMPTY_CART")
        profile = self.services.engine.session(session.user_ref, session.channel).profile
        procurement = self.services.procurement
        task = self._dialog_task(session, profile.procurement_task_id)
        if task is None:
            fields: dict[str, Any] = {
                "institution_type": profile.institution,
                "room": profile.room,
                "age_group": profile.age,
                "deadline": profile.deadline,
                "norm_document": profile.norm_doc_ids[0] if profile.norm_doc_ids else None,
            }
            task = procurement.create_task(session.user_ref, session.channel, fields={k: v for k, v in fields.items() if v})
        spec = procurement.build_specification(
            task.id, session.user_ref, [SpecificationLine(item.sku_1c, item.quantity) for item in cart.items]
        )
        return self._specification(session, spec)

    def checkout(self, session: CoreSession) -> tuple[Result, Result]:
        """«Оформить»: корзина → спецификация → предзаказ. Одна цепочка для любого канала.

        Прежняя анкета из шести шагов в этой цепочке не участвует: согласие и контакт канал
        собирает поверх предзаказа — так же, как для загруженного файла заказа.
        """
        spec = self.cart_specification(session)
        assert isinstance(spec.data, dto.SpecificationOut)
        return spec, self.create_preorder(session, "specification", spec.data.id, None)

    def _dialog_task(self, session: CoreSession, task_id: str | None):  # noqa: ANN202
        if not task_id:
            return None
        try:
            task = self.services.procurement.get_task(task_id, session.user_ref)
        except NotFound:
            return None
        return None if task.is_closed else task

    # --- Заказ клиента и предзаказ ------------------------------------------------------------

    def upload_order(self, session: CoreSession, filename: str, content: bytes, context: OrderContext) -> Result:
        order = self.services.orders.upload(session.user_ref, session.channel, filename, content, context)
        return self._order(session, order)

    def get_order(self, session: CoreSession, order_id: str) -> Result:
        return self._order(session, self.services.orders.get_order(order_id, session.user_ref))

    def evaluate_order(self, session: CoreSession, order_id: str) -> Result:
        return self._evaluation(session, self.services.orders.evaluate(order_id, session.user_ref))

    def order_evaluation(self, session: CoreSession, order_id: str) -> Result:
        evaluation = self.services.orders.latest_evaluation(order_id, session.user_ref)
        if evaluation is None:
            raise NotFound("Заказ ещё не проверен.", code="EVALUATION_NOT_FOUND", details={"order_id": order_id})
        return self._evaluation(session, evaluation)

    def create_preorder(self, session: CoreSession, source: str, source_id: str, comment: str | None) -> Result:
        service = self.services.preorders
        if source == "specification":
            preorder = service.create_from_specification(source_id, session.user_ref, session.channel, comment)
        else:
            preorder = service.create_from_order(source_id, session.user_ref, session.channel, comment)
        return self._preorder(session, preorder)

    def get_preorder(self, session: CoreSession, preorder_id: str) -> Result:
        return self._preorder(session, self.services.preorders.get(preorder_id, session.user_ref))

    def send_preorder(self, session: CoreSession, preorder_id: str, customer: dto.CustomerIn) -> Result:
        preorder = self.services.preorders.send_to_manager(
            preorder_id, session.user_ref, Customer(**customer.model_dump())
        )
        return self._preorder(session, preorder)

    def history(self, session: CoreSession) -> Result:
        owner = session.user_ref
        services = self.services
        data = dto.HistoryOut(
            tasks=[
                {"id": t.id, "stage": str(t.stage), "institution_type": t.institution_type, "room": t.room, "updated_at": t.updated_at}
                for t in services.procurement.repository.tasks_of(owner)
            ],
            specifications=[
                {"id": s.id, "task_id": s.task_id, "status": str(s.status), "amount": s.totals.amount, "catalog_version": s.catalog_version, "created_at": s.created_at}
                for s in services.procurement.repository.specifications_of(owner)
            ],
            orders=[
                {"id": o.id, "status": str(o.status), "filename": o.source_file.filename, "items": len(o.items), "created_at": o.created_at}
                for o in services.orders.orders_of(owner)
            ],
            preorders=[
                {"id": p.id, "status": str(p.status), "amount": p.totals.amount, "review_required": p.review_required, "created_at": p.created_at}
                for p in services.preorders.of_owner(owner)
            ],
        )
        return Result(data, session.id)

    # --- Менеджер ------------------------------------------------------------------------------

    def manager_queue(self, status: str) -> Result:
        try:
            wanted = PreorderStatus(status)
        except ValueError as exc:
            raise InvalidRequest(f"Статуса «{status}» нет.", code="INVALID_STATUS") from exc
        items = [self._preorder_out(p) for p in self.services.preorders.manager_queue(wanted)]
        return Result(dto.PreorderListOut(preorders=items))

    def manager_preorder(self, preorder_id: str) -> Result:
        return Result(self._preorder_out(self.services.preorders.manager_get(preorder_id)))

    def manager_review(self, preorder_id: str, actor: str) -> Result:
        return Result(self._preorder_out(self.services.preorders.start_review(preorder_id, actor)))

    def manager_confirm(self, preorder_id: str, actor: str, comment: str | None) -> Result:
        return Result(self._preorder_out(self.services.preorders.confirm(preorder_id, actor, comment)))

    def manager_reject(self, preorder_id: str, actor: str, reason: str) -> Result:
        return Result(self._preorder_out(self.services.preorders.reject(preorder_id, actor, reason)))

    def manager_match(self, preorder_id: str, line_no: int, product_id: str, actor: str) -> Result:
        return Result(self._preorder_out(self.services.preorders.manual_match(preorder_id, line_no, product_id, actor)))

    def manager_quantity(self, preorder_id: str, line_no: int, quantity: int, actor: str) -> Result:
        return Result(self._preorder_out(self.services.preorders.set_quantity(preorder_id, line_no, quantity, actor)))

    def manager_match_order_line(self, order_id: str, line_no: int, product_id: str, actor: str) -> Result:
        order = self.services.orders.manual_match(order_id, line_no, product_id, actor)
        return Result(dto.OrderOut.model_validate(order.to_dict()))

    def manager_recoding(self, old_sku: str, new_sku: str, actor: str, comment: str | None) -> Result:
        return Result(dto.DecisionOut(id=self.services.preorders.record_recoding(old_sku, new_sku, actor, comment)))

    def manager_decisions(self, kind: str | None) -> Result:
        return Result(dto.DecisionsOut(decisions=self.services.preorders.decisions(kind)))

    def manager_retry_notifications(self) -> Result:
        return Result(dto.CountOut(count=self.services.preorders.retry_notifications()))

    # --- Внутреннее -----------------------------------------------------------------------------

    def _session_out(self, session: CoreSession) -> dto.SessionOut:
        active = self.storage.active_consent(session.user_ref) is not None
        return dto.SessionOut(**session.to_dict(), consent=dto.ConsentOut(version=CONSENT_VERSION, active=active, text=CONSENT_TEXT))

    def _task(self, session: CoreSession, task) -> Result:  # noqa: ANN001 — procurement.models.ProcurementTask
        data = task.to_dict()
        data.pop("owner")
        return Result(dto.TaskOut.model_validate(data), session.id, task.id)

    def _specification(self, session: CoreSession, spec) -> Result:  # noqa: ANN001
        return Result(
            dto.SpecificationOut.model_validate(spec.to_dict()),
            session.id,
            spec.task_id,
            spec.catalog_version,
            spec.norm_version,
            [notice.to_dict() for notice in spec.warnings],
        )

    def _order(self, session: CoreSession, order) -> Result:  # noqa: ANN001
        return Result(
            dto.OrderOut.model_validate(order.to_dict()),
            session.id,
            order.context.task_id,
            order.catalog_version,
            order.norm_version,
            [notice.to_dict() for notice in order.warnings],
        )

    def _evaluation(self, session: CoreSession, evaluation) -> Result:  # noqa: ANN001
        return Result(
            dto.EvaluationOut.model_validate(evaluation.to_dict()),
            session.id,
            catalog_version=evaluation.catalog_version,
            norm_version=evaluation.norm_version,
        )

    def _preorder(self, session: CoreSession, preorder) -> Result:  # noqa: ANN001
        return Result(
            self._preorder_out(preorder),
            session.id,
            catalog_version=preorder.catalog_version,
            norm_version=preorder.norm_version,
            warnings=[notice.to_dict() for notice in preorder.warnings],
        )

    @staticmethod
    def _preorder_out(preorder) -> dto.PreorderOut:  # noqa: ANN001
        return dto.PreorderOut.model_validate(preorder.to_dict())

    def _cart_out(self, session: CoreSession) -> dto.CartOut:
        cart = self.storage.load_cart(session.user_ref)
        return dto.CartOut(
            items=[
                {
                    "product_id": item.sku_1c,
                    "name": item.name,
                    "quantity": item.quantity,
                    "price": item.price,
                    "total": item.total if item.price is not None else None,
                    "url": item.url,
                    "norm_citation": item.norm_citation,
                }
                for item in cart.items
            ],
            count=cart.count,
            total=cart.total,
            complete=all(item.price is not None for item in cart.items),
        )
