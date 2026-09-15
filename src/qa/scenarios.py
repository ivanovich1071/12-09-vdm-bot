"""Сценарии из markdown заказчика.

Формат — как в «100 сценариев для бота-консультанта и бота-продажника»:

    ## Сценарий 7. Воспитатель — Оснастить игровой уголок «Магазин »
    **Категория:** …  **Тип клиента:** …  **Маршрут:** …  **Цель:** …
    **Клиент:** первая реплика   **Консультант:** … **Продажник:** …
    ### Данные передачи   - `required_fields`: возраст, число детей, бюджет
    ### Ветки              - **Цена:** «ожидаемый ответ бота»

Реплики ботов из файла — эталон для судьи, а не текст, который бот обязан повторить.
Отсутствующие разделы не ломают разбор: сценарий без веток гоняется только основным путём.

Второй формат — «50 сценариев» (15.09): диалог расписан по ходам, реплики клиента идут как написаны.

    **Ход 2. Клиент:** без мебели, мебель отдельно по 44-ФЗ.
    **Ход 3. Бот:** Понял. …                       ← полный файл: эталонный ответ
    **[БОТ ДОЛЖЕН СГЕНЕРИРОВАТЬ: запрос параметров]** ← урезанный файл: что ждём вместо ответа
    **Что проверяет сценарий:**  - Не обещает скидку…

Полный и урезанный файлы — одни и те же реплики клиента; `merge` сводит их в один сценарий.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

_HEADER = re.compile(r"^##\s+Сценарий\s+(\d+)\.\s*(.+?)\s*$", re.MULTILINE)
_FIELD = re.compile(r"^\*\*(Категория|Тип клиента|Маршрут|Цель):\*\*\s*(.+?)\s*$", re.MULTILINE)
_REPLICA = re.compile(r"^\*\*(Клиент|Консультант|Продажник):\*\*\s*(.+?)\s*$", re.MULTILINE)
_HANDOFF = re.compile(r"^-\s+`(\w+)`:\s*(.+?)\s*$", re.MULTILINE)
_BRANCH = re.compile(r"^-\s+\*\*(.+?):\*\*\s*(.+?)\s*$", re.MULTILINE)
_STEP = re.compile(
    r"^\*\*Ход\s+\d+\.\s*(Клиент|Бот):\*\*\s*(.+?)\s*$|^\*\*\[БОТ ДОЛЖЕН СГЕНЕРИРОВАТЬ:\s*(.+?)\]\*\*\s*$",
    re.MULTILINE,
)
_CHECKS = re.compile(r"^\*\*Что проверяет сценарий:\*\*\s*$(.*?)(?=^---\s*$|\Z)", re.MULTILINE | re.DOTALL)
_ITEM = re.compile(r"^-\s+(.+?)\s*$", re.MULTILINE)

MAIN = "основной"
MODES = ("main", "main+1", "all")


@dataclass(frozen=True)
class Branch:
    name: str
    expected: str


@dataclass(frozen=True)
class Variant:
    name: str
    branch: Branch | None = None


@dataclass(frozen=True)
class Step:
    """Реплика клиента из сценария «Ход N» и что ждём от ответа бота на неё."""

    client: str
    reference: str = ""  # «Ход N. Бот» из полного файла — ориентир по смыслу
    expected: str = ""  # «[БОТ ДОЛЖЕН СГЕНЕРИРОВАТЬ: …]» из урезанного файла


@dataclass(frozen=True)
class Scenario:
    number: int
    title: str
    role: str
    goal: str
    category: str = ""
    client_type: str = ""
    route: str = ""
    first_message: str = ""
    required_fields: tuple[str, ...] = ()
    handoff: dict[str, str] = field(default_factory=dict)
    branches: tuple[Branch, ...] = ()
    reference: str = ""
    # Формат «Ход N»: реплики клиента отправляются как написаны, тестировщик-модель их не пересказывает.
    script: tuple[Step, ...] = ()
    checks: tuple[str, ...] = ()


def parse(text: str) -> list[Scenario]:
    headers = list(_HEADER.finditer(text))
    scenarios: list[Scenario] = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[header.end() : end]
        title = _clean(header.group(2))
        role, _, title_goal = title.partition(" — ")
        fields = {name: _clean(value) for name, value in _FIELD.findall(body)}
        replicas = [(who, _clean(line)) for who, line in _REPLICA.findall(body)]
        handoff = {key: _clean(value) for key, value in _HANDOFF.findall(_section(body, "Данные передачи"))}
        branches = tuple(
            Branch(_clean(name), _unquote(_clean(expected)))
            for name, expected in _BRANCH.findall(_section(body, "Ветки"))
        )
        script = _script(body)
        checks = _CHECKS.search(body)
        scenarios.append(
            Scenario(
                number=int(header.group(1)),
                title=title,
                role=role.strip(),
                goal=fields.get("Цель") or title_goal.strip(),
                category=fields.get("Категория", ""),
                client_type=fields.get("Тип клиента", ""),
                route=fields.get("Маршрут", ""),
                first_message=next((line for who, line in replicas if who == "Клиент"), script[0].client if script else ""),
                required_fields=tuple(
                    part.strip() for part in handoff.get("required_fields", "").split(",") if part.strip()
                ),
                handoff=handoff,
                branches=branches,
                reference="\n".join(f"{who}: {line}" for who, line in replicas),
                script=script,
                checks=tuple(_clean(item) for item in _ITEM.findall(checks.group(1))) if checks else (),
            )
        )
    return scenarios


def merge(*files: list[Scenario]) -> list[Scenario]:
    """Несколько файлов — один прогон.

    Сценарий с тем же номером в двух файлах — один диалог из полного и урезанного файлов: реплики клиента
    те же, эталонные ответы бота берутся из одного, ожидания вместо вырезанных ответов — из другого.
    Гонять оба файла по отдельности — дважды тот же разговор. Разные реплики под одним номером — ошибка.
    """
    merged: dict[int, Scenario] = {}
    for scenarios in files:
        for scenario in scenarios:
            known = merged.get(scenario.number)
            if known is None:
                merged[scenario.number] = scenario
                continue
            clients = [step.client for step in known.script]
            if not clients or clients != [step.client for step in scenario.script]:
                raise ValueError(
                    f"Сценарий {scenario.number} есть в двух файлах с разными репликами клиента — "
                    "такие файлы гоняйте разными прогонами (разный --out)."
                )
            merged[scenario.number] = replace(
                known,
                script=tuple(
                    replace(ours, reference=ours.reference or theirs.reference, expected=ours.expected or theirs.expected)
                    for ours, theirs in zip(known.script, scenario.script, strict=True)
                ),
                checks=known.checks or scenario.checks,
            )
    return [merged[number] for number in sorted(merged)]


def select(scenarios: list[Scenario], only: str | None) -> list[Scenario]:
    """`--only 1-10,15` — номера сценариев; пусто — все."""
    if not only:
        return scenarios
    wanted: set[int] = set()
    for part in only.replace(" ", "").split(","):
        if not part:
            continue
        low, _, high = part.partition("-")
        wanted.update(range(int(low), int(high or low) + 1))
    return [scenario for scenario in scenarios if scenario.number in wanted]


def variants(scenario: Scenario, mode: str) -> list[Variant]:
    """Основной путь и ветки. `main+1` — одна ветка по очереди: у 1-го сценария первая, у 2-го вторая…"""
    chosen = [Variant(MAIN)]
    if mode == "all":
        chosen += [Variant(branch.name, branch) for branch in scenario.branches]
    elif mode == "main+1" and scenario.branches:
        branch = scenario.branches[(scenario.number - 1) % len(scenario.branches)]
        chosen.append(Variant(branch.name, branch))
    return chosen


def _script(body: str) -> tuple[Step, ...]:
    """Ответ бота или ожидание после реплики клиента относятся к ней.

    Приветствие до первой реплики бот пишет сам после «Начать заново» — прогон его не сверяет.
    """
    steps: list[Step] = []
    for who, text, expected in _STEP.findall(body):
        if who == "Клиент":
            steps.append(Step(_clean(text)))
        elif steps and who == "Бот":
            steps[-1] = replace(steps[-1], reference=_clean(text))
        elif steps:
            steps[-1] = replace(steps[-1], expected=_clean(expected))
    return tuple(steps)


def _section(body: str, name: str) -> str:
    match = re.search(rf"^###\s+{re.escape(name)}\s*$(.*?)(?=^###\s|^---\s*$|\Z)", body, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def _unquote(value: str) -> str:
    """«Покажу цену из карточки». → Покажу цену из карточки"""
    return re.sub(r"[»\"]\.?$", "", re.sub(r"^[«\"]", "", value.strip())).strip()


def _clean(value: str) -> str:
    """«Магазин » → «Магазин»: в файле перед закрывающими кавычками и точками стоят лишние пробелы."""
    return re.sub(r"\s+([»”.,:;!?])", r"\1", value).strip()
