"""Судья: оценка диалога целиком по сценарию — на той же модели, что и тестировщик.

Факты (цены, коды, время) проверяет код в `qa/checks.py`; судья смотрит на то, чего регулярками не
поймать: достигнута ли цель, держит ли бот контекст, отработана ли ветка, не выдумывает ли.
"""

from __future__ import annotations

from agent.client import LLMAuthError, LLMError
from qa.checks import LABELS
from qa.llm import Model, ask_json
from qa.models import DialogResult, Verdict, transcript
from qa.scenarios import Scenario, Variant

SYSTEM = """Ты — строгий QA-аналитик диалоговых ботов. Оцени разговор бота магазина ЭЛТИ-КУДИЦ (vdm.ru) с клиентом \
по сценарию. У бота две роли: консультант выявляет задачу и объясняет нормативный контекст (приказ № 1057 для садов, \
№ 838 для школ, ФГОС, ФОП ДО), продавец подбирает конкретные товары каталога и ведёт к корзине, файлу или заявке.

Правила бота:
- цены, наличие, коды 1С и характеристики — только из каталога, ничего не выдумывать;
- не обещать соответствие приказу без сверки с перечнем; номера пунктов — только настоящие;
- не давать юридических, медицинских и дефектологических заключений;
- уточнять недостающее коротко, не анкетой; не переспрашивать то, что клиент уже сказал;
- держать контекст: отвечать на последнюю реплику, не возвращаться к старой задаче;
- без мусора: без повторов, простыней, markdown, чужих товаров.

Критерии: 1) цель клиента достигнута; 2) контекст удержан; 3) нужное выяснено; 4) ветка варианта отработана так, \
как ожидает сценарий; 5) правила соблюдены; 6) ответы по делу и читаемы.
Оценка: 5 — всё хорошо; 4 — мелкие огрехи; 3 — цель достигнута, но с заметными проблемами; 2 — цель не достигнута \
или серьёзная ошибка; 1 — провал (мусор, выдумки, потерянный контекст, бот не отвечает).

Ответ — только JSON:
{"score": 1-5, "goal_reached": true|false, "context_kept": true|false, "branch_handled": true|false|null, \
"problems": [{"turn": номер хода или null, "severity": "критично"|"важно"|"мелочь", "problem": "что не так, конкретно"}], \
"summary": "2–3 предложения: что получилось и что сломалось"}"""

SEVERITIES = ("критично", "важно", "мелочь")


def judge(model: Model, scenario: Scenario, variant: Variant, result: DialogResult) -> Verdict:
    try:
        data = ask_json(model, SYSTEM, brief(scenario, variant, result), temperature=0.1, max_tokens=1200)
    except LLMAuthError:
        raise
    except LLMError as exc:
        return Verdict(error=str(exc))
    return verdict_from(data)


def brief(scenario: Scenario, variant: Variant, result: DialogResult) -> str:
    lines = [
        f"Сценарий {scenario.number}. {scenario.title}",
        f"Клиент: {scenario.role}" + (f", {scenario.client_type}" if scenario.client_type else ""),
        f"Цель: {scenario.goal}",
        f"Маршрут: {scenario.route or 'не указан'}",
        f"Что бот должен выяснить: {', '.join(scenario.required_fields) or 'не указано'}",
    ]
    if variant.branch is None:
        lines.append("Вариант: основной путь (branch_handled = null).")
    else:
        lines.append(f"Вариант — ветка «{variant.branch.name}». Ожидаемая реакция бота: «{variant.branch.expected}»")
    if scenario.reference:
        lines += ["", "Эталон сценария (ориентир по смыслу, не текст для сверки слово в слово):", scenario.reference[:1500]]
    found = result.findings
    if found:
        lines += ["", "Автоматические проверки нашли:"]
        lines += [f"- ход {turn.number}: {LABELS.get(f.code, f.code)} — {f.text}" for turn in result.turns for f in turn.findings]
    lines += ["", f"Разговор закончился: {result.end_reason or result.error or 'без пометки'}", "", "Разговор:", transcript(result.turns)]
    return "\n".join(lines)


def verdict_from(data: dict) -> Verdict:
    try:
        score = max(0, min(5, int(data.get("score") or 0)))
    except (TypeError, ValueError):
        score = 0
    problems = []
    for raw in data.get("problems") or []:
        if not isinstance(raw, dict) or not str(raw.get("problem") or "").strip():
            continue
        severity = str(raw.get("severity") or "").lower()
        turn = raw.get("turn")
        problems.append(
            {
                "turn": turn if isinstance(turn, int) else None,
                "severity": severity if severity in SEVERITIES else "важно",
                "problem": str(raw["problem"]).strip(),
            }
        )
    return Verdict(
        score=score,
        goal_reached=_flag(data.get("goal_reached")),
        context_kept=_flag(data.get("context_kept")),
        branch_handled=_flag(data.get("branch_handled")),
        problems=problems[:12],
        summary=str(data.get("summary") or "").strip(),
    )


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
