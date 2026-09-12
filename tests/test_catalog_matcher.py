"""EPIC 3: сопоставление позиции с каталогом — `catalog/matcher.py`.

Каталог синтетический, реальные данные не читаются. Пары вроде «Ворон / Ворона»,
«X EDU / X EDU+», «1.14.3.3.1 / 1.14.4.3.1» взяты из того, что нашлось в настоящей
выгрузке от 26.08.2026: именно на них похожесть названия ошибается.
Интеграция с импортом 1С проверяется отдельно, не здесь.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from catalog import text
from catalog.matcher import (
    MAX_CANDIDATES,
    REASON_LABELS,
    STATUS_LABELS,
    CatalogMatcher,
    MatchInput,
    MatchMethod,
    MatchSettings,
    MatchStatus,
    Reason,
    canonical_name,
)
from catalog.models import Product
from catalog.repository import InMemoryCatalogRepository
from catalog.search import CatalogIndex
from catalog_import import parser
from core.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def product(sku: str, name: str, **attributes: str) -> Product:
    return Product.from_dict({"sku_1c": sku, "name": name, "attributes": attributes})


CATALOG = [
    product("1001", "Игрушка мягкая Ворон"),
    product("1002", "Игрушка мягкая Медведь"),
    product(
        "1003", "Конструктор магнитный большой набор для детского сада", **{"Артикул": "KM-500"}
    ),
    product("1004", "Обруч гимнастический плоский пластиковый 60 см"),
    product("1005", "Набор по робототехнике X EDU+"),
    product("1006", "Кубик деревянный А"),
    product("1007", "Кубик деревянный Б"),
    product("1008", "Мяч резиновый «Шарик» 8,5 см 40 х 60"),
    product("1009", "Пирамидка напольная большая", **{"Артикул": "LS-102", "Бренд": "Элти"}),
    product("1010", "Счётные палочки", **{"Артикул": "065"}),
    product("1011", "Мозаика напольная", **{"Артикул": "065"}),
    product("1012", "1.14.3.3.1 Горка детская"),
]

TYPO = "Конструктор магнитный большой набор для детксого сада"


def repository(products: list[Product] = CATALOG) -> InMemoryCatalogRepository:
    return InMemoryCatalogRepository(CatalogIndex(products))


@pytest.fixture(scope="module")
def catalog() -> InMemoryCatalogRepository:
    return repository()


def matcher(repo: InMemoryCatalogRepository, **settings: object) -> CatalogMatcher:
    return CatalogMatcher(repo, MatchSettings(**settings))


# --- код 1С -----------------------------------------------------------------------


def test_exact_article(catalog):
    result = matcher(catalog).match(MatchInput(name="Игрушка мягкая Ворон", article_1c="1001"))

    assert result.status is MatchStatus.MATCHED_EXACT
    assert result.method is MatchMethod.CODE_1C
    assert result.product_id == "1001"
    assert result.confidence == 1.0
    assert result.reason_codes == (Reason.CODE_MATCH, Reason.NAME_EXACT_MATCH, Reason.DIGITS_MATCH)


def test_code_with_compatible_rename_is_exact(catalog):
    result = matcher(catalog).match(
        MatchInput(name="Игрушка мягкая Ворон серая", article_1c="1001")
    )

    assert result.status is MatchStatus.MATCHED_EXACT
    assert Reason.NAME_SIMILAR in result.reason_codes


def test_code_without_name_is_exact(catalog):
    result = matcher(catalog).match(MatchInput(name="", article_1c="1001"))

    assert result.status is MatchStatus.MATCHED_EXACT
    assert result.reason_codes == (Reason.CODE_MATCH, Reason.NAME_NOT_PROVIDED)


def test_same_article_different_name_requires_review(catalog):
    result = matcher(catalog).match(MatchInput(name="Телескоп астрономический", article_1c="1001"))

    assert result.status is MatchStatus.MATCHED_REVIEW
    assert result.product_id == "1001"
    assert result.confidence < 0.60
    assert Reason.CODE_MATCH in result.reason_codes
    assert Reason.NAME_DIFFERS in result.reason_codes


# --- название ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Игрушка мягкая Ворон", "игрушка   МЯГКАЯ\xa0ворон "])
def test_exact_name(catalog, name):
    result = matcher(catalog).match(MatchInput(name=name))

    assert result.status is MatchStatus.MATCHED_HIGH
    assert result.method is MatchMethod.EXACT_NAME
    assert result.product_id == "1001"
    assert result.reason_codes[:2] == (Reason.NAME_EXACT_MATCH, Reason.SINGLE_CANDIDATE)


@pytest.mark.parametrize(
    ("name", "sku"),
    [
        ('мяч  резиновый "шарик" 8.5см 40x60', "1008"),
        ("Мяч резиновый Шарик 8,5 см 40 × 60", "1008"),
        ("МЯЧ РЕЗИНОВЫЙ «ШАРИК» 8,5 СМ 40*60", "1008"),
        ("Счетные палочки", "1010"),
    ],
)
def test_normalized_name(catalog, name, sku):
    result = matcher(catalog).match(MatchInput(name=name))

    assert result.status is MatchStatus.MATCHED_HIGH
    assert result.method is MatchMethod.NORMALIZED_NAME
    assert result.product_id == sku
    assert result.confidence == 1.0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Мяч 4х5х6 см", "мяч 4 x 5 × 6см"),
        ("Мяч 8,5 см", "мяч 8.5см"),
        ("Набор «Шарик»", 'набор "шарик"'),
        ("Ёлка", "елка"),
        ("Пособие № 5", "пособие №5"),
    ],
)
def test_normalization_ignores_only_spelling(left, right):
    assert canonical_name(left) == canonical_name(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Набор X EDU", "Набор X EDU+"),
        ("1.14.3.3.1 Горка", "1.14.4.3.1 Горка"),
        ("Обруч 60 см", "Обруч 80 см"),
        ("Стол EKUD 0823Б", "Стол EKUD 0823С"),
        ("_ОР Мяч", "ОР Мяч"),
        ("БОС Обруч", "Обруч"),
    ],
)
def test_normalization_keeps_significant_differences(left, right):
    assert canonical_name(left) != canonical_name(right)


def test_normalize_name_has_one_implementation():
    assert parser.normalize_name is text.normalize_name


# --- похожее название ---------------------------------------------------------


def test_high_confidence(catalog):
    result = matcher(catalog, auto_enabled=True).match(MatchInput(name=TYPO))

    assert result.status is MatchStatus.MATCHED_HIGH
    assert result.method is MatchMethod.FUZZY_NAME
    assert result.product_id == "1003"
    assert result.confidence >= 0.85
    assert result.reason_codes == (Reason.NAME_SIMILAR, Reason.DIGITS_MATCH)


def test_similar_name_is_review_when_auto_disabled(catalog):
    assert MatchSettings().auto_enabled is False
    result = matcher(catalog).match(MatchInput(name=TYPO))

    assert result.status is MatchStatus.MATCHED_REVIEW
    assert result.product_id == "1003"
    assert result.confidence >= 0.85
    assert Reason.AUTO_MATCH_DISABLED in result.reason_codes


def test_similar_name_below_auto_threshold_is_review(catalog):
    result = matcher(catalog, auto_enabled=True, auto_threshold=0.95).match(MatchInput(name=TYPO))

    assert result.status is MatchStatus.MATCHED_REVIEW
    assert Reason.BELOW_AUTO_THRESHOLD in result.reason_codes


@pytest.mark.parametrize("auto", [False, True])
def test_ambiguous(catalog, auto):
    result = matcher(catalog, auto_enabled=auto).match(MatchInput(name="Кубик деревянный"))

    assert result.status is MatchStatus.AMBIGUOUS
    assert result.matched_product is None
    assert {c.product_id for c in result.candidates[:2]} == {"1006", "1007"}
    assert Reason.CLOSE_SECOND_CANDIDATE in result.reason_codes


def test_ambiguity_margin_is_configurable(catalog):
    result = matcher(catalog, ambiguity_margin=0.0).match(MatchInput(name="Кубик деревянный"))

    assert result.status is MatchStatus.MATCHED_REVIEW


def test_same_name_twice_is_ambiguous():
    twins = repository([product("1", "Мяч резиновый"), product("2", "мяч  резиновый")])
    result = matcher(twins).match(MatchInput(name="Мяч резиновый"))

    assert result.status is MatchStatus.AMBIGUOUS
    assert result.method is MatchMethod.EXACT_NAME
    assert Reason.MULTIPLE_CANDIDATES in result.reason_codes


def test_not_found(catalog):
    result = matcher(catalog).match(MatchInput(name="Телескоп астрономический"))

    assert result.status is MatchStatus.NOT_FOUND
    assert result.matched_product is None
    assert result.confidence < 0.60
    assert result.reason_codes == (Reason.LOW_SIMILARITY,)


def test_unknown_code_is_searched_by_name(catalog):
    result = matcher(catalog).match(MatchInput(name="Конструктор магнитный", article_1c="9999"))

    assert result.status is MatchStatus.NOT_FOUND
    assert result.reason_codes[0] is Reason.CODE_NOT_IN_CATALOG

    lowered = matcher(catalog, review_threshold=0.4).match(
        MatchInput(name="Конструктор магнитный", article_1c="9999")
    )
    assert lowered.status is MatchStatus.MATCHED_REVIEW
    assert lowered.product_id == "1003"


# --- защитные проверки ---------------------------------------------------------


def test_digits_must_match(catalog):
    auto = matcher(catalog, auto_enabled=True)
    hoop = auto.match(MatchInput(name="Обруч гимнастический плоский пластиковый 80 см"))

    assert hoop.status is MatchStatus.MATCHED_REVIEW
    # Похожесть выше порога автовыбора: товар не выбран именно из-за чисел.
    assert hoop.confidence >= 0.85
    assert Reason.DIGITS_MISMATCH in hoop.reason_codes

    norm_code = auto.match(MatchInput(name="1.14.4.3.1 Горка детская"))
    assert norm_code.status is MatchStatus.MATCHED_REVIEW
    assert Reason.DIGITS_MISMATCH in norm_code.reason_codes

    by_code = auto.match(
        MatchInput(name="Обруч гимнастический плоский пластиковый 80 см", article_1c="1004")
    )
    assert by_code.status is MatchStatus.MATCHED_REVIEW
    assert Reason.DIGITS_MISMATCH in by_code.reason_codes


def test_plus_is_significant(catalog):
    result = matcher(catalog, auto_enabled=True).match(
        MatchInput(name="Набор по робототехнике X EDU")
    )

    assert result.status is MatchStatus.MATCHED_REVIEW
    assert result.confidence >= 0.85
    assert Reason.WORDS_CONFLICT in result.reason_codes

    both = repository([product("1", "Набор X EDU"), product("2", "Набор X EDU+")])
    assert matcher(both).match(MatchInput(name="набор x edu+")).product_id == "2"
    assert matcher(both).match(MatchInput(name="набор x edu")).product_id == "1"


# Классы из настоящей выгрузки: «(белочка+песик)», «песочница +интерактивный стол»,
# «(гимнастерка +пилотка+юбка)». До исправления пропавший «+» проходил проверку слов
# и при включённом автовыборе давал MATCHED_HIGH (8 случаев на каталоге 26.08).
PLUS_CASES = {
    "плюс приклеен к словам": (
        "Формочки для песка «Зоопарк» (белочка+песик) набор из двух штук",
        "Формочки для песка «Зоопарк» (белочкапесик) набор из двух штук",
    ),
    "плюс в начале слова": (
        "Комплект «Интерактивная песочница +интерактивный стол»",
        "Комплект «Интерактивная песочница интерактивный стол»",
    ),
    "плюс внутри составного названия": (
        "Костюм карнавальный «Солдат» детский для девочки (гимнастерка+пилотка+юбка)",
        "Костюм карнавальный «Солдат» детский для девочки (гимнастерка пилотка юбка)",
    ),
    "плюс добавлен": (
        "Набор по робототехнике для начальной школы X EDU",
        "Набор по робототехнике для начальной школы X EDU+",
    ),
}


@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("case", PLUS_CASES, ids=list(PLUS_CASES))
def test_plus_glued_to_words_is_significant(case, auto):
    catalog_name, query = PLUS_CASES[case]
    m = matcher(
        repository([product("1", catalog_name), product("2", "Мяч резиновый")]), auto_enabled=auto
    )

    result = m.match(MatchInput(name=query))
    assert result.status is MatchStatus.MATCHED_REVIEW
    assert result.product_id == "1"
    # Сходство выше порога автовыбора: товар не выбран именно из-за «+».
    assert result.confidence >= 0.85
    assert Reason.PLUS_MISMATCH in result.reason_codes

    by_code = m.match(MatchInput(name=query, article_1c="1"))
    assert by_code.status is MatchStatus.MATCHED_REVIEW
    assert Reason.PLUS_MISMATCH in by_code.reason_codes


# Формы слова, прошедшие проверку до исправления (каталог 26.08): «к человеку» →
# «к человекуа» — беглая гласная в `stem`; «+пилотка+» → «+пилотк+» — слово было
# склеено с «+». Плюс контрольные «Ворон/Ворона» и «Медведь/Медведица».
WORD_FORM_CASES = {
    "беглая гласная": (
        "Демонстрационные таблицы «Растения по отношению к человеку» комплект из 10 листов",
        "Демонстрационные таблицы «Растения по отношению к человекуа» комплект из 10 листов",
    ),
    "слово внутри «+»": (
        "Костюм карнавальный «Солдат» детский для девочки (гимнастерка+пилотка+юбка)",
        "Костюм карнавальный «Солдат» детский для девочки (гимнастерка+пилотк+юбка)",
    ),
    "окончание в конце названия": (
        "Шапочка театральная для детского спектакля «Ворон»",
        "Шапочка театральная для детского спектакля «Ворона»",
    ),
    "суффикс": (
        "Игрушка мягкая Медведь коричневый большой для детского сада",
        "Игрушка мягкая Медведица коричневый большой для детского сада",
    ),
}


@pytest.mark.parametrize("case", WORD_FORM_CASES, ids=list(WORD_FORM_CASES))
def test_word_forms_block_auto_match(case):
    catalog_name, query = WORD_FORM_CASES[case]
    m = matcher(
        repository([product("1", catalog_name), product("2", "Мяч резиновый")]), auto_enabled=True
    )

    result = m.match(MatchInput(name=query))

    assert result.status is MatchStatus.MATCHED_REVIEW
    assert result.product_id == "1"
    # Сходство выше порога автовыбора: остановила проверка слов.
    assert result.confidence >= 0.85
    assert Reason.WORDS_CONFLICT in result.reason_codes


def test_word_forms_do_not_confirm_supplier_article():
    catalog_name, query = WORD_FORM_CASES["беглая гласная"]
    art = repository([product("1", catalog_name, **{"Артикул": "DT-10"})])

    result = matcher(art).match(MatchInput(name=query, supplier_article="DT-10"))

    assert result.status is MatchStatus.MATCHED_REVIEW
    assert Reason.NAME_NOT_CONFIRMED in result.reason_codes


def test_spaces_around_plus_are_spelling():
    assert canonical_name("Набор (весы + касса)") == canonical_name("набор (весы+касса)")
    assert canonical_name("Набор (весы+касса)") != canonical_name("Набор (весы касса)")


@pytest.mark.parametrize("auto", [False, True])
def test_voron_vs_vorona(catalog, auto):
    m = matcher(catalog, auto_enabled=auto)
    vorona = m.match(MatchInput(name="Игрушка мягкая Ворона"))

    assert vorona.status is MatchStatus.MATCHED_REVIEW
    # Похожесть выше порога автовыбора — остановила проверка слов, а не порог.
    assert vorona.confidence >= 0.85
    assert Reason.WORDS_CONFLICT in vorona.reason_codes

    she_bear = m.match(MatchInput(name="Игрушка мягкая Медведица"))
    assert she_bear.status is MatchStatus.MATCHED_REVIEW

    both = repository([product("1", "Игрушка мягкая Ворон"), product("2", "Игрушка мягкая Ворона")])
    assert (
        matcher(both, auto_enabled=auto).match(MatchInput(name="Игрушка мягкая Ворона")).product_id
        == "2"
    )


def test_supplier_article_with_confirming_name(catalog):
    # Автовыбор выключен: одно похожее название дало бы MATCHED_REVIEW.
    result = matcher(catalog).match(MatchInput(name=TYPO, supplier_article="km 500"))

    assert result.status is MatchStatus.MATCHED_HIGH
    assert result.method is MatchMethod.SUPPLIER_ARTICLE
    assert result.product_id == "1003"
    assert result.reason_codes[:3] == (
        Reason.SUPPLIER_ARTICLE_MATCH,
        Reason.NAME_CONFIRMS,
        Reason.SINGLE_CANDIDATE,
    )


def test_supplier_article_without_confirmation(catalog):
    m = matcher(catalog, auto_enabled=True)

    other_name = m.match(MatchInput(name="Горка для улицы", supplier_article="LS-102"))
    assert other_name.status is MatchStatus.MATCHED_REVIEW
    assert other_name.product_id == "1009"
    assert Reason.NAME_NOT_CONFIRMED in other_name.reason_codes

    no_name = m.match(MatchInput(name="", supplier_article="LS-102"))
    assert no_name.status is MatchStatus.MATCHED_REVIEW
    assert Reason.NAME_NOT_PROVIDED in no_name.reason_codes

    shared = m.match(MatchInput(name="Палочки", supplier_article="065"))
    assert shared.status is MatchStatus.AMBIGUOUS
    assert Reason.SUPPLIER_ARTICLE_NOT_UNIQUE in shared.reason_codes


def test_manufacturer_conflict_requires_review(catalog):
    m = matcher(catalog, auto_enabled=True)
    name = "Пирамидка напольная большая"

    conflict = m.match(MatchInput(name=name, manufacturer="Другой завод"))
    assert conflict.status is MatchStatus.MATCHED_REVIEW
    assert Reason.MANUFACTURER_CONFLICT in conflict.reason_codes

    by_code = m.match(MatchInput(name=name, article_1c="1009", manufacturer="Другой завод"))
    assert by_code.status is MatchStatus.MATCHED_REVIEW

    same = m.match(MatchInput(name=name, manufacturer="ЭЛТИ"))
    assert same.status is MatchStatus.MATCHED_HIGH

    unknown = m.match(MatchInput(name="Счётные палочки", manufacturer="Другой завод"))
    assert unknown.status is MatchStatus.MATCHED_HIGH  # у товара производитель не указан


def test_among_limits_name_search_but_not_code(catalog):
    m = matcher(catalog)

    by_code = m.match(MatchInput(name="Игрушка мягкая Ворон", article_1c="1001"), among=[])
    assert by_code.status is MatchStatus.MATCHED_EXACT

    outside = m.match(MatchInput(name="Игрушка мягкая Ворон"), among=["1002"])
    assert outside.product_id != "1001"
    assert outside.status is not MatchStatus.MATCHED_HIGH

    narrowed = m.match(MatchInput(name="Кубик деревянный"), among=["1006"])
    assert narrowed.status is MatchStatus.MATCHED_REVIEW
    assert narrowed.product_id == "1006"


# --- контракт результата ----------------------------------------------------------


def test_result_is_machine_readable(catalog):
    result = matcher(catalog).match(MatchInput(name="Кубик деревянный"))
    data = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))

    assert set(data) == {
        "status",
        "method",
        "confidence",
        "product_id",
        "candidates",
        "reason_codes",
    }
    assert data["status"] == "AMBIGUOUS"
    assert data["reason_codes"] == ["NAME_SIMILAR", "CLOSE_SECOND_CANDIDATE"]
    assert 0 < len(data["candidates"]) <= MAX_CANDIDATES
    assert set(data["candidates"][0]) == {"product_id", "article", "name", "score", "reason_codes"}
    assert all(isinstance(code, str) for c in data["candidates"] for code in c["reason_codes"])
    assert set(REASON_LABELS) == set(Reason)


# --- настройки -------------------------------------------------------------------


def test_match_settings_defaults_are_safe():
    settings = Settings()

    assert MatchSettings.from_settings(settings) == MatchSettings()
    assert MatchSettings() == MatchSettings(
        auto_enabled=False, auto_threshold=0.85, review_threshold=0.60, ambiguity_margin=0.05
    )


@pytest.mark.parametrize(
    ("raw", "enabled"),
    [("0", False), ("false", False), ("False", False), ("off", False), ("1", True), ("TRUE", True)],
)
def test_match_auto_enabled_from_env(monkeypatch, raw, enabled):
    monkeypatch.setenv("MATCH_AUTO_ENABLED", raw)
    monkeypatch.setenv("MATCH_AUTO_THRESHOLD", "0.9")
    monkeypatch.setenv("MATCH_REVIEW_THRESHOLD", "0.5")
    monkeypatch.setenv("MATCH_AMBIGUITY_MARGIN", "0.1")

    settings = MatchSettings.from_settings(Settings.from_env())

    assert settings == MatchSettings(enabled, 0.9, 0.5, 0.1)


@pytest.mark.parametrize(
    "values",
    [
        {"auto_threshold": 1.5},
        {"review_threshold": -0.1},
        {"auto_threshold": 0.5, "review_threshold": 0.6},
    ],
)
def test_match_settings_are_validated(values):
    with pytest.raises(ValueError):
        MatchSettings(**values)


def test_env_example_matches_settings():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    example = dict(line.split("=", 1) for line in lines if line.startswith("MATCH_"))
    defaults = Settings()

    assert example == {
        "MATCH_AUTO_ENABLED": "1" if defaults.match_auto_enabled else "0",
        "MATCH_AUTO_THRESHOLD": f"{defaults.match_auto_threshold:.2f}",
        "MATCH_REVIEW_THRESHOLD": f"{defaults.match_review_threshold:.2f}",
        "MATCH_AMBIGUITY_MARGIN": f"{defaults.match_ambiguity_margin:.2f}",
    }


# --- неактивный товар, артикул, одинаковые и короткие названия --------------------


def test_inactive_code_is_not_matched():
    inactive = Product.from_dict(
        {"sku_1c": "9", "name": "Мяч футбольный старый", "is_active": False}
    )
    m = matcher(repository([inactive, product("1", "Мяч футбольный новый")]))

    by_code = m.match(MatchInput(name="Мяч футбольный старый", article_1c="9"))
    assert by_code.status not in (MatchStatus.MATCHED_EXACT, MatchStatus.MATCHED_HIGH)
    assert by_code.product_id != "9"
    assert by_code.reason_codes[0] is Reason.CODE_INACTIVE

    no_name = m.match(MatchInput(name="", article_1c="9"))
    assert no_name.status is MatchStatus.NOT_FOUND
    assert no_name.reason_codes[0] is Reason.CODE_INACTIVE


def test_supplier_article_conflict_requires_review(catalog):
    m = matcher(catalog, auto_enabled=True)
    name = "Пирамидка напольная большая"

    by_name = m.match(MatchInput(name=name, supplier_article="ZZ-999"))
    assert by_name.status is MatchStatus.MATCHED_REVIEW
    assert Reason.SUPPLIER_ARTICLE_CONFLICT in by_name.reason_codes

    by_code = m.match(MatchInput(name=name, article_1c="1009", supplier_article="ZZ-999"))
    assert by_code.status is MatchStatus.MATCHED_REVIEW
    assert Reason.SUPPLIER_ARTICLE_CONFLICT in by_code.reason_codes

    # У товара артикула нет — это не конфликт.
    no_article = m.match(MatchInput(name="Игрушка мягкая Ворон", supplier_article="ZZ-999"))
    assert no_article.status is MatchStatus.MATCHED_HIGH


def test_same_normalized_name_twice_is_ambiguous():
    twins = repository([product("1", "Ёлка «Зимняя» 40 х 60"), product("2", 'елка "зимняя" 40x60')])

    result = matcher(twins, auto_enabled=True).match(MatchInput(name="ЕЛКА ЗИМНЯЯ 40×60"))

    assert result.status is MatchStatus.AMBIGUOUS
    assert result.method is MatchMethod.NORMALIZED_NAME
    assert result.matched_product is None
    assert {c.product_id for c in result.candidates} == {"1", "2"}


def test_short_name_typo():
    # Известное ограничение: у короткого названия одна опечатка роняет сходство
    # ниже порога. Порог ради коротких названий не снижаем.
    m = matcher(
        repository([product("1", "Глобус"), product("2", "Мяч резиновый")]), auto_enabled=True
    )

    no_code = m.match(MatchInput(name="Голбус"))
    assert no_code.status is MatchStatus.NOT_FOUND
    assert no_code.confidence < 0.60
    assert no_code.reason_codes == (Reason.LOW_SIMILARITY,)

    with_code = m.match(MatchInput(name="Голбус", article_1c="1"))
    assert with_code.status is MatchStatus.MATCHED_REVIEW
    assert Reason.NAME_DIFFERS in with_code.reason_codes

    assert m.match(MatchInput(name="глобус", article_1c="1")).status is MatchStatus.MATCHED_EXACT


# --- русские подписи ---------------------------------------------------------------

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
TECHNICAL_CODE = re.compile(r"[A-Z]+_[A-Z]")


def test_status_and_reason_labels_are_russian():
    assert set(STATUS_LABELS) == set(MatchStatus)
    assert set(REASON_LABELS) == set(Reason)
    for label in (*STATUS_LABELS.values(), *REASON_LABELS.values()):
        assert CYRILLIC.search(label), label
        assert not TECHNICAL_CODE.search(label), label


def test_result_messages_are_russian(catalog):
    m = matcher(catalog)

    exact = m.match(MatchInput(name="Игрушка мягкая Ворон", article_1c="1001"))
    assert exact.status_label == "Товар точно сопоставлен по коду 1С."

    review = m.match(MatchInput(name="Игрушка мягкая Ворона"))
    assert review.status_label == "Требуется проверка менеджера."
    assert review.reason_labels[0] == "Название похоже"
    assert review.message.startswith("Требуется проверка менеджера. Причины: название похоже;")
    assert "есть различие в значимых словах" in review.message
    assert review.candidates[0].reason_labels[0] == "Название похоже"

    ambiguous = m.match(MatchInput(name="Кубик деревянный"))
    assert ambiguous.status_label == "Найдено несколько подходящих товаров. Требуется уточнение."

    not_found = m.match(MatchInput(name="Телескоп астрономический"))
    assert not_found.message == (
        "Подходящий товар в текущем каталоге не найден. Причины: недостаточное сходство названий."
    )
    # Машинный контракт подписями не расширяется.
    assert "status_label" not in review.to_dict()
    assert review.to_dict()["reason_codes"][0] == "NAME_SIMILAR"


def test_settings_errors_are_russian():
    with pytest.raises(ValueError, match="MATCH_AUTO_THRESHOLD: ожидается число от 0 до 1"):
        MatchSettings(auto_threshold=1.5)
    with pytest.raises(ValueError, match="не может быть выше"):
        MatchSettings(auto_threshold=0.5, review_threshold=0.6)


# --- архитектура -----------------------------------------------------------------

# Сопоставлению нельзя зависеть от хранилища, модели, каналов, форматов файлов,
# импорта 1С, нормативного слоя и сборки базы знаний.
FORBIDDEN_ROOTS = {
    "sqlite3",
    "catalog_import",
    "ingest",
    "norms",
    "core",
    "orders",
    "agent",
    "adapters",
    "web",
    "media",
    "privacy",
    "observability",
    "telegram",
    "aiogram",
    "httpx",
    "requests",
    "openai",
    "openpyxl",
    "docx",
    "pypdf",
    "fitz",
}


def test_matcher_imports_only_catalog_text():
    source = (ROOT / "src/catalog/matcher.py").read_text(encoding="utf-8")
    runtime = set()
    for node in ast.parse(source).body:  # импорты под TYPE_CHECKING лежат внутри `if`
        if isinstance(node, ast.Import):
            runtime |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            runtime.add(node.module)

    assert {name.split(".")[0] for name in runtime} & FORBIDDEN_ROOTS == set()
    project = {name for name in runtime if name.split(".")[0] not in sys.stdlib_module_names}
    assert project == {"catalog.text"}
    assert not re.search(r"sqlite|execute\(|cursor|SELECT |INSERT ", source)


def test_importing_matcher_loads_no_storage_llm_or_import():
    code = (
        "import sys; import catalog.matcher; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        f"{sorted(FORBIDDEN_ROOTS)!r}))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "[]"


def test_matcher_uses_shared_normalize_name():
    import catalog.matcher as matcher_module

    assert matcher_module.normalize_name is text.normalize_name
    assert parser.normalize_name is text.normalize_name
