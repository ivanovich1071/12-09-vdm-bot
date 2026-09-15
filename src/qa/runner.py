"""`python run.py scenarios` — прогон сценариев через Telegram и отчёт в markdown.

Бот уже запущен в соседнем терминале (`python run.py telegram`). Прогон идёт по диалогу: тестировщик
начинает разговор заново, ведёт его по сценарию до цели или лимита ходов, судья ставит оценку, результат
дописывается в `results.jsonl`, отчёт `report.md` пересобирается. Остановили — тот же `--out` продолжит.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from agent.client import LLMAuthError
from qa.checks import CatalogFacts, check_turn
from qa.judge import judge
from qa.llm import DEFAULT_MODEL, Model, openrouter
from qa.models import DialogResult, Turn
from qa.persona import next_move
from qa.report import append_result, load_results, render
from qa.scenarios import Scenario, Variant, parse, select, variants
from qa.telegram import BotChat, LoginError

SESSION = Path("data/qa/tester")
# 100 сценариев заказчика — файл по умолчанию (tests/scenarios, сверен с присланными частями 14.09).
DEFAULT_SCENARIOS = Path("tests/scenarios/vdm_100_scenarios.md")
MODES = {"main": "только основной путь", "main+1": "основной путь и одна ветка по очереди", "all": "основной путь и все ветки"}

HELP_API = """Нужны TELEGRAM_API_ID и TELEGRAM_API_HASH в .env — ключи приложения для пользовательского аккаунта, не бота.
1. Откройте https://my.telegram.org тестовым аккаунтом → API development tools.
2. Создайте приложение (название любое), впишите api_id и api_hash в .env.
3. Войдите один раз: python run.py scenarios --login — телефон тестового аккаунта (не токен бота) и код из Telegram вводите сами."""


async def main(args) -> int:  # noqa: ANN001 — argparse.Namespace
    from core.config import Settings, load_env

    load_env()
    settings = Settings.from_env()
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not api_id.isdigit() or not api_hash:
        print(HELP_API)
        return 2
    username = (args.bot or os.environ.get("QA_BOT_USERNAME") or bot_username(os.environ.get("TELEGRAM_BOT_TOKEN", ""))).lstrip("@")
    if not username:
        print("Не удалось узнать имя бота: задайте QA_BOT_USERNAME в .env или --bot имя_бота.")
        return 2
    session = Path(os.environ.get("QA_TELEGRAM_SESSION") or SESSION)
    session.parent.mkdir(parents=True, exist_ok=True)

    if args.login:
        try:
            chat = await BotChat.connect(str(session), int(api_id), api_hash, username)
        except LoginError as exc:
            print(exc)
            return 2
        me = await chat.client.get_me()
        print(f"Вход выполнен: {me.first_name or me.username}. Бот для прогона: @{username}")
        await chat.close()
        return 0

    if not settings.openrouter_api_key:
        print("Нужен OPENROUTER_API_KEY в .env: на нём работают тестировщик и судья.")
        return 2
    source = Path(args.file) if args.file else DEFAULT_SCENARIOS
    if not source.exists():
        print(f"Файл сценариев не найден: {source}")
        return 2
    scenarios = select(parse(source.read_text(encoding="utf-8")), args.only)
    if not scenarios:
        print("В файле нет сценариев вида «## Сценарий N. Роль — Цель» (или --only их отсёк).")
        return 2

    out = Path(args.out or f"data/qa/run-{datetime.now():%Y%m%d-%H%M}")
    out.mkdir(parents=True, exist_ok=True)
    results_path, report_path = out / "results.jsonl", out / "report.md"
    results = finished(load_results(results_path))
    plan = plan_dialogs(scenarios, args.mode, {result.key for result in results})
    stop = stop_time(args.until)
    model_name = args.model or os.environ.get("QA_MODEL") or DEFAULT_MODEL
    judge_name = getattr(args, "judge_model", None) or os.environ.get("QA_JUDGE_MODEL") or model_name
    meta = {
        "started": f"{datetime.now():%d.%m.%Y %H:%M}",
        "Файл сценариев": source.name,
        "Бот": f"@{username}",
        "Модель тестировщика": model_name,
        "Модель судьи": judge_name,
        "Варианты": MODES[args.mode],
        "Лимит реплик тестировщика": args.turns,
    }

    facts = CatalogFacts.load(settings)
    model = openrouter(settings, model_name)
    judge_model = model if judge_name == model_name else openrouter(settings, judge_name)
    print(f"Сценариев: {len(scenarios)}, диалогов к прогону: {len(plan)}, уже готово: {len(results)}.")
    if stop:
        print(f"Новые диалоги не начинаю после {stop:%d.%m %H:%M}.")
    print(f"Отчёт: {report_path}", flush=True)
    try:
        chat = await BotChat.connect(str(session), int(api_id), api_hash, username, quiet=args.quiet, timeout=args.timeout)
    except LoginError as exc:
        print(exc)
        return 2
    spent: list[float] = []
    silent = 0
    try:
        for index, (scenario, variant) in enumerate(plan, 1):
            if stop and datetime.now() >= stop:
                print(f"Время --until вышло: пройдено {index - 1} из {len(plan)}. Продолжить — тот же --out.")
                break
            print(f"[{index}/{len(plan)}] сценарий {scenario.number} «{scenario.title}» — {variant.name}", flush=True)
            result = await play(chat, model, facts, scenario, variant, args.turns, judge_model)
            results.append(result)
            spent.append(result.seconds)
            append_result(results_path, result)
            report_path.write_text(render(results, meta), encoding="utf-8")
            errors = sum(1 for finding in result.findings if finding.severity == "error")
            left = (len(plan) - index) * sum(spent) / len(spent) / 3600
            print(
                f"    {result.seconds / 60:.1f} мин, ходов {len(result.turns)}, оценка {result.verdict.score or '—'}, "
                f"ошибок проверок {errors}" + (f", сорвался: {result.error}" if result.error else "")
                + f"; осталось ≈ {left:.1f} ч",
                flush=True,
            )
            # Бот упал или пропал VPN: ночью не жечь по пять минут ожидания на каждый оставшийся диалог.
            silent = silent + 1 if result.error and not result.turns else 0
            if silent >= 3:
                print(
                    f"Три диалога подряд сорвались до первого ответа ({result.error}) — прогон остановлен. "
                    "Проверьте бота и VPN, потом тот же --out: сорванные диалоги пройдут заново."
                )
                break
    finally:
        await chat.close()
        report_path.write_text(render(results, meta), encoding="utf-8")
    print(f"Готово. Отчёт: {report_path}")
    return 0


def finished(results: list[DialogResult]) -> list[DialogResult]:
    """Сорванный до первого хода диалог (бот молчал, Telegram отказал) не пройден — тот же --out его повторит."""
    return [result for result in results if result.turns or not result.error]


def plan_dialogs(scenarios: list[Scenario], mode: str, done: set[str]) -> list[tuple[Scenario, Variant]]:
    """Сначала основные пути всех сценариев, потом ветки: если ночи не хватит, каждый сценарий пройден хотя бы раз."""
    dialogs = [
        (scenario, variant)
        for scenario in scenarios
        for variant in variants(scenario, mode)
        if f"{scenario.number}:{variant.name}" not in done
    ]
    return sorted(dialogs, key=lambda pair: (pair[1].branch is not None, pair[0].number))


def stop_time(until: str | None, now: datetime | None = None) -> datetime | None:
    """`--until 06:40` — ближайшие впереди 06:40, после которых новый диалог не начинается."""
    if not until:
        return None
    now = now or datetime.now()
    hour, minute = (int(part) for part in until.split(":"))
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return moment if moment > now else moment + timedelta(days=1)


async def play(
    chat: BotChat,
    model: Model,
    facts: CatalogFacts,
    scenario: Scenario,
    variant: Variant,
    max_turns: int,
    judge_model: Model | None = None,
) -> DialogResult:
    result = DialogResult(
        scenario=scenario.number,
        title=scenario.title,
        role=scenario.role,
        goal=scenario.goal,
        variant=variant.name,
        started=f"{datetime.now():%d.%m.%Y %H:%M:%S}",
    )
    clock = time.monotonic()
    history: list[str] = []
    try:
        restart = await chat.restart()
        if restart.timed_out:
            result.error = "бот не ответил на «Начать заново» — он запущен?"
            return result
        for number in range(1, max_turns + 1):
            move = await asyncio.to_thread(next_move, model, scenario, variant, result.turns, number, max_turns)
            if move.kind == "end":
                result.end_reason = move.reason
                break
            exchange = await (chat.click(move.text) if move.kind == "button" else chat.send(move.text))
            if exchange.late and result.turns:
                result.turns[-1].messages.extend(exchange.late)
            turn = Turn(number, move.kind, move.text, move.reason, exchange.seconds, exchange.timed_out, exchange.messages)
            said = [earlier.text for earlier in result.turns if earlier.kind == "text"]
            turn.findings = check_turn(turn, facts, history, said + ([move.text] if move.kind == "text" else []))
            history += [message.text for message in turn.messages]
            result.turns.append(turn)
        else:
            result.end_reason = f"лимит {max_turns} реплик"
    except LLMAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 — один сорванный диалог не останавливает прогон
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.seconds = round(time.monotonic() - clock, 1)
    if result.turns:
        result.verdict = await asyncio.to_thread(judge, judge_model or model, scenario, variant, result)
    return result


def bot_username(token: str) -> str:
    """Имя бота по токену (getMe), чтобы не вписывать его руками. Токен никуда не выводится."""
    if not token:
        return ""
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=20) as response:
            return str(json.loads(response.read().decode("utf-8")).get("result", {}).get("username") or "")
    except Exception:  # noqa: BLE001 — адрес с токеном в сообщение об ошибке не пускаем
        return ""
