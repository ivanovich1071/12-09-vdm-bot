"""Отчёт прогона в markdown: итог, сводная таблица, частые проблемы, стенограммы с замечаниями.

Результаты копятся в `results.jsonl` по диалогу — прерванный прогон продолжается с того же места
(`--out` той же папки), а отчёт пересобирается целиком после каждого диалога.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from qa.checks import LABELS
from qa.models import DialogResult


def load_results(path: Path) -> list[DialogResult]:
    if not path.exists():
        return []
    return [DialogResult.from_dict(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_result(path: Path, result: DialogResult) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")


def render(results: list[DialogResult], meta: dict[str, Any]) -> str:
    lines = [f"# Автотест сценариев vdm-бота — {meta.get('started', '')}", ""]
    lines += [f"- {name}: {value}" for name, value in meta.items() if name != "started"]
    lines += ["", *_summary(results), "", *_table(results), "", *_problems(results), "", "## Диалоги", ""]
    for result in results:
        lines += _dialog(result)
    return "\n".join(lines).rstrip() + "\n"


def _summary(results: list[DialogResult]) -> list[str]:
    turns = [turn for result in results for turn in result.turns]
    answered = [turn.seconds for turn in turns if turn.messages]
    scores = [result.verdict.score for result in results if result.verdict.score]
    judged = [result for result in results if result.verdict.goal_reached is not None]
    findings = [finding for result in results for finding in result.findings]
    lines = ["## Итог", ""]
    lines.append(f"- Диалогов: {len(results)}, ходов: {len(turns)}, сорвалось с ошибкой: {sum(1 for r in results if r.error)}")
    if scores:
        lines.append(f"- Средняя оценка судьи: {statistics.mean(scores):.1f} из 5 (оценено {len(scores)})")
    if judged:
        reached = sum(1 for result in judged if result.verdict.goal_reached)
        kept = sum(1 for result in judged if result.verdict.context_kept)
        lines.append(f"- Цель достигнута: {reached} из {len(judged)}; контекст удержан: {kept} из {len(judged)}")
    if answered:
        lines.append(
            f"- Первый ответ бота: в среднем {statistics.mean(answered):.0f} с, медиана {statistics.median(answered):.0f} с, "
            f"максимум {max(answered):.0f} с"
        )
    lines.append(
        f"- Замечаний проверок: ошибок {sum(1 for f in findings if f.severity == 'error')}, "
        f"предупреждений {sum(1 for f in findings if f.severity == 'warning')}"
    )
    return lines


def _table(results: list[DialogResult]) -> list[str]:
    lines = [
        "## Сводная таблица",
        "",
        "| № | Сценарий | Вариант | Ходов | Ср. ответ, с | Макс., с | Ошибки | Предупр. | Оценка | Цель | Контекст |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        answered = [turn.seconds for turn in result.turns if turn.messages]
        findings = result.findings
        verdict = result.verdict
        lines.append(
            "| "
            + " | ".join(
                [
                    str(result.scenario),
                    _cell(result.title),
                    _cell(result.variant),
                    str(len(result.turns)),
                    f"{statistics.mean(answered):.0f}" if answered else "—",
                    f"{max(answered):.0f}" if answered else "—",
                    str(sum(1 for f in findings if f.severity == "error")),
                    str(sum(1 for f in findings if f.severity == "warning")),
                    str(verdict.score) if verdict.score else ("ошибка" if result.error or verdict.error else "—"),
                    _yes(verdict.goal_reached),
                    _yes(verdict.context_kept),
                ]
            )
            + " |"
        )
    return lines


def _problems(results: list[DialogResult]) -> list[str]:
    lines = ["## Частые проблемы", ""]
    codes: Counter[str] = Counter()
    where: dict[str, list[str]] = {}
    for result in results:
        for code in {finding.code for finding in result.findings}:
            codes[code] += 1
            where.setdefault(code, []).append(str(result.scenario))
    if codes:
        lines += ["Проверки кода (в скольких диалогах):", ""]
        lines += [f"- {LABELS.get(code, code)} — {count}: сценарии {', '.join(where[code][:15])}" for code, count in codes.most_common()]
        lines.append("")
    serious = [
        (result, problem)
        for result in results
        for problem in result.verdict.problems
        if problem.get("severity") in ("критично", "важно")
    ]
    if serious:
        lines += ["Судья — критичное и важное:", ""]
        for result, problem in sorted(serious, key=lambda pair: pair[1].get("severity") != "критично")[:40]:
            turn = f", ход {problem['turn']}" if problem.get("turn") else ""
            lines.append(f"- [{problem['severity']}] сц. {result.scenario} ({result.variant}{turn}): {problem['problem']}")
    if len(lines) == 2:
        lines.append("Не найдено.")
    return lines


def _dialog(result: DialogResult) -> list[str]:
    verdict = result.verdict
    lines = [f"### {result.scenario}. {result.title} — {result.variant}", ""]
    lines.append(f"Цель: {result.goal}. Начало {result.started}, длительность {result.seconds / 60:.1f} мин.")
    if result.end_reason:
        lines.append(f"Завершение: {result.end_reason}.")
    if result.error:
        lines.append(f"**Прогон сорвался:** {result.error}")
    if verdict.score:
        lines.append(
            f"**Оценка {verdict.score}/5** · цель: {_yes(verdict.goal_reached)} · контекст: {_yes(verdict.context_kept)}"
            + (f" · ветка: {_yes(verdict.branch_handled)}" if verdict.branch_handled is not None else "")
        )
    if verdict.summary:
        lines.append(f"> {verdict.summary}")
    if verdict.error:
        lines.append(f"Судья не ответил: {verdict.error}")
    if verdict.problems:
        lines.append("")
        lines += [
            f"- [{problem['severity']}]" + (f" ход {problem['turn']}:" if problem.get("turn") else "") + f" {problem['problem']}"
            for problem in verdict.problems
        ]
    lines.append("")
    for turn in result.turns:
        said = f"нажимает «{turn.text}»" if turn.kind == "button" else turn.text
        lines.append(f"**{turn.number}. Клиент:** {said}" + (f" _({turn.reason})_" if turn.reason else ""))
        lines.append("")
        if not turn.messages:
            lines += [f"> _бот не ответил за {turn.seconds:.0f} с_", ""]
        for index, message in enumerate(turn.messages):
            head = f"**Бот** · {turn.seconds:.0f} с" if index == 0 else "**Бот**"
            marks = [mark for mark, on in (("изменено", message.edited), ("пришло позже", message.late)) if on]
            if marks:
                head += f" _({', '.join(marks)})_"
            lines.append(head)
            lines += [f"> {line}" if line else ">" for line in (message.text or "").splitlines()] or ["> _(без текста)_"]
            if message.photo:
                lines.append("> 🖼 фото")
            if message.file:
                size = f", {message.file_size // 1024} КБ" if message.file_size else ""
                lines.append(f"> 📎 {message.file}{size}")
            if message.buttons:
                lines.append("> Кнопки: " + " · ".join(f"[{label}]" for label in message.buttons))
            lines.append("")
        for finding in turn.findings:
            mark = "❌" if finding.severity == "error" else "⚠️"
            lines.append(f"{mark} {LABELS.get(finding.code, finding.code)}: {finding.text}")
        if turn.findings:
            lines.append("")
    lines += ["---", ""]
    return lines


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _yes(value: bool | None) -> str:
    return "—" if value is None else ("да" if value else "нет")
