"""Сценарии из markdown заказчика.

Формат — как в «100 сценариев для бота-консультанта и бота-продажника»:

    ## Сценарий 7. Воспитатель — Оснастить игровой уголок «Магазин »
    **Категория:** …  **Тип клиента:** …  **Маршрут:** …  **Цель:** …
    **Клиент:** первая реплика   **Консультант:** … **Продажник:** …
    ### Данные передачи   - `required_fields`: возраст, число детей, бюджет
    ### Ветки              - **Цена:** «ожидаемый ответ бота»

Реплики ботов из файла — эталон для судьи, а не текст, который бот обязан повторить.
Отсутствующие разделы не ломают разбор: сценарий без веток гоняется только основным путём.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADER = re.compile(r"^##\s+Сценарий\s+(\d+)\.\s*(.+?)\s*$", re.MULTILINE)
_FIELD = re.compile(r"^\*\*(Категория|Тип клиента|Маршрут|Цель):\*\*\s*(.+?)\s*$", re.MULTILINE)
_REPLICA = re.compile(r"^\*\*(Клиент|Консультант|Продажник):\*\*\s*(.+?)\s*$", re.MULTILINE)
_HANDOFF = re.compile(r"^-\s+`(\w+)`:\s*(.+?)\s*$", re.MULTILINE)
_BRANCH = re.compile(r"^-\s+\*\*(.+?):\*\*\s*(.+?)\s*$", re.MULTILINE)

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
        scenarios.append(
            Scenario(
                number=int(header.group(1)),
                title=title,
                role=role.strip(),
                goal=fields.get("Цель") or title_goal.strip(),
                category=fields.get("Категория", ""),
                client_type=fields.get("Тип клиента", ""),
                route=fields.get("Маршрут", ""),
                first_message=next((line for who, line in replicas if who == "Клиент"), ""),
                required_fields=tuple(
                    part.strip() for part in handoff.get("required_fields", "").split(",") if part.strip()
                ),
                handoff=handoff,
                branches=branches,
                reference="\n".join(f"{who}: {line}" for who, line in replicas),
            )
        )
    return scenarios


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


def _section(body: str, name: str) -> str:
    match = re.search(rf"^###\s+{re.escape(name)}\s*$(.*?)(?=^###\s|^---\s*$|\Z)", body, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def _unquote(value: str) -> str:
    """«Покажу цену из карточки». → Покажу цену из карточки"""
    return re.sub(r"[»\"]\.?$", "", re.sub(r"^[«\"]", "", value.strip())).strip()


def _clean(value: str) -> str:
    """«Магазин » → «Магазин»: в файле перед закрывающими кавычками и точками стоят лишние пробелы."""
    return re.sub(r"\s+([»”.,:;!?])", r"\1", value).strip()
