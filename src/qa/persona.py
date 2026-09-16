"""Тестировщик-«тайный покупатель»: следующая реплика или нажатие кнопки по плану и по разговору.

Сценарии «Ход N» (15.09) отправлялись боту как написаны, без модели. Прогон 16.09 показал, чем это
кончается: реплики не вяжутся с ответами («покажите первые три позиции из раздела», когда раздел ещё
не выбран), в файле заказчика попадаются склеенные строки («у меня бюджет вы не менеджер по продажам?»)
и вопросы, которые по смыслу задаёт бот («что приоритетно: минимальный бюджет или…»), — а клиент читал
их вслух. Бот на такой разговор отвечает вопросами по кругу, и судья оценивает не бота, а качество файла
сценариев. Кнопки в этом режиме не нажимались вовсе: ход всегда уходил текстом.

Теперь план сценария — это план, а не текст: что клиент поднимает на этом ходе. Реплику тестировщик
пишет сам, глядя на последний ответ бота: отвечает на заданный вопрос, жмёт кнопки, возражает, когда
бот не сделал обещанного, и соглашается, когда сделал. `hold` — «пункт плана не отработан, оставь его
на следующий ход»: так уточнение или возражение не съедает пункт плана.
"""

from __future__ import annotations

from dataclasses import dataclass

from qa.llm import Model, ask_json
from qa.models import Turn, transcript
from qa.scenarios import Scenario, Step, Variant

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
# Сколько ходов подряд один пункт плана можно держать: дальше разговор ходит по кругу.
HOLD_LIMIT = 2

SYSTEM = """Ты — тайный покупатель. Ты пишешь в Telegram боту магазина ЭЛТИ-КУДИЦ (vdm.ru): оборудование и игрушки \
для детских садов и школ. Бот консультирует по приказам № 1057 (сады), № 838 (школы), ФГОС и ФОП ДО и подбирает \
товары из каталога: карточки, списки, корзина, файл Excel/Word, передача менеджеру. Ты проверяешь, как он работает, \
но ведёшь себя как обычный клиент со своей задачей.

Главное правило: ты отвечаешь на то, что бот сказал только что. План разговора — это твои намерения, а не текст: \
каждую реплику ты пишешь сам, своими словами, с поправкой на ответ бота.

Как вести разговор:
- бот задал вопрос — ответь на него; ответ и следующий пункт плана можно уместить в одну короткую реплику \
(«18 детей, зал 45 метров — покажите первые три позиции»);
- бот не сделал того, что ты просил, — скажи это как клиент («вы мне ещё ничего не показали», «я просил список») \
и поставь "hold": true, чтобы пункт плана остался на следующий ход;
- в плане иногда стоит вопрос, который по смыслу задаёт бот («что приоритетно: бюджет или полное закрытие?») — \
не повторяй его за ботом, а ответь на него как клиент;
- план не вяжется с разговором — действуй по цели, а не по букве плана;
- в плане стоит команда бота (со слэша: /my_data, /delete_data, /order) — отправь её ровно как написано;
- бот сделал, что просили, — согласись и иди дальше, не спорь ради спора;
- цена, пункт приказа, срок или скидка выглядят взятыми с потолка — переспроси, откуда это и кто подтвердит;
- детали — возраст детей, их число, площадь, бюджет, город, срок — придумай правдоподобно, когда бот спросит, \
и дальше держись их, не меняя на ходу;
- кнопку нажимай, если она есть под последними сообщениями бота и ведёт к цели («Показать ещё», «В корзину», \
«Оформить», «Скачать Excel», «Подробнее»); надпись кнопки текстом не печатай. «Начать заново» не нажимай;
- пиши как живой человек в мессенджере: одно-два коротких предложения, разговорно, без markdown, иногда со \
строчной буквы или с небольшой опечаткой;
- не говори, что ты тест или бот, и не пересказывай план дословно;
- заканчивай (end), когда цель достигнута — показаны подходящие товары с ценами, собран список, файл или корзина, \
заявка передана менеджеру — или когда бот два хода подряд не продвинулся.

Ответ — только JSON, без пояснений вокруг:
{"action": "text" | "button" | "end", "text": "реплика или точная надпись кнопки", "hold": true | false, \
"reason": "зачем, 3–8 слов"}"""


@dataclass(frozen=True)
class Plan:
    """План разговора по ходам и место в нём. Пустой — сценарий без «Ход N», тестировщик ведёт сам."""

    steps: tuple[Step, ...] = ()
    index: int = 0

    @property
    def current(self) -> Step | None:
        return self.steps[self.index] if self.index < len(self.steps) else None

    @property
    def done(self) -> bool:
        return bool(self.steps) and self.index >= len(self.steps)


@dataclass(frozen=True)
class Move:
    kind: str  # text | button | end
    text: str = ""
    reason: str = ""
    # Пункт плана остаётся на следующий ход: этот ход ушёл на уточнение или возражение.
    hold: bool = False


def next_move(
    model: Model,
    scenario: Scenario,
    variant: Variant,
    turns: list[Turn],
    number: int,
    max_turns: int,
    plan: Plan | None = None,
) -> Move:
    data = ask_json(
        model,
        SYSTEM,
        brief(scenario, variant, turns, number, max_turns, plan),
        temperature=0.8,
        max_tokens=400,
    )
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


def brief(
    scenario: Scenario,
    variant: Variant,
    turns: list[Turn],
    number: int,
    max_turns: int,
    plan: Plan | None = None,
) -> str:
    plan = plan or Plan()
    lines = [
        f"Ты: {scenario.role}" + (f" ({scenario.client_type})" if scenario.client_type else ""),
        f"Цель разговора: {scenario.goal}",
    ]
    if scenario.category:
        lines.append(f"Категория товаров: {scenario.category}")
    if scenario.route:
        lines.append(f"Ожидаемый маршрут: {scenario.route}")
    if scenario.first_message and not plan.steps:
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
    lines += plan_lines(plan)
    lines.append(f"Ход {number} из {max_turns}." + (" Это последний ход." if number >= max_turns else ""))
    lines += ["", "Разговор до сих пор:", transcript(turns[-HISTORY_TURNS:]) or "(ещё не начат — напиши первую реплику)"]
    return "\n".join(lines)


def plan_lines(plan: Plan) -> list[str]:
    """План разговора: что уже поднято, что поднимаем сейчас, что осталось."""
    if not plan.steps:
        return []
    lines = ["", "План разговора — твои намерения по ходам, говори своими словами:"]
    for position, step in enumerate(plan.steps):
        if position < plan.index:
            mark = "  ✓"
        elif position == plan.index:
            mark = "→ сейчас:"
        else:
            mark = "  потом:"
        lines.append(f"{mark} {step.client}")
    current = plan.current
    if current is not None and current.expected:
        lines.append(f"Чего ждём от бота в ответ: {current.expected}")
    if plan.done:
        lines.append("План пройден — доведи начатое до конца (корзина, файл или заявка) и заканчивай.")
    return lines


def move_from(data: dict, buttons: list[str]) -> Move:
    action = str(data.get("action") or "").strip().lower()
    text = str(data.get("text") or "").strip()
    reason = str(data.get("reason") or "").strip()
    hold = bool(data.get("hold"))
    if action == "end" or not text:
        return Move("end", "", reason or "тестировщик закончил разговор")
    if action == "button":
        if text.lower().strip("«»\" ") in FORBIDDEN_BUTTONS:
            return Move("end", "", "тестировщик хотел начать заново")
        label = match_button(text, buttons)
        # Надписи нет под сообщением — это кнопка нижней клавиатуры или выдумка: уходит текстом.
        if label:
            return Move("button", label, reason, hold)
        return Move("text", text.strip("«»\""), reason, hold)
    return Move("text", text, reason, hold)


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
