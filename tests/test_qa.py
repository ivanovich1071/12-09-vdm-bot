"""Автотест сценариев: разбор файла, тестировщик, проверки, судья, отчёт и ожидание хода — без Telegram и сети."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_fixtures import products
from qa.checks import CatalogFacts, check_turn
from qa.judge import judge, verdict_from
from qa.llm import parse_json
from qa.models import BotMessage, DialogResult, Finding, Turn, Verdict
from qa.persona import Move, move_from, next_move, recent_buttons
from qa.report import append_result, load_results, render
from qa.runner import finished, plan_dialogs, play, stop_time
from qa.scenarios import merge, parse, select, variants
from qa.telegram import BotChat, Exchange, ask_phone

SAMPLE = """# 100 сценариев

## Сценарий 7. Воспитатель — Оснастить игровой уголок «Магазин »

**Категория:** Игрушки и сюжетные игры
**Тип клиента:** B2G/B2B
**Маршрут:** Консультант → Продажник
**Цель:** Оснастить игровой уголок «Магазин »

### Диалог: бот-консультант

**Клиент:** Оснастить игровой уголок «Магазин ».

**Консультант:** Уточните, пожалуйста, возраст.

### Данные передачи

- `category`: Игрушки и сюжетные игры
- `required_fields`: возраст, число детей, место, бюджет

### Диалог: бот-продажник

**Продажник:** Принял запрос.

### Ветки

- **Цена:** «Покажу цену из актуальной карточки ».
- **Строго по приказу:** «Сопоставим ваш ТЗ ».

---

## Сценарий 8. Родитель — Выбрать сюжетную игру ребёнку 5 лет

**Цель:** Выбрать сюжетную игру ребёнку 5 лет

**Клиент:** Выбрать сюжетную игру.
"""


class FakeModel:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.requests: list[list[dict]] = []

    def complete(self, messages, tools=None, temperature=0.3, max_tokens=None):  # noqa: ANN001, ANN201
        self.requests.append(messages)
        return {"content": self.answers.pop(0)}


class ReplyInlineMarkup:
    """Кнопки под сообщением — имя класса то же, что у Telethon."""

    def __init__(self, *rows: list[str]) -> None:
        self.rows = [SimpleNamespace(buttons=[SimpleNamespace(text=label) for label in row]) for row in rows]


def _message(number: int, text: str, markup=None):  # noqa: ANN001, ANN202
    return SimpleNamespace(id=number, message=text, reply_markup=markup, document=None, file=None, photo=None)


def _turn(*texts: str, kind: str = "text", said: str = "привет", seconds: float = 5.0) -> Turn:
    return Turn(1, kind, said, seconds=seconds, messages=[BotMessage(id=i, text=text) for i, text in enumerate(texts)])


def test_scenarios_are_parsed_from_the_customer_markdown():
    first, second = parse(SAMPLE)

    assert (first.number, first.role, first.client_type) == (7, "Воспитатель", "B2G/B2B")
    assert first.title == "Воспитатель — Оснастить игровой уголок «Магазин»"
    assert first.goal == "Оснастить игровой уголок «Магазин»"
    assert first.first_message == "Оснастить игровой уголок «Магазин»."
    assert first.required_fields == ("возраст", "число детей", "место", "бюджет")
    assert [(branch.name, branch.expected) for branch in first.branches] == [
        ("Цена", "Покажу цену из актуальной карточки"),
        ("Строго по приказу", "Сопоставим ваш ТЗ"),
    ]
    assert "Продажник: Принял запрос." in first.reference
    assert (second.number, second.branches, second.required_fields) == (8, (), ())


def test_all_hundred_customer_scenarios_are_readable():
    text = (Path(__file__).parent / "scenarios" / "vdm_100_scenarios.md").read_text(encoding="utf-8")
    scenarios = parse(text)

    assert [scenario.number for scenario in scenarios] == list(range(1, 101))
    assert all(s.first_message and s.goal and s.role and s.required_fields for s in scenarios)
    assert all([b.name for b in s.branches] == ["Цена", "Строго по приказу", "Нужен аналог", "Нужно срочно"] for s in scenarios)


def test_main_paths_go_first_and_until_is_the_next_such_moment():
    plan = plan_dialogs(parse(SAMPLE), "main+1", done={"8:основной"})
    assert [(scenario.number, variant.name) for scenario, variant in plan] == [(7, "основной"), (7, "Цена")]
    plan = plan_dialogs(parse(SAMPLE) + [parse(SAMPLE.replace("Сценарий 7.", "Сценарий 9."))[0]], "main+1", set())
    assert [(s.number, v.name) for s, v in plan] == [(7, "основной"), (8, "основной"), (9, "основной"), (7, "Цена"), (9, "Цена")]

    refused = DialogResult(1, "т", "р", "ц", "основной", "now", error="BadRequestError: USER_BOT_TO_BOT_DISABLED")
    played = DialogResult(2, "т", "р", "ц", "основной", "now", turns=[_turn("ответ")], error="TimeoutError")
    assert finished([refused, played]) == [played]

    night = datetime(2026, 9, 14, 23, 0)
    assert stop_time("06:40", night) == datetime(2026, 9, 15, 6, 40)
    assert stop_time("23:30", night) == datetime(2026, 9, 14, 23, 30)
    assert stop_time(None, night) is None


def test_login_takes_a_phone_not_a_bot_token():
    answers = iter(["", "123456789:AAE-token", " +79990000000 "])
    assert ask_phone(lambda prompt: next(answers)) == "+79990000000"


def test_only_and_variants_choose_the_dialogs():
    scenarios = parse(SAMPLE)
    assert [s.number for s in select(scenarios, "8")] == [8]
    assert [s.number for s in select(scenarios, "1-7, 9")] == [7]
    assert [v.name for v in variants(scenarios[0], "main+1")] == ["основной", "Цена"]
    assert [v.name for v in variants(scenarios[0], "all")] == ["основной", "Цена", "Строго по приказу"]
    assert [v.name for v in variants(scenarios[1], "main+1")] == ["основной"]


def test_tester_writes_the_first_message_from_the_scenario():
    scenario = parse(SAMPLE)[0]
    model = FakeModel('```json\n{"action": "text", "text": "нужен уголок магазин в группу", "reason": "начало"}\n```')

    move = next_move(model, scenario, variants(scenario, "main")[0], [], 1, 8)

    assert move == Move("text", "нужен уголок магазин в группу", "начало")
    prompt = model.requests[0][1]["content"]
    assert "Оснастить игровой уголок «Магазин»" in prompt and "ещё не начат" in prompt


def test_tester_presses_only_existing_buttons_and_never_restarts():
    buttons = ["Показать ещё", "Скачать Excel"]
    assert move_from({"action": "button", "text": "показать ещё"}, buttons) == Move("button", "Показать ещё", "")
    assert move_from({"action": "button", "text": "Моя корзина"}, buttons).kind == "text"
    assert move_from({"action": "button", "text": "Да, начать заново"}, buttons).kind == "end"
    assert move_from({}, buttons).kind == "end"


def test_card_and_list_prices_are_checked_against_the_catalog():
    product = next(item for item in products() if item.price)
    facts = CatalogFacts(products(), {"1.5.1.7"})

    card = f"{product.name}\n{product.price + 100} ₽ · в наличии\nКод 1С: {product.sku_1c}\nОснование: 1.5.1.7 и 9.9.9.9"
    assert {finding.code for finding in check_turn(_turn(card), facts, [])} == {"PRICE_MISMATCH", "UNKNOWN_POINT"}
    listed = f"Подобрал:\n\n1. {product.name} — {product.price + 1} ₽ — в наличии — п. 1.5.1.7"
    assert [finding.code for finding in check_turn(_turn(listed), facts, [])] == ["PRICE_MISMATCH"]
    exact = f"{product.name}\n{product.price} ₽ · в наличии\nКод 1С: {product.sku_1c}"
    assert check_turn(_turn(exact), facts, []) == []
    assert [f.code for f in check_turn(_turn("Код 1С: НЕТ-ТАКОГО"), facts, [])] == ["UNKNOWN_SKU"]


def test_silence_slowness_markdown_and_repeats_are_noticed():
    facts = CatalogFacts([], ())
    silent = Turn(2, "button", "Скачать Excel", seconds=300, timed_out=True)
    assert {f.code for f in check_turn(silent, facts, [])} == {"NO_REPLY", "FILE_MISSING"}
    assert {f.code for f in check_turn(_turn("**Итог**\n| a | b |", seconds=200), facts, [])} == {"SLOW", "MARKDOWN"}
    again = _turn("Для какого помещения подбираем?")
    assert [f.code for f in check_turn(again, facts, ["Для какого помещения подбираем?"])] == ["REPEAT"]


def test_judge_verdict_is_read_from_json():
    scenario = parse(SAMPLE)[0]
    result = DialogResult(7, scenario.title, scenario.role, scenario.goal, "основной", "now", turns=[_turn("ответ")])
    model = FakeModel(
        '{"score": 2, "goal_reached": false, "context_kept": true, "branch_handled": null, '
        '"problems": [{"turn": 1, "severity": "критично", "problem": "не показал товары"}, {"problem": ""}], '
        '"summary": "Цель не достигнута."}'
    )

    verdict = judge(model, scenario, variants(scenario, "main")[0], result)

    assert (verdict.score, verdict.goal_reached, verdict.context_kept, verdict.branch_handled) == (2, False, True, None)
    assert verdict.problems == [{"turn": 1, "severity": "критично", "problem": "не показал товары"}]
    assert verdict_from({"score": "плохо"}).score == 0
    assert parse_json('вот: {"a": 1} спасибо') == {"a": 1} and parse_json("без json") == {}


def test_report_has_summary_table_problems_and_transcript(tmp_path):
    turn = _turn("Мяч — 900 ₽", said="нужны мячи")
    turn.findings = [Finding("PRICE_MISMATCH", "error", "«Мяч»: в ответе 900 ₽, в каталоге 908 ₽")]
    result = DialogResult(
        7,
        "Воспитатель — Уголок | магазин",
        "Воспитатель",
        "цель",
        "Цена",
        "14.09.2026 18:00",
        seconds=120,
        turns=[turn],
        verdict=Verdict(score=3, goal_reached=True, context_kept=False, problems=[{"turn": 1, "severity": "важно", "problem": "забыл возраст"}]),
    )
    path = tmp_path / "results.jsonl"
    append_result(path, result)
    loaded = load_results(path)
    assert loaded[0].turns[0].findings[0].code == "PRICE_MISMATCH" and loaded[0].key == "7:Цена"

    text = render(loaded, {"started": "14.09.2026 18:00", "Бот": "@vdm_bot"})
    # Судья сказал «цель достигнута», но в ходе ошибка проверки — в итог идёт «нет», мнение судьи — рядом.
    assert "| 7 | Воспитатель — Уголок \\| магазин | Цена | 1 | 5 | 5 | 1 | 0 | 3 | нет | да | нет |" in text
    assert "Цель достигнута: 0 из 1 (по судье 1" in text
    assert "- цена не совпадает с каталогом — 1: сценарии 7" in text
    assert "- [важно] сц. 7 (Цена, ход 1): забыл возраст" in text
    assert "**1. Клиент:** нужны мячи" in text and "> Мяч — 900 ₽" in text
    assert "❌ цена не совпадает с каталогом" in text


def test_turn_waits_while_the_bot_types_and_ends_after_quiet():
    async def talk():
        chat = BotChat(None, None, quiet=0.3, timeout=5)

        async def bot():
            await chat._events.put(("typing", None))
            await asyncio.sleep(0.4)
            await chat._events.put(("message", _message(1, "Подобрал", ReplyInlineMarkup(["Показать ещё"]))))
            await asyncio.sleep(0.1)
            await chat._events.put(("edit", _message(1, "Подобрал 3 позиции", ReplyInlineMarkup(["Показать ещё"]))))

        task = asyncio.create_task(bot())
        exchange = await chat._collect(time.monotonic(), first_timeout=0.2)
        await task
        silent = await chat._collect(time.monotonic(), first_timeout=0.2)
        return chat, exchange, silent

    chat, exchange, silent = asyncio.run(talk())
    assert [message.text for message in exchange.messages] == ["Подобрал 3 позиции"]
    assert exchange.messages[0].edited and exchange.messages[0].buttons == ["Показать ещё"]
    assert not exchange.timed_out and exchange.seconds >= 0.4
    assert chat._button("показать ещё")[0] is not None
    assert silent.timed_out and not silent.messages


def test_night_problems_are_caught_by_code():
    """Ночь 14.09: не тот файл, чужой возраст, иероглифы, мёртвая кнопка менеджера, предзаказ на 0 ₽."""
    facts = CatalogFacts([], ())
    technopark = ["Предварительная комплектация — технопарк\nОснование: приказ № 838\n- 2.20.153 — робототехнический набор"]
    chairs = Turn(
        4,
        "button",
        "Скачать Excel",
        seconds=1,
        messages=[BotMessage(id=1, text="Комплектация 2.14 Стул ученический. Предварительный список.", file="Комплектация_2_14.xlsx")],
    )
    assert [finding.code for finding in check_turn(chairs, facts, technopark)] == ["WRONG_FILE"]

    toddlers = _turn("Раздел 1.14.3 «Групповые помещения для детей 1 - 2 лет»")
    assert [f.code for f in check_turn(toddlers, facts, [], ["детям 5-6 лет, подготовительная группа"])] == ["AGE_MISMATCH"]
    assert [f.code for f in check_turn(toddlers, facts, [], ["малыши 1-2 года"])] == []

    assert [f.code for f in check_turn(_turn("полоса должна быть不大"), facts, [])] == ["FOREIGN_SCRIPT"]
    dead = Turn(3, "button", "Связаться с менеджером", seconds=1, messages=[BotMessage(id=1, text="Чем помочь?")])
    assert [f.code for f in check_turn(dead, facts, [])] == ["DEAD_BUTTON"]
    zero = _turn("Предварительный заказ PO-20260915-CDE760: позиций 15 на 0 ₽ по текущим ценам.")
    assert [f.code for f in check_turn(zero, facts, [])] == ["ZERO_PREORDER"]
    assert [f.code for f in check_turn(_turn("Я передал ваш запрос специалисту."), facts, [])] == ["FALSE_HANDOFF"]
    sent = _turn("Предварительный заказ PO-20260915-F39948 передан менеджеру.")
    assert check_turn(sent, facts, []) == []

    cards = Turn(
        1,
        "text",
        "все по 1",
        seconds=2,
        messages=[
            BotMessage(id=1, text="Найдено:\n\n13. ПОН Звонкий-глухой (Д-214) — 205 ₽"),
            BotMessage(id=2, text="ПОН Логопедическое лото (Д-222)\n205 ₽ · в наличии 1 шт.\nКод 1С: 34894"),
        ],
    )
    assert "CARD_TEXT" in {finding.code for finding in check_turn(cards, facts, [])}


def test_tester_presses_a_button_from_a_recent_message_instead_of_typing_it():
    turns = [
        Turn(1, "text", "нужна комплектация", messages=[BotMessage(id=1, text="Комплектация…", buttons=["Скачать Excel", "Скачать Word"])]),
        Turn(2, "button", "Скачать Word", messages=[BotMessage(id=2, text="Комплектация 1.5", file="k.docx")]),
    ]
    assert move_from({"action": "button", "text": "Скачать Excel"}, recent_buttons(turns)) == Move("button", "Скачать Excel", "")


# Формат «50 сценариев» 15.09: урезанный файл — ожидания вместо части ответов бота, полный — все ответы.
SCRIPTED = """# 50 сценариев

## Сценарий 1. Заведующая ДОУ, новостройка

**Тип клиента:** B2G
**Цель:** Оснастить 6 групп по приказу 1057
**Удалено ходов бота:** 2 из 3 (ходы: [1, 3])

---

### Диалог

**[БОТ ДОЛЖЕН СГЕНЕРИРОВАТЬ: стандартное приветствие]**

**Ход 1. Клиент:** Оснастить 6 групп, возраст 3-7 лет .

**Ход 2. Бот:** Уточните площадь.

**Ход 2. Клиент:** без мебели.

**[БОТ ДОЛЖЕН СГЕНЕРИРОВАТЬ: предложение передать продажнику]**

**Ход 3. Клиент:** /my_data

---

**Что проверяет сценарий:**
- Не обещает скидку, корректно переводит на менеджера.
- Сохраняет спокойный тон .

---
"""
FULL = SCRIPTED.replace("**[БОТ ДОЛЖЕН СГЕНЕРИРОВАТЬ: стандартное приветствие]**", "**Ход 1. Бот:** Здравствуйте!").replace(
    "**[БОТ ДОЛЖЕН СГЕНЕРИРОВАТЬ: предложение передать продажнику]**", "**Ход 3. Бот:** Обещаю скидку 10 %."
)


class FakeChat:
    """Бот в Telegram: на каждую реплику — заранее заданные сообщения, пустой список — молчание."""

    def __init__(self, *replies: list[str]) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []

    async def restart(self) -> Exchange:
        return Exchange(messages=[BotMessage(id=0, text="Здравствуйте!")], seconds=1)

    async def send(self, text: str) -> Exchange:
        self.sent.append(text)
        texts = self.replies.pop(0)
        return Exchange(messages=[BotMessage(id=len(self.sent), text=t) for t in texts], seconds=1, timed_out=not texts)

    async def click(self, label: str) -> Exchange:
        return await self.send(f"[кнопка] {label}")


def test_turn_by_turn_scenarios_keep_the_client_script_and_merge_full_with_trimmed():
    (trimmed,) = parse(SCRIPTED)

    assert (trimmed.number, trimmed.role, trimmed.client_type) == (1, "Заведующая ДОУ, новостройка", "B2G")
    assert trimmed.goal == "Оснастить 6 групп по приказу 1057"
    assert [step.client for step in trimmed.script] == ["Оснастить 6 групп, возраст 3-7 лет.", "без мебели.", "/my_data"]
    assert trimmed.first_message == "Оснастить 6 групп, возраст 3-7 лет."
    # Приветствие до первой реплики не сверяется; ожидание относится к реплике перед ним.
    assert [(s.reference, s.expected) for s in trimmed.script] == [
        ("Уточните площадь.", ""),
        ("", "предложение передать продажнику"),
        ("", ""),
    ]
    assert trimmed.checks == ("Не обещает скидку, корректно переводит на менеджера.", "Сохраняет спокойный тон.")

    (merged,) = merge(parse(SCRIPTED), parse(FULL))
    assert [(s.reference, s.expected) for s in merged.script] == [
        ("Уточните площадь.", ""),
        ("Обещаю скидку 10 %.", "предложение передать продажнику"),
        ("", ""),
    ]
    assert [s.number for s in merge(parse(SAMPLE), parse(SCRIPTED))] == [1, 7, 8]
    with pytest.raises(ValueError, match="Сценарий 1 есть в двух файлах"):
        merge(parse(SCRIPTED), parse(SCRIPTED.replace("без мебели.", "с мебелью.")))


def test_fifty_customer_scenarios_of_both_files_merge_into_one_run():
    folder = Path(__file__).parent / "scenarios"
    full = parse((folder / "vdm_50_scenarios_full.md").read_text(encoding="utf-8"))
    trimmed = parse((folder / "vdm_50_scenarios_autotest_trimmed.md").read_text(encoding="utf-8"))

    merged = merge(trimmed, full)

    assert [scenario.number for scenario in merged] == list(range(1, 51))
    assert all(len(s.script) == 11 and s.goal and s.client_type and len(s.checks) == 8 for s in merged)
    assert all(step.reference for scenario in merged for step in scenario.script)
    # Вырезано 214 ответов, из них 50 приветствий — ожидания остаются у 164 реплик.
    assert sum(1 for scenario in merged for step in scenario.script if step.expected) == 164


def move(text: str, hold: bool = False, action: str = "text") -> str:
    """Ответ тестировщика-модели одной строкой JSON."""
    return json.dumps({"action": action, "text": text, "hold": hold, "reason": "по плану"}, ensure_ascii=False)


def test_the_tester_follows_the_plan_but_writes_its_own_lines():
    """Сценарий «Ход N» — план разговора, а не текст: реплики пишет тестировщик по ответам бота."""
    (scenario,) = merge(parse(SCRIPTED), parse(FULL))
    main = variants(scenario, "main")[0]
    chat = FakeChat(["Уточните площадь"], ["Передам продажнику"], ["Храню корзину"])
    tester = FakeModel(
        move("шесть групп, 3–7 лет, помогите подобрать"),
        move("мебель не нужна, она уже есть"),
        move("а что у вас хранится обо мне?"),
        move("", action="end"),
    )
    judge_model = FakeModel('{"score": 4, "goal_reached": true, "context_kept": true, "summary": "ок"}')

    result = asyncio.run(play(chat, tester, CatalogFacts([], ()), scenario, main, 2, judge_model))

    assert chat.sent == ["шесть групп, 3–7 лет, помогите подобрать", "мебель не нужна, она уже есть", "а что у вас хранится обо мне?"]
    assert result.error is None and result.verdict.score == 4
    # План виден тестировщику: что уже поднято, что поднимаем сейчас.
    second = tester.requests[1][1]["content"]
    assert "✓ Оснастить 6 групп, возраст 3-7 лет." in second and "→ сейчас: без мебели." in second
    assert "Чего ждём от бота в ответ: предложение передать продажнику" in second
    assert "Уточните площадь" in second, "тестировщик видит последний ответ бота"
    # Судья знает, что реплики живые, а ходы файла — план.
    prompt = judge_model.requests[0][1]["content"]
    assert "ход 2. План клиента: без мебели." in prompt and "ждём от бота: предложение передать продажнику" in prompt
    assert "эталонный ответ: Обещаю скидку 10 %." in prompt and "- Сохраняет спокойный тон." in prompt


def test_a_clarifying_turn_does_not_eat_a_step_of_the_plan():
    """«Вы мне ещё ничего не показали» — ход потрачен, пункт плана остался."""
    (scenario,) = merge(parse(SCRIPTED), parse(FULL))
    main = variants(scenario, "main")[0]
    chat = FakeChat(["Уточните площадь"], ["45 метров?"], ["Передам продажнику"], ["Храню корзину"])
    tester = FakeModel(
        move("шесть групп, 3–7 лет"),
        move("вы не ответили, что с мебелью", hold=True),
        move("мебель не нужна"),
        move("что у вас обо мне хранится?"),
        move("", action="end"),
    )

    result = asyncio.run(play(chat, tester, CatalogFacts([], ()), scenario, main, 2, FakeModel('{"score": 3}')))

    assert [turn.text for turn in result.turns] == [
        "шесть групп, 3–7 лет",
        "вы не ответили, что с мебелью",
        "мебель не нужна",
        "что у вас обо мне хранится?",
    ]
    held = tester.requests[2][1]["content"]
    assert "→ сейчас: без мебели." in held, "после hold план остался на том же пункте"


def test_the_tester_may_press_a_button_in_a_turn_by_turn_scenario():
    (scenario,) = merge(parse(SCRIPTED), parse(FULL))
    main = variants(scenario, "main")[0]
    chat = FakeChat(["Уточните площадь"], ["Корзина"], ["Храню корзину"])
    tester = FakeModel(move("шесть групп"), move("Оформить", action="button"), move("", action="end"))

    asyncio.run(play(chat, tester, CatalogFacts([], ()), scenario, main, 2, FakeModel('{"score": 3}')))

    assert chat.sent[1] == "[кнопка] Оформить" or chat.sent[1] == "Оформить", chat.sent


def test_the_bot_going_silent_ends_the_dialog():
    (scenario,) = merge(parse(SCRIPTED), parse(FULL))
    main = variants(scenario, "main")[0]
    silent = FakeChat([], [], ["поздно"])
    tester = FakeModel(move("шесть групп"), move("без мебели"), move("/my_data"))

    result = asyncio.run(play(silent, tester, CatalogFacts([], ()), scenario, main, 8, FakeModel('{"score": 1}')))

    assert silent.sent == ["шесть групп", "без мебели"]
    assert result.end_reason == "бот молчит два хода подряд" and len(result.turns) == 2


def test_discount_and_free_delivery_promises_are_flagged():
    facts = CatalogFacts([], ())
    promised = _turn("Обычно менеджер даёт дополнительную скидку 5–10%, точную цифру назовёт он.")
    assert [(f.code, f.severity) for f in check_turn(promised, facts, [])] == [("PROMISE", "error")]
    delivery = _turn("При заказе от 100 000 доставка по Москве бесплатна.")
    assert [(f.code, f.severity) for f in check_turn(delivery, facts, [])] == [("PROMISE", "warning")]
    assert check_turn(_turn("Скидку и условия доставки назовёт менеджер."), facts, []) == []
