"""Order Core: файл → разбор → нормализация → сопоставление → цена, наличие, норматив → оценка.

Сервис не знает о каналах. Файл приходит байтами с именем; Telegram, виджет и
Mini App сами решают, как его получить.

Оценка закрепляет одну версию каталога (`CatalogRuntime.turn`) и записывает её,
вместе с версией нормативной базы: заказ, проверенный вчера, воспроизводим.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from catalog.matcher import MatchSettings
from catalog.runtime import CatalogRuntime, CatalogRuntimeState
from core.errors import Conflict, InvalidRequest, NotFound
from norms.mapping import NormMappingService
from norms.repository import NormRepository
from order_import.evaluation import OrderEvaluation, OrderEvaluator
from order_import.matching import MatchAssistant, OrderMatcher
from order_import.models import OrderContext, SourceFile, UploadedOrder, UploadStatus
from order_import.normalizer import OrderNormalizer
from order_import.parsers import MEDIA_TYPES, parser_for
from order_import.repository import OrderRepository

MB = 1024 * 1024
# Сигнатуры: расширение должно соответствовать содержимому.
_MAGIC = {".xlsx": b"PK", ".docx": b"PK", ".pdf": b"%PDF"}


class OrderCoreService:
    def __init__(
        self,
        repository: OrderRepository,
        runtime: CatalogRuntime,
        norms: NormRepository,
        uploads_dir: str | Path,
        *,
        max_bytes: int = 20 * MB,
        match_settings: MatchSettings | None = None,
        assistant: MatchAssistant | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self.norms = norms
        self.uploads = Path(uploads_dir)
        self.max_bytes = max_bytes
        self.match_settings = match_settings
        self.assistant = assistant
        self.normalizer = OrderNormalizer()
        self.evaluator = OrderEvaluator(NormMappingService(norms))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._matchers: dict[tuple[str, str | None], OrderMatcher] = {}

    # --- Загрузка -------------------------------------------------------------

    def upload(
        self,
        owner: str,
        channel: str,
        filename: str,
        content: bytes,
        context: OrderContext | None = None,
    ) -> UploadedOrder:
        name = Path((filename or "").replace("\\", "/")).name[:200]
        suffix = Path(name).suffix.lower()
        parser = parser_for(name)
        if not content:
            raise InvalidRequest("Файл пустой.", code="UPLOAD_REJECTED", details={"reason": "empty"})
        if len(content) > self.max_bytes:
            raise InvalidRequest(
                f"Файл больше {self.max_bytes // MB} МБ.",
                code="UPLOAD_REJECTED",
                details={"reason": "too_large", "max_bytes": self.max_bytes},
            )
        magic = _MAGIC.get(suffix)
        if magic and not content.startswith(magic):
            raise InvalidRequest(
                f"Содержимое файла не похоже на {suffix}.",
                code="UPLOAD_REJECTED",
                details={"reason": "content_mismatch"},
            )

        checksum = hashlib.sha256(content).hexdigest()
        existing = self.repository.find_by_checksum(owner, checksum)
        if existing is not None:
            return existing
        path = self._store(content, checksum, suffix)
        now = self._now()
        state = self.runtime.current()
        source = SourceFile(name, MEDIA_TYPES[suffix], len(content), checksum, path.as_posix())
        items, warnings, error, status = (), (), None, UploadStatus.PARSED
        try:
            document = parser.parse(path)
            parsed, notices = self.normalizer.normalize(document)
            items, warnings = tuple(parsed), tuple(notices)
        except InvalidRequest as exc:
            error, status = exc.message, UploadStatus.FAILED
        order = UploadedOrder(
            id=f"UO-{self._clock():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            owner=owner,
            channel=channel,
            status=status,
            source_file=source,
            parser=parser.name,
            catalog_version=state.label,
            norm_version=self.norms.version,
            context=context or OrderContext(),
            created_at=now,
            updated_at=now,
            items=items,
            warnings=warnings,
            error=error,
        )
        self.repository.save_order(order)
        return order

    def get_order(self, order_id: str, owner: str) -> UploadedOrder:
        order = self.repository.get_order(order_id)
        if order is None or order.owner != owner:
            raise NotFound("Заказ не найден.", code="ORDER_NOT_FOUND", details={"order_id": order_id})
        return order

    def orders_of(self, owner: str, limit: int = 20) -> list[UploadedOrder]:
        return self.repository.orders_of(owner, limit)

    # --- Оценка ---------------------------------------------------------------

    def evaluate(self, order_id: str, owner: str) -> OrderEvaluation:
        order = self.get_order(order_id, owner)
        if order.status is UploadStatus.FAILED:
            raise Conflict(
                f"Файл не разобран: {order.error}", code="ORDER_NOT_PARSED", details={"order_id": order_id}
            )
        with self.runtime.turn() as state:
            evaluation = self.evaluator.evaluate(
                order,
                state,
                self._matcher(state),
                evaluation_id=f"EV-{uuid.uuid4().hex[:12].upper()}",
                created_at=self._now(),
                norm_version=self.norms.version,
            )
        self.repository.save_evaluation(evaluation)
        self.repository.set_status(order.id, UploadStatus.EVALUATED, self._now())
        return evaluation

    def latest_evaluation(self, order_id: str, owner: str) -> OrderEvaluation | None:
        self.get_order(order_id, owner)
        return self.repository.latest_evaluation(order_id)

    def manual_match(self, order_id: str, line_no: int, product_id: str, actor: str) -> UploadedOrder:
        """Ручное сопоставление менеджера. Действует со следующей оценки."""
        order = self.repository.get_order(order_id)
        if order is None:
            raise NotFound("Заказ не найден.", code="ORDER_NOT_FOUND", details={"order_id": order_id})
        with self.runtime.turn() as state:
            if state.index.get(product_id) is None:
                raise InvalidRequest(
                    f"Товара {product_id} нет в каталоге версии {state.label}.",
                    code="UNKNOWN_PRODUCT",
                    details={"product_id": product_id},
                )
        if not self.repository.set_manual_match(order_id, line_no, product_id, actor, self._now()):
            raise NotFound("Строки заказа нет.", code="ORDER_LINE_NOT_FOUND", details={"line_no": line_no})
        return self.repository.get_order(order_id)  # type: ignore[return-value]

    # --- Права субъекта ПДн -------------------------------------------------------

    def delete_owner(self, owner: str) -> int:
        paths = self.repository.delete_owner(owner)
        for stored in paths:
            if not self.repository.is_file_used(stored):
                Path(stored).unlink(missing_ok=True)
        return len(paths)

    # --- Внутреннее ---------------------------------------------------------------

    def _matcher(self, state: CatalogRuntimeState) -> OrderMatcher:
        key = (state.label, state.sha256)
        matcher = self._matchers.get(key)
        if matcher is None:
            # Индекс названий строится секунды — один на версию каталога.
            self._matchers = {key: OrderMatcher(state, self.match_settings, self.assistant)}
            matcher = self._matchers[key]
        return matcher

    def _store(self, content: bytes, checksum: str, suffix: str) -> Path:
        target = self.uploads / checksum[:2] / f"{checksum}{suffix}"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".partial")
            partial.write_bytes(content)
            partial.replace(target)
        return target

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")
