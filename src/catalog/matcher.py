"""Сопоставление позиции с каталогом: код 1С, название, артикул поставщика.

Один механизм на всех потребителей: импорт выгрузки 1С (EPIC 3) и загрузка
заказа клиента (EPIC 9). Откуда пришла позиция, сопоставлению неизвестно: на
входе `MatchInput` только с теми признаками, которые у позиции есть.

Сопоставление предлагает, но не решает. `MATCHED_REVIEW` и `AMBIGUOUS` уходят
менеджеру, выбор кандидата — EPIC 5. Модель здесь не участвует (п. 27 ТЗ).

Почему похожее название само по себе товар не выбирает: у 857 из 5 936 товаров
каталога есть другой товар с названием, похожим на 80 % и больше — «Обруч 60 см /
80 см», «Ворон / Ворона». Опечатку от соседнего товара по похожести не отличить,
а ошибка здесь — чужая цена в заказе.
"""

from __future__ import annotations

import heapq
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from catalog.text import normalize_name, stem, trigrams

if TYPE_CHECKING:
    # Только для аннотаций. Сопоставлению нужен любой объект с `list_active` и
    # `get_by_article`, а `catalog.repository` при импорте тянет поиск и
    # нормативный слой.
    from catalog.models import Product
    from catalog.repository import CatalogRepository
    from core.config import Settings

MAX_CANDIDATES = 3


class MatchStatus(StrEnum):
    # Код 1С совпал, название с ним согласуется.
    MATCHED_EXACT = "MATCHED_EXACT"
    # Товар найден без кода, но однозначно: название или артикул с подтверждением.
    MATCHED_HIGH = "MATCHED_HIGH"
    # Предложен товар, но нужен взгляд менеджера.
    MATCHED_REVIEW = "MATCHED_REVIEW"
    # Несколько товаров одинаково близки — не выбираем ни один.
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


class MatchMethod(StrEnum):
    CODE_1C = "code_1c"
    EXACT_NAME = "exact_name"
    NORMALIZED_NAME = "normalized_name"
    SUPPLIER_ARTICLE = "supplier_article"
    FUZZY_NAME = "fuzzy_name"
    NONE = "none"


class Reason(StrEnum):
    CODE_MATCH = "CODE_MATCH"
    CODE_NOT_IN_CATALOG = "CODE_NOT_IN_CATALOG"
    CODE_INACTIVE = "CODE_INACTIVE"
    NAME_NOT_PROVIDED = "NAME_NOT_PROVIDED"
    NAME_EXACT_MATCH = "NAME_EXACT_MATCH"
    NAME_NORMALIZED_MATCH = "NAME_NORMALIZED_MATCH"
    NAME_SIMILAR = "NAME_SIMILAR"
    NAME_DIFFERS = "NAME_DIFFERS"
    DIGITS_MATCH = "DIGITS_MATCH"
    DIGITS_MISMATCH = "DIGITS_MISMATCH"
    PLUS_MISMATCH = "PLUS_MISMATCH"
    WORDS_CONFLICT = "WORDS_CONFLICT"
    MANUFACTURER_CONFLICT = "MANUFACTURER_CONFLICT"
    SUPPLIER_ARTICLE_MATCH = "SUPPLIER_ARTICLE_MATCH"
    SUPPLIER_ARTICLE_CONFLICT = "SUPPLIER_ARTICLE_CONFLICT"
    SUPPLIER_ARTICLE_NOT_UNIQUE = "SUPPLIER_ARTICLE_NOT_UNIQUE"
    NAME_CONFIRMS = "NAME_CONFIRMS"
    NAME_NOT_CONFIRMED = "NAME_NOT_CONFIRMED"
    SINGLE_CANDIDATE = "SINGLE_CANDIDATE"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    CLOSE_SECOND_CANDIDATE = "CLOSE_SECOND_CANDIDATE"
    AUTO_MATCH_DISABLED = "AUTO_MATCH_DISABLED"
    BELOW_AUTO_THRESHOLD = "BELOW_AUTO_THRESHOLD"
    LOW_SIMILARITY = "LOW_SIMILARITY"
    NO_CANDIDATES = "NO_CANDIDATES"


# Всё, что видит менеджер, — на русском. Коды статусов и причин остаются
# машинным контрактом (`MatchResult.to_dict`), подписи — только для показа.
STATUS_LABELS: dict[MatchStatus, str] = {
    MatchStatus.MATCHED_EXACT: "Товар точно сопоставлен по коду 1С.",
    MatchStatus.MATCHED_HIGH: (
        "Товар уверенно сопоставлен без кода 1С: по названию или артикулу поставщика."
    ),
    MatchStatus.MATCHED_REVIEW: "Требуется проверка менеджера.",
    MatchStatus.AMBIGUOUS: "Найдено несколько подходящих товаров. Требуется уточнение.",
    MatchStatus.NOT_FOUND: "Подходящий товар в текущем каталоге не найден.",
}

REASON_LABELS: dict[Reason, str] = {
    Reason.CODE_MATCH: "Совпадает код 1С",
    Reason.CODE_NOT_IN_CATALOG: "Кода 1С нет в каталоге",
    Reason.CODE_INACTIVE: "Товар с этим кодом 1С неактивен в каталоге",
    Reason.NAME_NOT_PROVIDED: "Название не указано",
    Reason.NAME_EXACT_MATCH: "Название совпадает",
    Reason.NAME_NORMALIZED_MATCH: "Название совпадает с точностью до написания",
    Reason.NAME_SIMILAR: "Название похоже",
    Reason.NAME_DIFFERS: "Название существенно отличается",
    Reason.DIGITS_MATCH: "Числовые характеристики совпадают",
    Reason.DIGITS_MISMATCH: "Отличаются числовые характеристики",
    Reason.PLUS_MISMATCH: "Отличается знак «+» в названии",
    Reason.WORDS_CONFLICT: "Есть различие в значимых словах",
    Reason.MANUFACTURER_CONFLICT: "Не совпадает производитель",
    Reason.SUPPLIER_ARTICLE_MATCH: "Совпадает артикул поставщика",
    Reason.SUPPLIER_ARTICLE_CONFLICT: "Не совпадает артикул поставщика",
    Reason.SUPPLIER_ARTICLE_NOT_UNIQUE: "Такой артикул поставщика есть у нескольких товаров",
    Reason.NAME_CONFIRMS: "Название подтверждает совпадение",
    Reason.NAME_NOT_CONFIRMED: "Название не подтверждает совпадение",
    Reason.SINGLE_CANDIDATE: "Подходящий товар один",
    Reason.MULTIPLE_CANDIDATES: "Подходящих товаров несколько",
    Reason.CLOSE_SECOND_CANDIDATE: "Второй кандидат почти так же близок",
    Reason.AUTO_MATCH_DISABLED: "Автоматический выбор по похожему названию выключен",
    Reason.BELOW_AUTO_THRESHOLD: "Сходство ниже порога автоматического выбора",
    Reason.LOW_SIMILARITY: "Недостаточное сходство названий",
    Reason.NO_CANDIDATES: "Кандидатов нет",
}

_ENV_NAMES = {
    "auto_threshold": "MATCH_AUTO_THRESHOLD",
    "review_threshold": "MATCH_REVIEW_THRESHOLD",
    "ambiguity_margin": "MATCH_AMBIGUITY_MARGIN",
}


@dataclass(frozen=True)
class MatchSettings:
    """Пороги — доли общих триграмм названия (коэффициент Жаккара), от 0 до 1."""

    # Автовыбор товара по похожему названию. Выключен: см. докстринг модуля.
    auto_enabled: bool = False
    auto_threshold: float = 0.85
    # Ниже — `NOT_FOUND`. Им же меряется «название существенно отличается» при
    # совпавшем коде 1С.
    review_threshold: float = 0.60
    # Разница с ближайшим соседом, меньше которой лучший кандидат не выбирается.
    # 0.05 — по прогону на каталоге 26.08: сравнение 0.02 / 0.05 / 0.10 в отчёте EPIC 3.
    ambiguity_margin: float = 0.05

    def __post_init__(self) -> None:
        for name, env_name in _ENV_NAMES.items():
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{env_name}: ожидается число от 0 до 1, получено {value}.")
        if self.review_threshold > self.auto_threshold:
            raise ValueError("MATCH_REVIEW_THRESHOLD не может быть выше MATCH_AUTO_THRESHOLD.")

    @classmethod
    def from_settings(cls, settings: Settings) -> MatchSettings:
        return cls(
            auto_enabled=settings.match_auto_enabled,
            auto_threshold=settings.match_auto_threshold,
            review_threshold=settings.match_review_threshold,
            ambiguity_margin=settings.match_ambiguity_margin,
        )


@dataclass(frozen=True)
class MatchInput:
    """Позиция, которую нужно найти в каталоге. Пустое поле — признака нет."""

    name: str
    article_1c: str | None = None
    supplier_article: str | None = None
    manufacturer: str | None = None


def _labels(codes: Iterable[Reason]) -> tuple[str, ...]:
    return tuple(REASON_LABELS[code] for code in codes)


@dataclass(frozen=True)
class MatchCandidate:
    product_id: str
    article: str
    name: str
    score: float
    reason_codes: tuple[Reason, ...]

    @property
    def reason_labels(self) -> tuple[str, ...]:
        return _labels(self.reason_codes)


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    method: MatchMethod
    # Похожесть названия позиции и выбранного товара; 1.0 — совпадение с точностью
    # до записи. У `AMBIGUOUS` и `NOT_FOUND` — похожесть лучшего кандидата.
    confidence: float
    # У `MATCHED_REVIEW` — предложенный, но не подтверждённый товар.
    matched_product: Product | None
    candidates: tuple[MatchCandidate, ...]
    reason_codes: tuple[Reason, ...]

    @property
    def product_id(self) -> str | None:
        return self.matched_product.id if self.matched_product else None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def reason_labels(self) -> tuple[str, ...]:
        return _labels(self.reason_codes)

    @property
    def message(self) -> str:
        """Итог и причины одной фразой — для менеджера и администратора."""
        if not self.reason_codes:
            return self.status_label
        reasons = "; ".join(label[0].lower() + label[1:] for label in self.reason_labels)
        return f"{self.status_label} Причины: {reasons}."

    def to_dict(self) -> dict[str, Any]:
        """Машинный контракт: коды без подписей. Подписи — `status_label`, `reason_labels`."""
        return {
            "status": str(self.status),
            "method": str(self.method),
            "confidence": self.confidence,
            "product_id": self.product_id,
            "candidates": [
                {**asdict(c), "reason_codes": [str(r) for r in c.reason_codes]}
                for c in self.candidates
            ],
            "reason_codes": [str(r) for r in self.reason_codes],
        }


# --- Нормализация -------------------------------------------------------------

_QUOTES = re.compile(r"[\"'«»„“”`]")
# «+» и «.» сюда не входят: «X EDU» и «X EDU+», «1.14.3.3.1» и «1.14.4.3.1» —
# разные товары. «_» тоже: с него начинаются служебные позиции сайта.
_PUNCT = re.compile(r"[,;:!?()\[\]{}/\\|*#–—-]+")
# Вторая цифра не поглощается: иначе в «4х5х6» соседние размеры делят «5» и
# второй разделитель остаётся как был.
_SIZE = re.compile(r"(\d)\s*[xх×*]\s*(?=\d)")
_DECIMAL = re.compile(r"(\d),(\d)")
_NUMBER_SIGN = re.compile(r"№\s*(\d)")
_UNIT = re.compile(r"(\d)(мм|мл|см|кг|шт|м|л|г)\b")
_LONE_DOT = re.compile(r"(?<!\d)\.|\.(?!\d)")
# «весы + касса» и «весы+касса» — одно написание; сам «+» остаётся.
_PLUS = re.compile(r"\s*\+\s*")
_SPACES = re.compile(r"\s+")
_ARTICLE_NOISE = re.compile(r"[\s\-_/\\]+")
# При сравнении слов «+» — отдельная часть: «белочка+песик» — это «белочка», «+»,
# «песик», и пропавший «+» виден как лишнее различие, а не как опечатка.
_WORD_PARTS = re.compile(r"\+|[^\s+]+")

# Слово, которое при одной и той же длине названия всё ещё может быть опечаткой,
# а не другим товаром: не короче этого и не совпадающее с соседним по основе.
_MIN_TYPO_WORD = 5
_MIN_TYPO_SIMILARITY = 0.3


def canonical_name(name: str) -> str:
    """Название без различий записи: регистр, «ё», кавычки, пробелы, «40 х 60».

    Цифры, номера, точки внутри чисел, «+» и приставки поставщика не трогаются:
    в каталоге они различают товары.
    """
    text = normalize_name(name).replace("ё", "е")
    text = _QUOTES.sub(" ", text)
    text = _SIZE.sub(r"\1x", text)
    text = _DECIMAL.sub(r"\1.\2", text)
    text = _NUMBER_SIGN.sub(r"№\1", text)
    text = _UNIT.sub(r"\1 \2", text)
    text = _LONE_DOT.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    text = _PLUS.sub("+", text)
    return _SPACES.sub(" ", text).strip()


def _article_key(value: str | None) -> str:
    return _ARTICLE_NOISE.sub("", normalize_name(value or "").replace("ё", "е"))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = len(a | b)
    return len(a & b) / union if union else 0.0


@dataclass(frozen=True)
class _Name:
    exact: str
    canonical: str
    grams: frozenset[str]
    # Слова с цифрами по порядку: размер, тип, номер, пункт перечня, модель.
    digits: tuple[str, ...]
    plus: int

    @classmethod
    def of(cls, name: str) -> _Name:
        canonical = canonical_name(name)
        return cls(
            exact=normalize_name(name),
            canonical=canonical,
            grams=frozenset(trigrams(canonical)) if canonical else frozenset(),
            digits=tuple(w for w in canonical.split() if any(ch.isdigit() for ch in w)),
            plus=canonical.count("+"),
        )


# Сколько последних букв короткого слова может отличаться, чтобы слова всё ещё
# считались разными формами одного слова, а не опечаткой.
_FORM_ENDING = 2


def _words_compatible(a: str, b: str) -> bool:
    """Отличаются ли названия не больше чем опечаткой в одном слове.

    «Ворон» и «Ворона», «Медведь» и «Медведица» — одна основа, разные товары.
    Опечатка — перестановка или замена букв внутри длинного слова. Сомнение
    решается в пользу проверки менеджером: опечатка в последних буквах тоже
    не даёт автовыбора.
    """
    left, right = _WORD_PARTS.findall(a), _WORD_PARTS.findall(b)
    if len(left) != len(right):
        return False
    differing = [(x, y) for x, y in zip(left, right, strict=True) if x != y]
    if not differing:
        return True
    if len(differing) > 1:
        return False
    x, y = differing[0]
    shorter = min(len(x), len(y))
    if any(ch.isdigit() for ch in x + y) or shorter < _MIN_TYPO_WORD:
        return False
    # Сначала исходные буквы: `stem` сам по себе не опора — из-за беглой гласной
    # «человеку» становится «человк» и перестаёт быть началом «человекуа».
    pairs = enumerate(zip(x, y, strict=False))  # длины разные: «ворон» и «ворона»
    common = next((i for i, (cx, cy) in pairs if cx != cy), shorter)
    if common >= shorter - _FORM_ENDING:
        return False
    sx, sy = stem(x), stem(y)
    if sx.startswith(sy) or sy.startswith(sx):
        return False
    return _jaccard(frozenset(trigrams(x)), frozenset(trigrams(y))) >= _MIN_TYPO_SIMILARITY


@dataclass(frozen=True)
class _Evidence:
    score: float
    name_given: bool
    same_name: bool
    digits_match: bool
    plus_match: bool
    words_compatible: bool
    manufacturer_conflict: bool
    supplier_article_conflict: bool

    @property
    def conflicts(self) -> bool:
        """Есть ли признак, запрещающий `MATCHED_EXACT` и `MATCHED_HIGH`."""
        return (
            not self.digits_match
            or not self.plus_match
            or self.manufacturer_conflict
            or self.supplier_article_conflict
        )

    def reasons(self) -> list[Reason]:
        found: list[Reason] = []
        if self.name_given:
            found.append(Reason.DIGITS_MATCH if self.digits_match else Reason.DIGITS_MISMATCH)
        if not self.plus_match:
            found.append(Reason.PLUS_MISMATCH)
        if self.manufacturer_conflict:
            found.append(Reason.MANUFACTURER_CONFLICT)
        if self.supplier_article_conflict:
            found.append(Reason.SUPPLIER_ARTICLE_CONFLICT)
        return found


# --- Сопоставление ------------------------------------------------------------


class CatalogMatcher:
    """Индекс названий строится один раз по активным товарам репозитория."""

    def __init__(
        self, repository: CatalogRepository, settings: MatchSettings | None = None
    ) -> None:
        self.repository = repository
        self.settings = settings or MatchSettings()
        self._products: dict[str, Product] = {}
        self._names: dict[str, _Name] = {}
        self._by_exact: dict[str, list[str]] = defaultdict(list)
        self._by_canonical: dict[str, list[str]] = defaultdict(list)
        self._by_supplier_article: dict[str, list[str]] = defaultdict(list)
        self._postings: dict[str, list[str]] = defaultdict(list)
        for product in repository.list_active():
            name = _Name.of(product.name)
            self._products[product.id] = product
            self._names[product.id] = name
            self._by_exact[name.exact].append(product.id)
            self._by_canonical[name.canonical].append(product.id)
            if key := _article_key(product.supplier_article):
                self._by_supplier_article[key].append(product.id)
            for gram in name.grams:
                self._postings[gram].append(product.id)

    def match(self, item: MatchInput, *, among: Collection[str] | None = None) -> MatchResult:
        """Найти позицию в каталоге.

        `among` — коды товаров, среди которых искать по названию и артикулу
        поставщика. Код 1С ищется по всему каталогу: товар с этим кодом существует
        однозначно, и по названию его не перепроверяют.

        Совпадением считается только активный товар. `get_by_article` репозитория
        возвращает и неактивные: неактивный код не сопоставляется, а причина
        `CODE_INACTIVE` показывает, что товар в каталоге есть. Вернуть ли его в
        продажу, решает применение импорта (EPIC 4), а не сопоставление.
        """
        code = (item.article_1c or "").strip()
        product = self.repository.get_by_article(code) if code else None
        if product is not None and product.is_active:
            return self._by_code(item, product)
        pool = None if among is None else frozenset(among)
        result = self._by_name(item, pool)
        if code:
            missing = Reason.CODE_NOT_IN_CATALOG if product is None else Reason.CODE_INACTIVE
            result = replace(result, reason_codes=(missing, *result.reason_codes))
        return result

    # --- по коду 1С -------------------------------------------------------------

    def _by_code(self, item: MatchInput, product: Product) -> MatchResult:
        query = _Name.of(item.name)
        evidence = self._evidence(item, query, product)
        reasons = [Reason.CODE_MATCH, self._name_reason(query, product, evidence)]
        reasons += evidence.reasons()
        compatible = (
            not query.canonical or evidence.score >= self.settings.review_threshold
        ) and not evidence.conflicts
        return MatchResult(
            status=MatchStatus.MATCHED_EXACT if compatible else MatchStatus.MATCHED_REVIEW,
            method=MatchMethod.CODE_1C,
            confidence=evidence.score,
            matched_product=product,
            candidates=(self._candidate(item, query, product),),
            reason_codes=tuple(reasons),
        )

    # --- без кода: название, артикул поставщика, похожесть -------------------

    def _by_name(self, item: MatchInput, pool: frozenset[str] | None) -> MatchResult:
        query = _Name.of(item.name)
        article = _article_key(item.supplier_article)
        if not query.canonical and not article:
            return self._nothing(MatchMethod.NONE, 0.0, (), [Reason.NAME_NOT_PROVIDED])

        if query.canonical:
            steps = (
                (MatchMethod.EXACT_NAME, Reason.NAME_EXACT_MATCH, self._by_exact, query.exact),
                (
                    MatchMethod.NORMALIZED_NAME,
                    Reason.NAME_NORMALIZED_MATCH,
                    self._by_canonical,
                    query.canonical,
                ),
            )
            for method, reason, index, key in steps:
                ids = self._allowed(index.get(key, ()), pool)
                if len(ids) > 1:
                    return MatchResult(
                        status=MatchStatus.AMBIGUOUS,
                        method=method,
                        confidence=1.0,
                        matched_product=None,
                        candidates=self._candidates(item, query, ids),
                        reason_codes=(reason, Reason.MULTIPLE_CANDIDATES),
                    )
                if len(ids) == 1:
                    return self._single(item, query, ids[0], method, [reason])

        if article:
            holders = self._allowed(self._by_supplier_article.get(article, ()), pool)
            if holders:
                return self._by_article(item, query, holders)

        if not query.canonical:
            return self._nothing(
                MatchMethod.SUPPLIER_ARTICLE,
                0.0,
                (),
                [Reason.NAME_NOT_PROVIDED, Reason.NO_CANDIDATES],
            )
        return self._by_similarity(item, query, pool)

    def _single(
        self,
        item: MatchInput,
        query: _Name,
        product_id: str,
        method: MatchMethod,
        reasons: list[Reason],
    ) -> MatchResult:
        product = self._products[product_id]
        evidence = self._evidence(item, query, product)
        return MatchResult(
            status=MatchStatus.MATCHED_REVIEW if evidence.conflicts else MatchStatus.MATCHED_HIGH,
            method=method,
            confidence=evidence.score,
            matched_product=product,
            candidates=(self._candidate(item, query, product),),
            reason_codes=(*reasons, Reason.SINGLE_CANDIDATE, *evidence.reasons()),
        )

    def _by_article(self, item: MatchInput, query: _Name, holders: list[str]) -> MatchResult:
        """Артикул поставщика не уникален и часто короткий («065»): без названия не решает."""
        confirmed = [
            pid
            for pid in holders
            if self._confirms(self._evidence(item, query, self._products[pid]))
        ]
        candidates = self._candidates(item, query, holders)
        if len(holders) == 1:
            product = self._products[holders[0]]
            evidence = self._evidence(item, query, product)
            if confirmed:
                status = (
                    MatchStatus.MATCHED_REVIEW if evidence.conflicts else MatchStatus.MATCHED_HIGH
                )
                reasons = [Reason.SUPPLIER_ARTICLE_MATCH, Reason.NAME_CONFIRMS]
                reasons += [Reason.SINGLE_CANDIDATE]
            else:
                status = MatchStatus.MATCHED_REVIEW
                missing = Reason.NAME_NOT_CONFIRMED if query.canonical else Reason.NAME_NOT_PROVIDED
                reasons = [Reason.SUPPLIER_ARTICLE_MATCH, missing]
            return MatchResult(
                status=status,
                method=MatchMethod.SUPPLIER_ARTICLE,
                confidence=evidence.score,
                matched_product=product,
                candidates=candidates,
                reason_codes=(*reasons, *evidence.reasons()),
            )

        reasons = [Reason.SUPPLIER_ARTICLE_MATCH, Reason.SUPPLIER_ARTICLE_NOT_UNIQUE]
        if len(confirmed) == 1:
            product = self._products[confirmed[0]]
            return MatchResult(
                status=MatchStatus.MATCHED_REVIEW,
                method=MatchMethod.SUPPLIER_ARTICLE,
                confidence=self._evidence(item, query, product).score,
                matched_product=product,
                candidates=candidates,
                reason_codes=(*reasons, Reason.NAME_CONFIRMS),
            )
        return MatchResult(
            status=MatchStatus.AMBIGUOUS,
            method=MatchMethod.SUPPLIER_ARTICLE,
            confidence=candidates[0].score,
            matched_product=None,
            candidates=candidates,
            reason_codes=(*reasons, Reason.MULTIPLE_CANDIDATES),
        )

    def _by_similarity(
        self, item: MatchInput, query: _Name, pool: frozenset[str] | None
    ) -> MatchResult:
        ranked = self._similar(query, pool)
        if not ranked:
            return self._nothing(MatchMethod.FUZZY_NAME, 0.0, (), [Reason.NO_CANDIDATES])
        candidates = self._candidates(item, query, [pid for _, pid in ranked])
        best, top_id = ranked[0]
        settings = self.settings
        if best < settings.review_threshold:
            return self._nothing(MatchMethod.FUZZY_NAME, best, candidates, [Reason.LOW_SIMILARITY])
        if len(ranked) > 1:
            second = ranked[1][0]
            if second >= settings.review_threshold and best - second < settings.ambiguity_margin:
                return MatchResult(
                    status=MatchStatus.AMBIGUOUS,
                    method=MatchMethod.FUZZY_NAME,
                    confidence=round(best, 4),
                    matched_product=None,
                    candidates=candidates,
                    reason_codes=(Reason.NAME_SIMILAR, Reason.CLOSE_SECOND_CANDIDATE),
                )

        product = self._products[top_id]
        evidence = self._evidence(item, query, product)
        reasons = [Reason.NAME_SIMILAR, *evidence.reasons()]
        if not evidence.words_compatible:
            reasons.append(Reason.WORDS_CONFLICT)
        status = MatchStatus.MATCHED_REVIEW
        if not settings.auto_enabled:
            reasons.append(Reason.AUTO_MATCH_DISABLED)
        elif best < settings.auto_threshold:
            reasons.append(Reason.BELOW_AUTO_THRESHOLD)
        elif evidence.words_compatible and not evidence.conflicts:
            status = MatchStatus.MATCHED_HIGH
        return MatchResult(
            status=status,
            method=MatchMethod.FUZZY_NAME,
            confidence=evidence.score,
            matched_product=product,
            candidates=candidates,
            reason_codes=tuple(reasons),
        )

    # --- признаки ---------------------------------------------------------------

    def _evidence(self, item: MatchInput, query: _Name, product: Product) -> _Evidence:
        name = self._names.get(product.id) or _Name.of(product.name)
        given = bool(query.canonical)
        same = given and query.canonical == name.canonical
        return _Evidence(
            score=1.0 if same or not given else round(_jaccard(query.grams, name.grams), 4),
            name_given=given,
            same_name=same,
            digits_match=not given or query.digits == name.digits,
            plus_match=not given or query.plus == name.plus,
            words_compatible=not given or _words_compatible(query.canonical, name.canonical),
            manufacturer_conflict=_differ(
                canonical_name(item.manufacturer or ""), canonical_name(product.manufacturer or "")
            ),
            supplier_article_conflict=_differ(
                _article_key(item.supplier_article), _article_key(product.supplier_article)
            ),
        )

    def _confirms(self, evidence: _Evidence) -> bool:
        """Подтверждает ли название то, что нашёл другой признак."""
        if not evidence.name_given:
            return False
        if evidence.same_name:
            return True
        return (
            evidence.score >= self.settings.auto_threshold
            and evidence.words_compatible
            and not evidence.conflicts
        )

    def _name_reason(self, query: _Name, product: Product, evidence: _Evidence) -> Reason:
        if not evidence.name_given:
            return Reason.NAME_NOT_PROVIDED
        if query.exact == normalize_name(product.name):
            return Reason.NAME_EXACT_MATCH
        if evidence.same_name:
            return Reason.NAME_NORMALIZED_MATCH
        if evidence.score >= self.settings.review_threshold:
            return Reason.NAME_SIMILAR
        return Reason.NAME_DIFFERS

    def _similar(self, query: _Name, pool: frozenset[str] | None) -> list[tuple[float, str]]:
        """Лучшие по похожести названия.

        С `pool` сравниваются только товары из него, а не весь каталог: импорт ищет
        перекодировку среди исчезнувших кодов, и остальные товары не перебираются.
        """
        overlaps: Counter[str] = Counter()
        if pool is None:
            for gram in query.grams:
                overlaps.update(self._postings.get(gram, ()))
        else:
            for pid in pool:
                if name := self._names.get(pid):
                    overlaps[pid] = len(query.grams & name.grams)
        scored = (
            (overlap / (len(query.grams) + len(self._names[pid].grams) - overlap), pid)
            for pid, overlap in overlaps.items()
            if overlap
        )
        return heapq.nsmallest(MAX_CANDIDATES, scored, key=lambda pair: (-pair[0], pair[1]))

    def _candidate(self, item: MatchInput, query: _Name, product: Product) -> MatchCandidate:
        evidence = self._evidence(item, query, product)
        reasons = [self._name_reason(query, product, evidence), *evidence.reasons()]
        if evidence.name_given and not evidence.words_compatible:
            reasons.append(Reason.WORDS_CONFLICT)
        if _article_key(item.supplier_article) and not evidence.supplier_article_conflict:
            if _article_key(product.supplier_article):
                reasons.append(Reason.SUPPLIER_ARTICLE_MATCH)
        return MatchCandidate(
            product_id=product.id,
            article=product.article,
            name=product.name,
            score=evidence.score,
            reason_codes=tuple(reasons),
        )

    def _candidates(
        self, item: MatchInput, query: _Name, ids: Iterable[str]
    ) -> tuple[MatchCandidate, ...]:
        found = [self._candidate(item, query, self._products[pid]) for pid in ids]
        found.sort(key=lambda c: (-c.score, c.product_id))
        return tuple(found[:MAX_CANDIDATES])

    @staticmethod
    def _allowed(ids: Iterable[str], pool: frozenset[str] | None) -> list[str]:
        return [pid for pid in ids if pool is None or pid in pool]

    @staticmethod
    def _nothing(
        method: MatchMethod,
        confidence: float,
        candidates: tuple[MatchCandidate, ...],
        reasons: list[Reason],
    ) -> MatchResult:
        return MatchResult(
            status=MatchStatus.NOT_FOUND,
            method=method,
            confidence=round(confidence, 4),
            matched_product=None,
            candidates=candidates,
            reason_codes=tuple(reasons),
        )


def _differ(left: str, right: str) -> bool:
    """Признак есть с обеих сторон и не совпадает. Отсутствие — не конфликт."""
    return bool(left and right and left != right)
