"""EPIC 3, шаг 2: сопоставление товаров импорта 1С с каталогом бота.

Состояние позиции — есть в каталоге, новая, исчезнувшая — решает импорт по точному
коду 1С. Сопоставление только проверяет пару «код → товар» и ищет перекодировку
среди исчезнувших кодов — никогда по всему каталогу. Каталог синтетический,
настоящие data/kb и базы не читаются.
"""

from __future__ import annotations

import pytest

from catalog.matcher import CatalogMatcher, MatchInput, MatchMethod, MatchStatus, Reason
from catalog.models import Product
from catalog.repository import InMemoryCatalogRepository
from catalog.search import CatalogIndex
from catalog_import.files import FileStore
from catalog_import.matching import CODE_STATE_LABELS, CodeState, compare_with_catalog
from catalog_import.models import (
    CatalogComparison,
    CatalogImport,
    ImportItem,
    ImportStatus,
    ImportSummary,
    StoredFile,
)
from catalog_import.repository import SqliteImportRepository
from catalog_import.service import CatalogImportService, format_preview
from test_catalog_import import NOW, VALID_BITRIX, VALID_ROWS, write_xlsx

MATCH_FIELDS = {
    "state",
    "old_article",
    "new_article",
    "old_name",
    "new_name",
    "match_status",
    "match_method",
    "confidence",
    "matched_product_id",
    "candidates",
    "reason_codes",
}


class _WholeCatalog(dict):
    """Триграммный индекс всего каталога: любое обращение к нему — полный перебор."""

    def get(self, *_args):
        raise AssertionError("сопоставление перебирает весь каталог")

    def __getitem__(self, _key):
        raise AssertionError("сопоставление перебирает весь каталог")


class SpyMatcher(CatalogMatcher):
    """Настоящее сопоставление, которое запоминает, как и где оно искало."""

    def __init__(self, repository, settings=None):
        super().__init__(repository, settings)
        self._postings = _WholeCatalog(self._postings)
        self.calls: list[tuple[str | None, frozenset[str] | None]] = []
        self.pools: list[frozenset[str] | None] = []

    def match(self, item, *, among=None):
        self.calls.append((item.article_1c, None if among is None else frozenset(among)))
        return super().match(item, among=among)

    def _similar(self, query, pool):
        self.pools.append(pool)
        return super()._similar(query, pool)


def catalog(*products: tuple[str, str], inactive: tuple[str, ...] = ()):
    return InMemoryCatalogRepository(
        CatalogIndex(
            [
                Product.from_dict({"sku_1c": code, "name": name, "is_active": code not in inactive})
                for code, name in products
            ]
        )
    )


def item(code: str, name: str) -> ImportItem:
    return ImportItem(
        sku_1c=code,
        name=name,
        price=None,
        stock=None,
        rows=[2],
        payload={"sku_1c": code, "name": name},
    )


def compare(repo, *items: ImportItem):
    matcher = SpyMatcher(repo)
    return compare_with_catalog(items, [i.sku_1c for i in items], repo, matcher), matcher


# --- Случаи из постановки ------------------------------------------------------


def test_existing_code_is_checked_only_against_its_product():
    repo = catalog(("100", "Мяч резиновый 60 см"), ("101", "Мяч резиновый 80 см"))

    result, matcher = compare(
        repo, item("100", "Мяч резиновый 60 см"), item("101", "Мяч резиновый 80 см")
    )

    assert [position.state for position in result.existing] == [CodeState.EXISTING] * 2
    assert [
        (position.old_article, position.new_article, position.match.product_id)
        for position in result.existing
    ] == [("100", "100", "100"), ("101", "101", "101")]
    assert {position.match.status for position in result.existing} == {MatchStatus.MATCHED_EXACT}
    assert {position.match.method for position in result.existing} == {MatchMethod.CODE_1C}
    # Код проверяется сам по себе: поиска по названию нет ни по каталогу, ни по кандидатам.
    assert matcher.calls == [("100", frozenset()), ("101", frozenset())]
    assert matcher.pools == []
    assert result.new == result.missing == ()


def test_new_code_is_new_not_a_match_status():
    result, matcher = compare(catalog(), item("200", "Скакалка"))

    [new] = result.new
    assert (new.state, new.old_article, new.new_article, new.match) == (
        CodeState.NEW,
        None,
        "200",
        None,
    )
    assert new.to_dict()["state"] == "NEW" and new.to_dict()["match_status"] is None
    assert "NEW" not in {str(status) for status in MatchStatus}
    # Исчезнувших кодов нет — сопоставление не вызывается вовсе.
    assert matcher.calls == []
    comparison = result.comparison
    assert (comparison.in_catalog, comparison.new, comparison.missing_from_file) == (0, 1, 0)
    assert (comparison.recoding_checked, comparison.recoding_candidates) == (0, 0)


def test_disappeared_code_is_missing():
    result, matcher = compare(catalog(("300", "Обруч гимнастический")))

    [missing] = result.missing
    assert (missing.state, missing.old_article, missing.old_name) == (
        CodeState.MISSING,
        "300",
        "Обруч гимнастический",
    )
    assert (missing.new_article, missing.new_name, missing.match) == (None, None, None)
    assert "MISSING" not in {str(status) for status in MatchStatus}
    assert matcher.calls == []
    assert result.comparison.missing_from_file == 1


@pytest.mark.parametrize(
    ("new_name", "status", "method"),
    [
        ("Конструктор Бауер", MatchStatus.MATCHED_HIGH, MatchMethod.EXACT_NAME),
        ("Конструктор Бауэр", MatchStatus.MATCHED_REVIEW, MatchMethod.FUZZY_NAME),
    ],
)
def test_recoding_is_searched_only_among_disappeared_codes(new_name, status, method):
    # 400 остался в файле и похож на новое название, но кандидатом быть не может.
    repo = catalog(("100", "Конструктор Бауер"), ("400", "Конструктор Бауер большой"))

    result, matcher = compare(repo, item("400", "Конструктор Бауер большой"), item("200", new_name))

    [new] = result.new
    assert new.state is CodeState.NEW
    assert (new.match.status, new.match.method) == (status, method)
    assert (new.old_article, new.new_article, new.old_name, new.new_name) == (
        "100",
        "200",
        "Конструктор Бауер",
        new_name,
    )
    assert {candidate.product_id for candidate in new.match.candidates} == {"100"}
    assert ("200", frozenset({"100"})) in matcher.calls
    assert set(matcher.pools) <= {frozenset({"100"})}
    assert new.is_recoding_candidate
    assert set(new.to_dict()) == MATCH_FIELDS
    assert new.to_dict()["matched_product_id"] == "100"
    comparison = result.comparison
    assert (comparison.recoding_checked, comparison.recoding_candidates) == (1, 1)
    assert comparison.recoding_by_status == {str(status): 1}


def test_new_product_is_not_matched_to_product_still_in_file():
    repo = catalog(("100", "Конструктор Бауер"), ("300", "Скакалка"))

    # Название нового товара в точности совпадает с товаром 100, но 100 из файла не исчез.
    result, matcher = compare(
        repo, item("100", "Конструктор Бауер"), item("200", "Конструктор Бауер")
    )

    [new] = result.new
    assert new.match.status is MatchStatus.NOT_FOUND
    assert (new.match.product_id, new.old_article) == (None, None)
    assert all(candidate.product_id != "100" for candidate in new.match.candidates)
    assert ("200", frozenset({"300"})) in matcher.calls
    assert not new.is_recoding_candidate
    assert result.comparison.recoding_candidates == 0


def test_existing_code_with_changed_digits_requires_review():
    repo = catalog(("100", "Мяч 60 см"), ("101", "Мяч 80 см"))

    result, matcher = compare(repo, item("100", "Мяч 80 см"), item("101", "Мяч 80 см"))

    ball = result.existing[0]
    # Код не переезжает на 101 с тем же названием: проверка, а не выбор.
    assert (ball.match.status, ball.match.product_id) == (MatchStatus.MATCHED_REVIEW, "100")
    assert Reason.DIGITS_MISMATCH in ball.match.reason_codes
    assert "Требуется проверка менеджера" in ball.message
    assert matcher.pools == []


def test_existing_code_with_same_name_is_exact():
    result, _ = compare(catalog(("100", "Мяч 60 см")), item("100", "Мяч 60 см"))

    assert result.existing[0].match.status is MatchStatus.MATCHED_EXACT


# --- Отсутствие полного перебора -------------------------------------------------


def test_without_disappeared_codes_new_items_are_not_matched():
    repo = catalog(("1", "Мяч"), ("2", "Обруч"), ("3", "Скакалка"))

    result, matcher = compare(
        repo,
        item("1", "Мяч"),
        item("2", "Обруч"),
        item("3", "Скакалка"),
        item("4", "Мяч большой"),
        item("5", "Обруч малый"),
    )

    assert [position.state for position in result.new] == [CodeState.NEW] * 2
    assert all(position.match is None for position in result.new)
    assert matcher.calls == [("1", frozenset()), ("2", frozenset()), ("3", frozenset())]
    assert matcher.pools == []
    assert (result.comparison.recoding_checked, result.comparison.recoding_candidates) == (0, 0)


def test_spy_detects_full_catalog_scan():
    """Проверка самой защиты: поиск без `among` идёт по всему каталогу и ловится."""
    matcher = SpyMatcher(catalog(("1", "Мяч резиновый")))

    with pytest.raises(AssertionError, match="весь каталог"):
        matcher.match(MatchInput("Мяч резиновый большой"))


# --- Правила шага 1 сохраняются ---------------------------------------------------


def test_inactive_code_keeps_code_inactive_reason():
    repo = catalog(("100", "Мяч резиновый"), ("300", "Обруч"), inactive=("100",))

    result, matcher = compare(repo, item("100", "Мяч резиновый"))

    [new] = result.new
    assert new.state is CodeState.NEW
    assert new.match.reason_codes[0] is Reason.CODE_INACTIVE
    assert new.match.status is MatchStatus.NOT_FOUND
    assert matcher.calls == [("100", frozenset({"300"}))]
    assert [position.old_article for position in result.missing] == ["300"]


def test_states_and_messages_are_russian():
    assert CODE_STATE_LABELS == {
        CodeState.EXISTING: "Товар есть в каталоге",
        CodeState.NEW: "Новый товар",
        CodeState.MISSING: "Товар отсутствует в новом файле 1С",
    }
    repo = catalog(("100", "Мяч 60 см"), ("300", "Обруч"))

    result, _ = compare(repo, item("100", "Мяч 80 см"), item("200", "Скакалка"))

    [existing], [new], [missing] = result.existing, result.new, result.missing
    assert existing.message.startswith("Товар есть в каталоге. Требуется проверка менеджера.")
    assert new.message == "Новый товар. Среди исчезнувших кодов подходящего товара нет."
    assert missing.message == "Товар отсутствует в новом файле 1С."
    assert [p.to_dict()["state"] for p in (existing, new, missing)] == [
        "EXISTING",
        "NEW",
        "MISSING",
    ]
    assert existing.to_dict()["match_status"] == "MATCHED_REVIEW"


# --- Через сервис импорта ---------------------------------------------------------


@pytest.fixture
def import_repository(tmp_path):
    repo = SqliteImportRepository(tmp_path / "catalog.sqlite3")
    yield repo
    repo.close()


@pytest.fixture
def similarity_pools(monkeypatch):
    """Каждый поиск по похожести во время импорта: `None` — по всему каталогу."""
    pools: list[frozenset[str] | None] = []
    real = CatalogMatcher._similar

    def spy(self, query, pool):
        pools.append(pool)
        return real(self, query, pool)

    monkeypatch.setattr(CatalogMatcher, "_similar", spy)
    return pools


def upload(import_repository, tmp_path, repo):
    service = CatalogImportService(
        import_repository, FileStore(tmp_path / "uploads"), clock=lambda: NOW, catalog=lambda: repo
    )
    path = write_xlsx(tmp_path / "Pricelist20260912.xlsx", [VALID_ROWS, VALID_BITRIX])
    return service, service.upload(path, uploaded_by="manager")


def test_import_of_unchanged_catalog_searches_nothing(
    import_repository, tmp_path, similarity_pools
):
    repo = catalog(("S1", "Мяч резиновый"), ("S2", "Обруч 60 см"), ("S3", "Скакалка"))

    service, record = upload(import_repository, tmp_path, repo)

    summary = record.summary
    assert (summary.accepted, summary.rejected, summary.warnings) == (3, 0, 2)
    assert summary.comparison == CatalogComparison(
        in_catalog=3,
        new=0,
        missing_from_file=0,
        matching=True,
        existing_by_status={"MATCHED_EXACT": 3},
    )
    assert similarity_pools == []
    assert service.get(record.id).summary == summary
    preview = format_preview(record, [])
    assert "Товары с кодом 1С из каталога: совпадают по коду и названию 3" in preview
    assert "Возможные перекодировки: 0 — нет исчезнувших кодов." in preview


def test_import_searches_recoding_only_among_disappeared(
    import_repository, tmp_path, similarity_pools
):
    # В файле S1, S2, S3. В каталоге S2 записан под старым кодом OLD2.
    repo = catalog(("S1", "Мяч резиновый"), ("OLD2", "Обруч 60 см"))

    _, record = upload(import_repository, tmp_path, repo)

    assert record.summary.comparison == CatalogComparison(
        in_catalog=1,
        new=2,
        missing_from_file=1,
        matching=True,
        existing_by_status={"MATCHED_EXACT": 1},
        recoding_checked=2,
        recoding_candidates=1,
        recoding_by_status={"MATCHED_HIGH": 1, "NOT_FOUND": 1},
    )
    assert similarity_pools == [frozenset({"OLD2"})]
    assert (
        "Возможные перекодировки (новые коды сравнены только с исчезнувшими): проверено 2, "
        "с кандидатом 1 — уверенное совпадение 1, кандидата нет 1"
    ) in format_preview(record, [])


def test_summary_saved_before_matching_still_loads():
    summary = ImportSummary.from_dict(
        {"products": 1, "comparison": {"in_catalog": 1, "new": 0, "missing_from_file": 0}}
    )
    assert summary.comparison == CatalogComparison(in_catalog=1, new=0, missing_from_file=0)

    record = CatalogImport(
        id="2026-09-12-001",
        status=ImportStatus.PARSED,
        file=StoredFile("f1", "a.xlsx", "xlsx", 1, "0" * 64, "a.xlsx", "manager", NOW),
        uploaded_by="manager",
        created_at=NOW,
        summary=summary,
    )
    assert "Сопоставление с каталогом не выполнялось" in format_preview(record, [])
