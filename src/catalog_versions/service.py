"""Версии каталога: baseline, утверждение, применение, откат, восстановление (EPIC 4, D11).

Каждая версия — неизменяемый снимок `data/kb/versions/<версия>/products.jsonl`.
Текущую версию называет указатель `data/kb/current` (`catalog/current.py`).

Применение любой версии — одним порядком, под файловым замком ОС:

    замок → снимок во временную папку → проверка и sha256 → папка версии →
    версия READY → замена указателя → история товаров, версия и импорт APPLIED →
    снятие замка

Транзакция SQLite держится только над записью в базу, не над файлами. Если процесс
упал, любая следующая пишущая команда `catalog` начинает с восстановления
(`recover`). Историю товаров она всегда пересчитывает из двух снимков — родителя
и версии, а не из сохранённого diff.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import shutil
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from catalog.current import (
    SNAPSHOT_NAME,
    CatalogPointerError,
    CatalogSnapshot,
    Pointer,
    file_sha256,
    read_pointer,
    read_records,
    snapshot_path,
    verify_pointer,
    versions_dir,
    write_pointer,
)
from catalog_import.models import ImportStatus
from catalog_versions.cards import (
    MEDIA_FIELDS,
    NORM_FIELDS,
    Record,
    apply_registry,
    change_counters,
    forbidden_changes,
    load_registry,
    product_changes,
    serialize,
    strip_registry,
    validate_records,
)
from catalog_versions.lock import CatalogApplyLock
from catalog_versions.models import (
    SOURCE_LABELS,
    ApplyResult,
    CatalogVersion,
    VersionSource,
    VersionStatus,
)
from catalog_versions.repository import SqliteVersionRepository

if TYPE_CHECKING:
    from catalog_import.service import CatalogImportService
    from core.config import Settings

log = logging.getLogger(__name__)

BASELINE_VERSION = "baseline"
BUILDING_PREFIX = ".building-"
LOCK_NAME = ".catalog.lock"
APPROVABLE = frozenset({ImportStatus.PARSED, ImportStatus.FAILED})


class CatalogVersionError(RuntimeError):
    """Отказ команды каталога: причина и что делать — в тексте."""


class SafetyCheckFailed(CatalogVersionError):
    """Изменение превышает порог безопасности; разрешается только `--force`."""


@dataclass(frozen=True)
class Thresholds:
    max_removed_share: float = 0.10
    max_price_changed_share: float = 0.30

    def exceeded(self, removed_share: float, price_changed_share: float) -> list[str]:
        problems = []
        if removed_share > self.max_removed_share:
            problems.append(
                f"исчезает {removed_share:.1%} товаров (порог {self.max_removed_share:.0%})"
            )
        if price_changed_share > self.max_price_changed_share:
            problems.append(
                f"цена меняется у {price_changed_share:.1%} товаров "
                f"(порог {self.max_price_changed_share:.0%})"
            )
        return problems


class CatalogVersionService:
    def __init__(
        self,
        versions: SqliteVersionRepository,
        imports: CatalogImportService | None,
        kb_path: str | Path,
        root: str | Path,
        *,
        thresholds: Thresholds | None = None,
        registry_path: Path | None = None,
        clock: Callable[[], str] | None = None,
        lock_timeout: float = 60.0,
    ) -> None:
        self.versions = versions
        self.imports = imports
        self.kb_path = Path(kb_path)
        self.kb_dir = self.kb_path.parent
        self.root = Path(root)
        self.thresholds = thresholds or Thresholds()
        self.registry_path = registry_path
        self.lock_timeout = lock_timeout
        self._now = clock or _now

    # --- Команды ---------------------------------------------------------------

    def init(self, by: str) -> CatalogVersion:
        """Baseline из legacy `products.jsonl`: побайтная копия, история всех товаров, указатель."""
        with self._lock():
            self._recover_locked()
            pointer = read_pointer(self.kb_dir)
            if pointer is not None or self.versions.has_versions():
                current = pointer.version if pointer else "нет указателя"
                raise CatalogVersionError(
                    f"Каталог уже ведётся версиями (текущая: {current}). "
                    "`catalog init` ничего не перезаписывает."
                )
            if not self.kb_path.is_file():
                raise CatalogVersionError(f"Нет исходного каталога {self.kb_path}: не из чего собрать baseline.")
            payload = self.kb_path.read_bytes()
            records = read_records(self.kb_path)
            return self._apply_locked(
                records=records,
                payload=payload,
                source=VersionSource.BASELINE,
                parent=None,
                parent_records=None,
                by=by,
                version=BASELINE_VERSION,
                inputs={"legacy_path": self._relative(self.kb_path)},
            )

    def approve(self, import_id: str, by: str, force: bool = False) -> CatalogVersion:
        """Утвердить импорт 1С и применить его версию."""
        if self.imports is None:
            raise CatalogVersionError("Сервис импорта не подключён.")
        with self._lock():
            self._recover_locked()
            record = self.imports.require(import_id)
            if record.status not in APPROVABLE:
                raise CatalogVersionError(
                    f"Импорт {import_id} в статусе {record.status}: утверждается только "
                    "разобранный и ещё не применённый импорт (PARSED или FAILED)."
                )
            if not record.diff_fingerprint or not record.base_version:
                raise CatalogVersionError(
                    f"У импорта {import_id} нет сохранённого diff. "
                    f"Пересчитайте: python run.py import-1c --rediff {import_id}"
                )
            current, snapshot = self._current_locked()
            if current is None:
                raise CatalogVersionError(
                    "Каталог ещё не ведётся версиями. Сначала: python run.py catalog init"
                )
            if not _same_base(record.base_version, current):
                raise CatalogVersionError(
                    f"Diff импорта {import_id} посчитан против {record.base_version}, а текущая "
                    f"версия каталога — {current.version}. Устаревший diff не утверждается: "
                    f"python run.py import-1c --rediff {import_id}"
                )
            current_records = snapshot.records()
            diff = self.imports.diff_for(import_id, snapshot, current_records)
            if diff.fingerprint != record.diff_fingerprint:
                raise CatalogVersionError(
                    f"Отпечаток diff импорта {import_id} изменился: сохранён "
                    f"{record.diff_fingerprint[:16]}…, сейчас {diff.fingerprint[:16]}…. "
                    "Каталог, реестр или фото поменялись после того, как diff показали. "
                    f"Посмотрите заново: python run.py import-1c --rediff {import_id}"
                )
            self._check_records(diff.candidate)
            counters = diff.counters
            forced = self._safety(counters.removed_share, counters.price_changed_share, force)
            return self._apply_locked(
                records=diff.candidate,
                source=VersionSource.ONE_C,
                parent=current,
                parent_records=current_records,
                by=by,
                import_id=import_id,
                forced=forced,
                counters_extra={"diff": counters.to_dict()},
                inputs={
                    "diff_fingerprint": diff.fingerprint,
                    "registry_sha256": diff.registry_sha256,
                },
            )

    def rollback(self, target: str, by: str, force: bool = False) -> CatalogVersion:
        """Новая версия — копия снимка выбранной. Указатель назад не переводится."""
        with self._lock():
            self._recover_locked()
            current, snapshot = self._current_locked()
            if current is None:
                raise CatalogVersionError("Каталог ещё не ведётся версиями: откатывать не на что.")
            wanted = self.versions.get(target)
            if wanted is None or wanted.status is not VersionStatus.APPLIED:
                raise CatalogVersionError(
                    f"Версии {target} нет среди применённых: откат возможен только на неё."
                )
            if wanted.version == current.version:
                raise CatalogVersionError(f"Версия {target} и так текущая.")
            path = snapshot_path(self.kb_dir, wanted.version)
            if not path.is_file() or file_sha256(path) != wanted.sha256:
                raise CatalogVersionError(
                    f"Снимок версии {target} отсутствует или повреждён: откат невозможен."
                )
            payload = path.read_bytes()
            records = read_records(path)
            current_records = snapshot.records()
            counters = change_counters(
                product_changes(current_records, records), len(records), len(current_records)
            )
            forced = self._safety(counters["removed_share"], counters["price_changed_share"], force)
            return self._apply_locked(
                records=records,
                payload=payload,
                source=VersionSource.ROLLBACK,
                parent=current,
                parent_records=current_records,
                by=by,
                forced=forced,
                inputs={"rollback_to": wanted.version},
            )

    def publish_media(
        self,
        media: Mapping[str, list[str]],
        attributes: Mapping[str, dict[str, str]],
        by: str,
    ) -> ApplyResult:
        """Версия `media`: меняются только фото и характеристики. Без изменений — ничего."""

        def change(records: list[Record]) -> None:
            for record in records:
                images = media.get(record["sku_1c"])
                if images and record.get("images") != images:
                    record["images"] = list(images)
                found = attributes.get(record["sku_1c"])
                if found and record.get("attributes") != found:
                    record["attributes"] = dict(found)

        return self._publish(VersionSource.MEDIA, MEDIA_FIELDS, change, by, {})

    def publish_registry(self, by: str) -> ApplyResult:
        """Версия `registry`: реестр 1057 применяется к текущему снимку заново."""
        registry, sha = load_registry(self.registry_path)
        if sha is None:
            raise CatalogVersionError(f"Реестра 1057 нет: {self.registry_path}.")

        def change(records: list[Record]) -> None:
            strip_registry(records)
            apply_registry(records, registry)

        return self._publish(
            VersionSource.REGISTRY, NORM_FIELDS, change, by, {"registry_sha256": sha}
        )

    def recover(self) -> list[str]:
        with self._lock():
            return self._recover_locked()

    def check(self) -> list[str]:
        """Диагностика без изменений: указатель, база, незавершённые версии, сироты."""
        findings: list[str] = []
        try:
            pointer = read_pointer(self.kb_dir)
        except CatalogPointerError as exc:
            return [f"ОШИБКА: {exc}"]
        known = self.versions.known_versions()
        if pointer is None:
            findings.append(
                "Указателя нет: каталог читается из legacy-файла."
                if not known
                else "ОШИБКА: версии в базе есть, а указателя нет — нужна сверка (catalog recover)."
            )
        else:
            try:
                verify_pointer(self.kb_dir, pointer)
                findings.append(f"Указатель: {pointer.version}, снимок цел (sha256 {pointer.sha256[:12]}…).")
            except CatalogPointerError as exc:
                findings.append(f"ОШИБКА: {exc}")
            if pointer.version not in known:
                findings.append(f"ОШИБКА: версии {pointer.version} из указателя нет в базе.")
        latest = self.versions.latest_applied()
        if latest and pointer and latest.version != pointer.version:
            findings.append(
                f"ОШИБКА: последняя применённая версия {latest.version}, а указатель на {pointer.version}."
            )
        findings += [
            f"Незавершённая версия {version.version} (READY): её завершит или отметит FAILED catalog recover."
            for version in self.versions.ready()
        ]
        folder = versions_dir(self.kb_dir)
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.name.startswith(BUILDING_PREFIX):
                    findings.append(f"Недостроенный снимок {path.name}: будет удалён при следующей команде.")
                elif path.is_dir() and path.name not in known:
                    findings.append(
                        f"Папка-сирота {path.name}: версии в базе нет, снимок не используется."
                    )
        return findings

    def list_versions(self, limit: int = 20) -> list[CatalogVersion]:
        return self.versions.list_versions(limit)

    def current_version(self) -> str | None:
        pointer = read_pointer(self.kb_dir)
        return pointer.version if pointer else None

    # --- Применение ------------------------------------------------------------

    def _publish(
        self,
        source: VersionSource,
        allowed: frozenset[str],
        change: Callable[[list[Record]], None],
        by: str,
        inputs: dict[str, Any],
    ) -> ApplyResult:
        with self._lock():
            self._recover_locked()
            current, snapshot = self._current_locked()
            if current is None:
                raise CatalogVersionError(
                    "Каталог ещё не ведётся версиями. Сначала: python run.py catalog init"
                )
            current_records = snapshot.records()
            records = copy.deepcopy(current_records)
            change(records)
            problems = forbidden_changes(current_records, records, allowed)
            if problems:
                raise CatalogVersionError(
                    f"Версия «{SOURCE_LABELS[source]}» меняет то, что ей менять нельзя: "
                    + "; ".join(problems[:5])
                )
            if not product_changes(current_records, records):
                return ApplyResult(
                    None, f"Изменений нет: версия «{SOURCE_LABELS[source]}» не создана."
                )
            version = self._apply_locked(
                records=records,
                source=source,
                parent=current,
                parent_records=current_records,
                by=by,
                inputs=inputs,
            )
            return ApplyResult(version, f"Создана и применена версия {version.version}.")

    def _apply_locked(
        self,
        *,
        records: Sequence[Record],
        source: VersionSource,
        parent: CatalogVersion | None,
        parent_records: Sequence[Record] | None,
        by: str,
        payload: bytes | None = None,
        import_id: str | None = None,
        forced: bool = False,
        version: str | None = None,
        counters_extra: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> CatalogVersion:
        self._check_records(records)
        pointer = read_pointer(self.kb_dir)
        expected = parent.version if parent else None
        if (pointer.version if pointer else None) != expected:
            raise CatalogVersionError(
                f"Текущая версия каталога сменилась ({pointer.version if pointer else 'нет'}), "
                f"ожидалась {expected}. Повторите команду."
            )
        data = payload if payload is not None else serialize(records)
        changes = product_changes(parent_records, records)
        counters = change_counters(changes, len(records), len(parent_records or ()))
        counters.update(counters_extra or {})
        number = version or self.versions.next_version(
            self._now()[:10], lambda name: (versions_dir(self.kb_dir) / name).exists()
        )
        sha = self._write_snapshot(number, data)

        created = self._now()
        record = CatalogVersion(
            version=number,
            parent_version=expected,
            source=source,
            status=VersionStatus.READY,
            created_at=created,
            created_by=by,
            import_id=import_id,
            snapshot_path=self._relative(snapshot_path(self.kb_dir, number)),
            sha256=sha,
            product_count=len(records),
            counters=counters,
            inputs=inputs or {},
            forced=forced,
        )
        self.versions.create_ready(record, created)
        try:
            write_pointer(self.kb_dir, Pointer(number, sha))
        except Exception as exc:
            self.versions.mark_failed(number, f"указатель не записан: {type(exc).__name__}: {exc}")
            raise
        # Указатель уже на новой версии. Если запись истории сорвётся, версия останется
        # READY под указателем, и восстановление её завершит.
        self.versions.finalize(number, self._now(), changes)
        log.info(
            "Каталог: применена версия %s (%s), товаров %s, sha256 %s",
            number,
            source,
            len(records),
            sha[:12],
        )
        return self.versions.get(number)

    def _write_snapshot(self, number: str, data: bytes) -> str:
        """Снимок во временную папку, проверка sha256, затем папка версии целиком."""
        folder = versions_dir(self.kb_dir)
        final = snapshot_path(self.kb_dir, number).parent
        if final.exists():
            raise CatalogVersionError(f"Папка версии {final} уже существует: снимок не перезаписывается.")
        building = folder / f"{BUILDING_PREFIX}{uuid.uuid4().hex}"
        building.mkdir(parents=True)
        try:
            target = building / SNAPSHOT_NAME
            with target.open("wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            sha = hashlib.sha256(data).hexdigest()
            if file_sha256(target) != sha:
                raise CatalogVersionError(f"Записанный снимок версии {number} не совпал с собранным.")
            os.replace(building, final)
        except BaseException:
            shutil.rmtree(building, ignore_errors=True)
            raise
        return sha

    # --- Восстановление --------------------------------------------------------

    def _recover_locked(self) -> list[str]:
        """Сверка указателя, базы и снимков после возможного падения процесса."""
        messages: list[str] = []
        folder = versions_dir(self.kb_dir)
        if folder.is_dir():
            for path in folder.glob(f"{BUILDING_PREFIX}*"):
                shutil.rmtree(path, ignore_errors=True)
                messages.append(f"удалён недостроенный снимок {path.name}")

        pointer = read_pointer(self.kb_dir)
        if pointer is not None:
            under = self.versions.get(pointer.version)
            if under is None:
                raise CatalogVersionError(
                    f"Указатель ссылается на версию {pointer.version}, которой нет в базе. "
                    "Каталог не меняется до разбора вручную (catalog check)."
                )
            if under.sha256 != pointer.sha256:
                raise CatalogVersionError(
                    f"sha256 версии {pointer.version} в указателе и в базе расходятся. "
                    "Каталог не меняется до разбора вручную."
                )
            verify_pointer(self.kb_dir, pointer)
            if under.status is VersionStatus.READY:
                self._finish_locked(under)
                messages.append(f"версия {under.version}: указатель уже на ней, применение завершено")
            elif under.status is VersionStatus.FAILED:
                raise CatalogVersionError(
                    f"Указатель смотрит на версию {under.version} со статусом FAILED. "
                    "Каталог не меняется до разбора вручную."
                )

        for ready in self.versions.ready():
            pointer = read_pointer(self.kb_dir)
            current = pointer.version if pointer else None
            path = snapshot_path(self.kb_dir, ready.version)
            if not path.is_file():
                self.versions.mark_failed(ready.version, "снимок отсутствует")
                messages.append(f"версия {ready.version}: снимка нет — FAILED")
                continue
            if file_sha256(path) != ready.sha256:
                self.versions.mark_failed(ready.version, "sha256 снимка не совпадает")
                messages.append(f"версия {ready.version}: снимок повреждён — FAILED")
                continue
            if ready.parent_version != current:
                self.versions.mark_failed(
                    ready.version, f"текущая версия {current}, а версия собрана от {ready.parent_version}"
                )
                messages.append(f"версия {ready.version}: собрана от другой версии — FAILED")
                continue
            write_pointer(self.kb_dir, Pointer(ready.version, ready.sha256))
            self._finish_locked(ready)
            messages.append(f"версия {ready.version}: применение продолжено и завершено")

        latest = self.versions.latest_applied()
        pointer = read_pointer(self.kb_dir)
        if latest is not None and (pointer is None or pointer.version != latest.version):
            path = snapshot_path(self.kb_dir, latest.version)
            if not path.is_file() or file_sha256(path) != latest.sha256:
                raise CatalogVersionError(
                    f"Последняя применённая версия {latest.version} расходится с указателем, "
                    "а её снимок отсутствует или повреждён. Каталог не меняется."
                )
            write_pointer(self.kb_dir, Pointer(latest.version, latest.sha256))
            messages.append(
                f"указатель сверен с базой: {pointer.version if pointer else 'нет'} → {latest.version}"
            )
        for message in messages:
            log.warning("Восстановление каталога: %s", message)
        return messages

    def _finish_locked(self, version: CatalogVersion) -> None:
        """История из двух снимков, версия и импорт APPLIED."""
        records = read_records(snapshot_path(self.kb_dir, version.version))
        parent_records = None
        if version.parent_version:
            parent = self.versions.get(version.parent_version)
            path = snapshot_path(self.kb_dir, version.parent_version)
            if parent is None or not path.is_file() or file_sha256(path) != parent.sha256:
                raise CatalogVersionError(
                    f"Снимок родителя {version.parent_version} недоступен: история версии "
                    f"{version.version} не восстанавливается."
                )
            parent_records = read_records(path)
        self.versions.finalize(version.version, self._now(), product_changes(parent_records, records))

    # --- Вспомогательное -------------------------------------------------------

    def _current_locked(self) -> tuple[CatalogVersion | None, CatalogSnapshot | None]:
        pointer = read_pointer(self.kb_dir)
        if pointer is None:
            return None, None
        snapshot = verify_pointer(self.kb_dir, pointer)
        version = self.versions.get(pointer.version)
        if version is None:
            raise CatalogVersionError(f"Версии {pointer.version} из указателя нет в базе.")
        return version, snapshot

    def _safety(self, removed_share: float, price_changed_share: float, force: bool) -> bool:
        problems = self.thresholds.exceeded(removed_share, price_changed_share)
        if problems and not force:
            raise SafetyCheckFailed(
                "Остановлено проверкой безопасности: "
                + "; ".join(problems)
                + ". Если так и должно быть — повторите с --force."
            )
        return bool(problems)

    @staticmethod
    def _check_records(records: Sequence[Record]) -> None:
        problems = validate_records(records)
        if problems:
            raise CatalogVersionError(
                "Снимок не прошёл проверку (--force это не отменяет): " + "; ".join(problems[:5])
            )

    def _relative(self, path: Path) -> str:
        try:
            return Path(os.path.relpath(path, self.root)).as_posix()
        except ValueError as exc:
            raise CatalogVersionError(
                f"Путь {path} нельзя записать относительно корня проекта {self.root}."
            ) from exc

    def _lock(self) -> CatalogApplyLock:
        return CatalogApplyLock(self.kb_dir / LOCK_NAME, timeout=self.lock_timeout)


def _same_base(base_version: str, current: CatalogVersion) -> bool:
    """Diff против legacy-файла действителен для baseline — побайтной копии этого файла."""
    if base_version == current.version:
        return True
    return base_version == f"legacy:{current.sha256}" and current.source is VersionSource.BASELINE


def build_version_service(settings: Settings, root: Path | None = None) -> CatalogVersionService:
    from catalog_import.service import build_service
    from ingest.norm_registry import DEFAULT_REGISTRY

    versions = SqliteVersionRepository(settings.catalog_db_path)
    kb_path = Path(settings.kb_path)
    return CatalogVersionService(
        versions,
        build_service(settings, returning=versions.returning_codes),
        kb_path,
        root or Path.cwd(),
        thresholds=Thresholds(
            settings.catalog_max_removed_share, settings.catalog_max_price_changed_share
        ),
        registry_path=kb_path.parent / DEFAULT_REGISTRY.name,
    )


# --- Вывод ------------------------------------------------------------------------


def format_version(version: CatalogVersion, current: str | None = None) -> str:
    mark = "▶" if version.version == current else " "
    counters = version.counters
    parts = [
        f"{mark} {version.version:<15} {version.status:<8} {SOURCE_LABELS[version.source]}",
        f"товаров {counters.get('products', version.product_count)}",
        f"новых {counters.get('new', 0)}, изменено {counters.get('updated', 0)}, "
        f"исчезло {counters.get('removed', 0)}, цена у {counters.get('price_changed', 0)}",
    ]
    if version.parent_version:
        parts.append(f"от {version.parent_version}")
    if version.import_id:
        parts.append(f"импорт {version.import_id}")
    if version.inputs.get("rollback_to"):
        parts.append(f"откат на {version.inputs['rollback_to']}")
    if version.forced:
        parts.append("--force")
    if version.created_by:
        parts.append(version.created_by)
    parts.append(version.applied_at or version.created_at)
    if version.error:
        parts.append(f"ошибка: {version.error}")
    return " · ".join(parts)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
