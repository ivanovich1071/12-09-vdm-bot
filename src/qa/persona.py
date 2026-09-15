"""Тестировщик-«клиент»: следующая реплика или нажатие кнопки по сценарию и разговору."""

from __future__ import annotations

from dataclasses import dataclass

from qa.llm import Model, ask_json
from qa.models import Turn, transcript
from qa.scenarios import Scenario, Variant

# Что делать в ветке — словами клиента. Неизвестная ветка берёт своё название и ожидаемый ответ.
BRANCH_HINTS = {
    "цена": "спроси цену конкретной показанной позиции или всего набора",
    "строго по приказу": "потребуй, чтобы всё было строго по приказу, и спроси номера пунктов перечня",
    "нужен аналог": "попроси аналог или замену одной из показанных позиций",
    "нужно срочно": "скажи, что нужно срочно, назови город и дату поставки",
}
# Сбросить разговор посреди сценария тестировщику нельзя: прогон потеряет всё собранное.
FORBIDDEN_BUTTONS = ("да, начать заново", "начать заново")
HISTORY_TURNS = 10

SYSTEM = """Ты — тестировщик. Ты играешь живого клиента в Telegram-чате с ботом магазина ЭЛТИ-КУДИЦ (vdm.ru): \
оборудование и игрушки для детских садов и школ. Бот умеет консультировать по приказам № 1057 (сады), № 838 (школы), \
ФГОС и ФОП ДО и подбирать товары из каталога: карточки, списки, корзина, файл Excel/Word, передача менеджеру.

Как писать:
- как реальный человек в мессенджере: одно-два коротких предложения, разговорно, без markdown; иногда со строчной \
буквы или с небольшой опечаткой;
- не говори, что ты тест или бот, и не пересказывай сценарий дословно;
- детали — возраст детей, их число, бюджет, город, срок — придумай правдоподобно, когда бот спросит, и дальше \
держись их;
- отвечай на вопросы бота; если бот уводит в сторону, повторяется или отвечает не на то — реагируй как недовольный \
клиент, но от цели не отказывайся;
- кнопку нажимай, только если она есть под последним ответом бота и подходит по смыслу («Показать ещё», \
«Всё в корзину», «Скачать Excel», «Подробнее»); «Начать заново» не нажимай;
- заканчивай (end), когда цель достигнута — показаны подходящие товары с ценами, собран список, файл или корзина, \
заявка передана менеджеру — или когда бот два хода подряд не продвинулся.

Ответ — только JSON, без пояснений вокруг:
{"action": "text" | "button" | "end", "text": "реплика или точная надпись кнопки", "reason": "зачем, 3–8 слов"}"""


@dataclass(frozen=True)
class Move:
    kind: str  # text | button | end
    text: str = ""
    reason: str = ""


def next_move(model: Model, scenario: Scenario, variant: Variant, turns: list[Turn], number: int, max_turns: int) -> Move:
    data = ask_json(model, SYSTEM, brief(scenario, variant, turns, number, max_turns), temperature=0.8, max_tokens=400)
    return move_from(data, recent_buttons(turns))


def recent_buttons(turns: list[Turn], depth: int = 3) -> list[str]:
    """Кнопки последних ответов бота, свежие первыми.

    Под сообщением с файлом кнопок нет, а «Скачать Word» двумя сообщениями выше нажать можно. Ночью 14.09
    тестировщик печатал такие надписи текстом, бот переспрашивал формат, и проверка насчитала 32 «повтора».
    """
    labels: list[str] = []
    for turn in reversed([turn for turn in turns if turn.messages][-depth:]):
        for message in reversed(turn.messages):
            labels += [label for label in message.buttons if label not in labels]
    return labels


def brief(scenario: Scenario, variant: Variant, turns: list[Turn], number: int, max_turns: int) -> str:
    lines = [
        f"Ты: {scenario.role}" + (f" ({scenario.client_type})" if scenario.client_type else ""),
        f"Цель разговора: {scenario.goal}",
    ]
    if scenario.category:
        lines.append(f"Категория товаров: {scenario.category}")
    if scenario.route:
        lines.append(f"Ожидаемый маршрут: {scenario.route}")
    if scenario.first_message:
        lines.append(f"Первая реплика по сценарию — перескажи своими словами: «{scenario.first_message}»")
    if scenario.required_fields:
        lines.append(f"Что бот должен у тебя выяснить: {', '.join(scenario.required_fields)}")
    if variant.branch is None:
        lines.append("Вариант: основной путь — доведи разговор до подбора товаров и корзины, списка или файла.")
    else:
        hint = BRANCH_HINTS.get(variant.branch.name.lower(), f"проверь ситуацию «{variant.branch.name}»")
        lines.append(
            f"Вариант «{variant.branch.name}»: не в первой реплике, а по ходу разговора {hint}. "
            f"Ожидаемая реакция бота: «{variant.branch.expected}»."
        )
    lines.append(f"Ход {number} из {max_turns}." + (" Это последний ход." if number >= max_turns else ""))
    lines += ["", "Разговор до сих пор:", transcript(turns[-HISTORY_TURNS:]) or "(ещё не начат — напиши первую реплику)"]
    return "\n".join(lines)


def move_from(data: dict, buttons: list[str]) -> Move:
    action = str(data.get("action") or "").strip().lower()
    text = str(data.get("text") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if action == "end" or not text:
        return Move("end", "", reason or "тестировщик закончил разговор")
    if action == "button":
        if text.lower().strip("«»\" ") in FORBIDDEN_BUTTONS:
            return Move("end", "", "тестировщик хотел начать заново")
        label = match_button(text, buttons)
        # Надписи нет под сообщением — это кнопка нижней клавиатуры или выдумка: уходит текстом.
        return Move("button", label, reason) if label else Move("text", text.strip("«»\""), reason)
    return Move("text", text, reason)


def last_buttons(turns: list[Turn]) -> list[str]:
    for turn in reversed(turns):
        labels = [label for message in turn.messages for label in message.buttons]
        if turn.messages:
            return labels
    return []


def match_button(text: str, buttons: list[str]) -> str | None:
    wanted = text.lower().strip("«»\" ")
    for label in buttons:
        if label.lower() == wanted:
            return label
    for label in buttons:
        if wanted and (label.lower().startswith(wanted) or wanted.startswith(label.lower())):
            return label
    return None
