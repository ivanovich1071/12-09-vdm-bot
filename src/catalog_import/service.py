"""Импорт выгрузки 1С: загрузка → контрольная сумма → разбор → проверка → diff → предпросмотр.

Загрузка каталог бота не меняет (D5): товары импорта и diff лежат в своих
таблицах. Сопоставление с каталогом — `matching.py` (EPIC 3), изменения по
позициям — `diff.py`, утверждение и применение — `catalog_versions` (EPIC 4).
"""

from __future__ import annotations

import logging
import uuid
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from catalog.matcher import CatalogMatcher, MatchSettings
from catalog_import.diff import CatalogDiff, DiffCounters, DiffRow, compute_diff
from catalog_import.files import MB, MIME_TYPES, FileStore, check_upload, checksum, megabytes
from catalog_import.matching import MATCH_STATUS_SHORT_LABELS, compare_with_catalog
from catalog_import.models import (
    CatalogComparison,
    CatalogImport,
    ImportItem,
    ImportStatus,
    ImportSummary,
    Issue,
    Severity,
    StoredFile,
)
from catalog_import.parser import ParsedSheet, parse_products, read_bitrix_ids
from catalog_import.repository import ImportRepository, SqliteImportRepository
from catalog_import.validator import ISSUE_LABELS, file_issues, row_issues
from catalog_versions.cards import Record, load_registry
from ingest.xlsx_reader import XlsxFile

if TYPE_CHECKING:
    from catalog.current import CatalogSnapshot
    from catalog.repository import CatalogRepository
    from core.config import Settings

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 50 * MB

# Ошибки, с которыми файл не читается как книга Excel вообще: не zip, битый
# архив, нет обязательных частей книги, испорченный XML.
_UNREADABLE = (zipfile.BadZipFile, zlib.error, EOFError, KeyError, ET.ParseError, UnicodeDecodeError)


@dataclass(frozen=True)
class Inspection:
    """Результат разбора и проверки одного файла. Ничего не записано."""

    status: ImportStatus
    summary: ImportSummary
    items: list[ImportItem] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    # Все коды 1С файла, включая исключённые, — для сравнения с каталогом.
    codes: frozenset[str] = frozenset()


class ImportStateError(RuntimeError):
    """Действие с импортом невозможно в его нынешнем состоянии."""


# Diff пересчитывается только до применения: применённый импорт уже стал версией.
REDIFF_STATUSES = frozenset({ImportStatus.PARSED, ImportStatus.FAILED})


class CatalogImportService:
    def __init__(
        self,
        repository: ImportRepository,
        files: FileStore,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        catalog: Callable[[], CatalogRepository] | None = None,
        current: Callable[[], CatalogSnapshot] | None = None,
        registry_path: Path | None = None,
        returning: Callable[[list[str]], set[str]] | None = None,
        match_settings: MatchSettings | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self.files = files
        self.max_bytes = max_bytes
        # Только сопоставление, без diff: так импорт работал до EPIC 4.
        self.catalog = catalog
        # Текущий каталог по резолверу: против него считается и сохраняется diff.
        self.current = current
        self.registry_path = registry_path
        # Какие из новых кодов уже были в каталоге раньше (история версий).
        self.returning = returning
        self.match_settings = match_settings
        self._now = clock or _now

    def upload(self, source: str | Path, uploaded_by: str) -> CatalogImport:
        """Загрузка на проверку. Тот же файл второй раз возвращает прежний импорт."""
        source = Path(source)
        check_upload(source, self.max_bytes)
        digest = checksum(source)

        existing = self.repository.find_by_checksum(digest)
        if existing is not None and existing.status is not ImportStatus.UPLOADED:
            log.info("Импорт %s: файл %s уже загружали", existing.id, source.name)
            return replace(existing, duplicate=True)

        stored = self.files.store(source, digest)
        now = self._now()
        record = existing or self.repository.create(
            StoredFile(
                id=uuid.uuid4().hex,
                filename=source.name,
                mime_type=MIME_TYPES[source.suffix.lower()],
                size=source.stat().st_size,
                checksum=digest,
                storage_path=str(stored),
                uploaded_by=uploaded_by,
                uploaded_at=now,
            )
        )

        try:
            inspection = inspect(stored, source_name=record.file.filename, now=now)
        except Exception as exc:
            # Импорт остаётся UPLOADED с причиной: повторная загрузка разберёт заново.
            self.repository.fail(record.id, f"{type(exc).__name__}: {exc}")
            raise

        summary = inspection.summary
        diff = None
        if inspection.status is ImportStatus.PARSED:
            snapshot = self.current_snapshot()
            if snapshot is not None:
                diff = self._compute(inspection.items, inspection.codes, snapshot)
                summary = replace(
                    summary, comparison=diff.comparison, diff=diff.counters.to_dict()
                )
            else:
                summary = replace(summary, comparison=self._compare(inspection))
        log.info(
            "Импорт %s: %s, товаров %s, принято %s, ошибок %s, предупреждений %s",
            record.id,
            inspection.status,
            summary.products,
            summary.accepted,
            summary.errors,
            summary.warnings,
        )
        return self.repository.finish(
            record.id, inspection.status, summary, inspection.items, inspection.issues, now, diff
        )

    def rediff(self, import_id: str) -> CatalogImport:
        """Diff заново против текущего каталога: новая базовая версия и новый отпечаток."""
        record = self.require(import_id)
        if record.status not in REDIFF_STATUSES:
            raise ImportStateError(
                f"Импорт {import_id} в статусе {record.status}: diff пересчитывается только "
                "у разобранного и ещё не применённого импорта (PARSED или FAILED)."
            )
        snapshot = self.current_snapshot()
        if snapshot is None:
            raise ImportStateError("Текущего каталога нет — сравнивать импорт не с чем.")
        diff = self.diff_for(import_id, snapshot)
        summary = replace(record.summary, comparison=diff.comparison, diff=diff.counters.to_dict())
        log.info(
            "Импорт %s: diff пересчитан против %s, отпечаток %s",
            import_id,
            diff.base_version,
            diff.fingerprint[:12],
        )
        return self.repository.save_diff(import_id, diff, summary, self._now())

    def diff_for(
        self,
        import_id: str,
        snapshot: CatalogSnapshot,
        current_records: list[Record] | None = None,
    ) -> CatalogDiff:
        """Diff сохранённого импорта против заданного снимка. Ничего не записывает."""
        items = self.repository.items(import_id)
        codes = {item.sku_1c for item in items} | self.repository.rejected_codes(import_id)
        return self._compute(items, codes, snapshot, current_records)

    def diff_rows(self, import_id: str) -> list[DiffRow]:
        return self.repository.diff_rows(import_id)

    def require(self, import_id: str) -> CatalogImport:
        record = self.get(import_id)
        if record is None:
            raise ImportStateError(f"Импорта {import_id} нет.")
        return record

    def current_snapshot(self) -> CatalogSnapshot | None:
        """Текущий каталог. `None` — ни указателя, ни собранной базы знаний."""
        if self.current is None:
            return None
        try:
            return self.current()
        except FileNotFoundError:
            return None

    def get(self, import_id: str) -> CatalogImport | None:
        return self.repository.get(import_id)

    def list_imports(self, limit: int = 20) -> list[CatalogImport]:
        return self.repository.list_imports(limit)

    def items(self, import_id: str) -> list[ImportItem]:
        return self.repository.items(import_id)

    def issues(
        self, import_id: str, severity: Severity | None = None, limit: int | None = None
    ) -> list[Issue]:
        return self.repository.issues(import_id, severity, limit)

    def _compare(self, inspection: Inspection) -> CatalogComparison | None:
        if self.catalog is None:
            return None
        catalog = self.catalog()
        matcher = CatalogMatcher(catalog, self.match_settings)
        matching = compare_with_catalog(inspection.items, inspection.codes, catalog, matcher)
        return matching.comparison

    def _compute(
        self,
        items: list[ImportItem],
        file_codes: set[str] | frozenset[str],
        snapshot: CatalogSnapshot,
        current_records: list[Record] | None = None,
    ) -> CatalogDiff:
        registry, registry_sha = load_registry(self.registry_path)
        return compute_diff(
            items,
            file_codes,
            snapshot,
            registry=registry,
            registry_sha256=registry_sha,
            match_settings=self.match_settings,
            returning=self.returning,
            current_records=current_records,
        )


def inspect(path: Path, *, source_name: str, now: str) -> Inspection:
    """Разбор и проверка файла выгрузки."""
    try:
        with XlsxFile(path) as book:
            names = book.sheet_names
            # Строки читаются целиком здесь, чтобы битый архив дал INVALID, а не
            # исключение посреди разбора. Лист товаров — около 10 МБ текста.
            products_rows = list(book.numbered_rows(0)) if names else []
            bitrix_rows = list(book.numbered_rows(1)) if len(names) > 1 else []
    except _UNREADABLE as exc:
        return _invalid(
            Issue(
                Severity.ERROR,
                "unreadable_file",
                f"Файл не читается как книга Excel ({type(exc).__name__}).",
            )
        )
    if not names:
        return _invalid(Issue(Severity.ERROR, "no_sheets", "В книге нет ни одного листа."))

    bitrix = read_bitrix_ids(bitrix_rows)
    sheet = parse_products(
        products_rows, bitrix_ids=bitrix.by_name, now=now, source_name=source_name
    )
    problems = file_issues(sheet)
    if problems:
        return Inspection(ImportStatus.INVALID, _summary(sheet, problems), issues=problems)

    issues = row_issues(sheet, bitrix)
    rejected = {
        issue.sku_1c for issue in issues if issue.severity is Severity.ERROR and issue.sku_1c
    }
    coded = [product for product in sheet.products if product.sku_1c]
    items = [
        ImportItem(
            sku_1c=product.sku_1c,
            name=product.name,
            price=product.price,
            stock=product.in_stock,
            rows=list(sheet.rows_by_key[product.sku_1c]),
            payload=asdict(product),
        )
        for product in coded
        if product.sku_1c not in rejected
    ]
    summary = _summary(
        sheet,
        issues,
        accepted=len(items),
        rejected=sum(product.sku_1c in rejected for product in coded),
    )
    return Inspection(
        ImportStatus.PARSED, summary, items, issues, frozenset(p.sku_1c for p in coded)
    )


def build_service(
    settings: Settings, returning: Callable[[list[str]], set[str]] | None = None
) -> CatalogImportService:
    """Сервис по настройкам приложения — для командной строки, а позже для админки."""
    from catalog.current import resolve_catalog
    from catalog_versions.repository import SqliteVersionRepository
    from ingest.norm_registry import DEFAULT_REGISTRY

    kb_path = Path(settings.kb_path)
    if returning is None:
        returning = SqliteVersionRepository(settings.catalog_db_path).returning_codes

    def current() -> CatalogSnapshot:
        return resolve_catalog(kb_path)

    return CatalogImportService(
        SqliteImportRepository(settings.catalog_db_path),
        FileStore(settings.uploads_dir),
        max_bytes=settings.import_max_mb * MB,
        current=current,
        registry_path=kb_path.parent / DEFAULT_REGISTRY.name,
        returning=returning,
        match_settings=MatchSettings.from_settings(settings),
    )


# --- Предпросмотр --------------------------------------------------------------


def format_preview(record: CatalogImport, issues: list[Issue]) -> str:
    """Текст предпросмотра: только реальные числа импорта."""
    summary = record.summary
    lines = [
        f"Импорт {record.id} — {record.status}",
        f"Файл: {record.file.filename}, {megabytes(record.file.size)} МБ, "
        f"sha256 {record.file.checksum[:16]}…, загрузил {record.uploaded_by}, {record.created_at}",
    ]
    if record.duplicate:
        lines.append("Этот файл уже загружали: показан прежний импорт, новых записей нет.")

    if record.status is ImportStatus.UPLOADED:
        lines.append(
            f"Разбор не завершён: {record.error or 'причина не записана'}. "
            "Повторная загрузка того же файла разберёт его заново."
        )
    elif record.status is ImportStatus.INVALID:
        lines.append("Файл непригоден для импорта:")
        lines += [f"  • {issue.message}" for issue in issues if issue.severity is Severity.ERROR]
    else:
        lines += [
            f"Строк на листе товаров: {_n(summary.rows_total)} — разделов {_n(summary.headings)}, "
            f"строк товаров {_n(summary.product_rows)}",
            f"Товаров (кодов 1С): {_n(summary.products)}, "
            f"в нескольких разделах: {_n(summary.cross_listed)}",
            f"Принято в импорт: {_n(summary.accepted)}, "
            f"исключено из-за ошибок: {_n(summary.rejected)}",
            f"Ошибок: {_n(summary.errors)}, предупреждений: {_n(summary.warnings)}",
        ]
        lines += [
            f"  • {ISSUE_LABELS.get(code, code)}: {_n(count)}"
            for code, count in summary.issues_by_code.items()
        ]
        comparison = summary.comparison
        if comparison is None:
            lines.append("Сравнение с текущим каталогом: база знаний бота не собрана.")
        else:
            lines.append(
                f"Сравнение с текущим каталогом по коду 1С: есть в каталоге "
                f"{_n(comparison.in_catalog)}, новых {_n(comparison.new)}, "
                f"нет в файле {_n(comparison.missing_from_file)}"
            )
            lines += _matching_lines(comparison)
        lines += _diff_lines(record)
        if issues:
            total = summary.errors + summary.warnings
            shown = f" (показано {len(issues)} из {_n(total)})" if total > len(issues) else ""
            lines.append(f"Проблемы строк{shown}:")
            lines += [f"  {_issue_line(issue)}" for issue in issues]

    if record.status is ImportStatus.APPLIED:
        lines.append(f"Импорт применён: текущая версия каталога — {record.version}.")
    elif record.status is ImportStatus.APPROVED:
        lines.append(
            f"Импорт утверждён, версия {record.version} ещё не стала текущей: "
            "любая команда `run.py catalog` завершит применение."
        )
    else:
        lines.append("Каталог бота не изменён.")
        if record.status in REDIFF_STATUSES and record.diff_fingerprint:
            lines.append(
                f"Изменения по позициям: python run.py import-1c --diff {record.id}; "
                f"утверждение: python run.py catalog approve {record.id}"
            )
    return "\n".join(lines)


def _diff_lines(record: CatalogImport) -> list[str]:
    if record.summary.diff is None:
        return []
    counters = DiffCounters.from_dict(record.summary.diff)
    return [
        f"Diff против {record.base_version}: NEW {_n(counters.new)}, UPDATED {_n(counters.updated)}, "
        f"UNCHANGED {_n(counters.unchanged)}, MISSING {_n(counters.missing)}, "
        f"RECODING {_n(counters.recoding)}, AMBIGUOUS {_n(counters.ambiguous)}",
        f"Цена изменилась у {_n(counters.price_changed)} ({counters.price_changed_share:.1%}), "
        f"остаток — у {_n(counters.stock_changed)}; исчезает {counters.removed_share:.1%} каталога",
        f"Отпечаток diff: {record.diff_fingerprint}",
    ]


def format_imports(records: list[CatalogImport]) -> str:
    if not records:
        return "Импортов пока нет."
    return "\n".join(
        f"{record.id}  {record.status:<8}  {record.file.filename}  "
        f"товаров {_n(record.summary.products)}, ошибок {_n(record.summary.errors)}, "
        f"предупреждений {_n(record.summary.warnings)}  {record.created_at}"
        for record in records
    )


def _matching_lines(comparison: CatalogComparison) -> list[str]:
    """Итог сопоставления: проверка кодов из каталога и поиск перекодировок."""
    if not comparison.matching:
        return ["Сопоставление с каталогом не выполнялось: импорт загружен до его появления."]
    lines = []
    if comparison.existing_by_status:
        lines.append(f"Товары с кодом 1С из каталога: {_statuses(comparison.existing_by_status)}")
    if comparison.recoding_checked:
        lines.append(
            "Возможные перекодировки (новые коды сравнены только с исчезнувшими): "
            f"проверено {_n(comparison.recoding_checked)}, "
            f"с кандидатом {_n(comparison.recoding_candidates)} — "
            f"{_statuses(comparison.recoding_by_status)}"
        )
    else:
        reason = (
            "нет исчезнувших кодов"
            if not comparison.missing_from_file
            else "нет новых товаров, принятых в импорт"
        )
        lines.append(f"Возможные перекодировки: 0 — {reason}.")
    return lines


def _statuses(counts: dict[str, int]) -> str:
    return ", ".join(
        f"{MATCH_STATUS_SHORT_LABELS.get(code, code)} {_n(count)}" for code, count in counts.items()
    )


def _issue_line(issue: Issue) -> str:
    parts = ["ОШИБКА" if issue.severity is Severity.ERROR else "оговорка"]
    if issue.sheet != 1:
        parts.append(f"лист {issue.sheet}")
    if issue.row_number is not None:
        parts.append(f"строка {issue.row_number}")
    if issue.column:
        parts.append(f"колонка {issue.column}")
    if issue.sku_1c:
        parts.append(f"код {issue.sku_1c}")
    return f"{', '.join(parts)}: {issue.message}"


def _summary(
    sheet: ParsedSheet, issues: list[Issue], accepted: int = 0, rejected: int = 0
) -> ImportSummary:
    coded = [product for product in sheet.products if product.sku_1c]
    severities = Counter(issue.severity for issue in issues)
    return ImportSummary(
        rows_total=sheet.rows_total,
        headings=len(sheet.headings),
        product_rows=len(sheet.product_rows),
        products=len({product.sku_1c for product in coded}),
        cross_listed=sum(len(product.category_paths) > 1 for product in coded),
        accepted=accepted,
        rejected=rejected,
        errors=severities[Severity.ERROR],
        warnings=severities[Severity.WARNING],
        issues_by_code=dict(Counter(issue.code for issue in issues).most_common()),
    )


def _invalid(issue: Issue) -> Inspection:
    return Inspection(
        ImportStatus.INVALID,
        ImportSummary(errors=1, issues_by_code={issue.code: 1}),
        issues=[issue],
    )


def _n(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
