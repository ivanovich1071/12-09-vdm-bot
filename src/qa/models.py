"""Стенограмма прогона: сообщения бота, ходы, замечания, оценка судьи. Всё сериализуется в JSONL."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Finding:
    code: str
    severity: str  # error | warning
    text: str


@dataclass
class BotMessage:
    id: int
    text: str = ""
    buttons: list[str] = field(default_factory=list)
    file: str | None = None
    file_size: int | None = None
    photo: bool = False
    edited: bool = False
    # Пришло уже после того, как ход закрылся по тишине, — дописано к нему при следующем действии.
    late: bool = False


@dataclass
class Turn:
    number: int
    kind: str  # text | button
    text: str
    reason: str = ""
    # Секунды до первого ответа бота.
    seconds: float = 0.0
    timed_out: bool = False
    messages: list[BotMessage] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


@dataclass
class Verdict:
    score: int = 0
    goal_reached: bool | None = None
    context_kept: bool | None = None
    branch_handled: bool | None = None
    problems: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    error: str | None = None


@dataclass
class DialogResult:
    scenario: int
    title: str
    role: str
    goal: str
    variant: str
    started: str
    seconds: float = 0.0
    turns: list[Turn] = field(default_factory=list)
    verdict: Verdict = field(default_factory=Verdict)
    end_reason: str = ""
    error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.scenario}:{self.variant}"

    @property
    def findings(self) -> list[Finding]:
        return [finding for turn in self.turns for finding in turn.findings]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DialogResult:
        data = dict(raw)
        data["turns"] = [
            Turn(
                **{
                    **turn,
                    "messages": [BotMessage(**message) for message in turn.get("messages", [])],
                    "findings": [Finding(**finding) for finding in turn.get("findings", [])],
                }
            )
            for turn in data.get("turns", [])
        ]
        data["verdict"] = Verdict(**(data.get("verdict") or {}))
        return cls(**data)


def transcript(turns: list[Turn], limit: int = 1500) -> str:
    """Разговор текстом — для тестировщика и судьи."""
    lines: list[str] = []
    for turn in turns:
        said = f"нажал кнопку «{turn.text}»" if turn.kind == "button" else turn.text
        lines.append(f"[ход {turn.number}] Клиент: {said}")
        if not turn.messages:
            lines.append(f"Бот: (нет ответа за {turn.seconds:.0f} с)")
        for message in turn.messages:
            text = message.text if len(message.text) <= limit else message.text[:limit] + " …(обрезано)"
            extra = []
            if message.photo:
                extra.append("фото")
            if message.file:
                extra.append(f"файл {message.file}")
            if message.buttons:
                extra.append("кнопки: " + " | ".join(message.buttons))
            lines.append(f"Бот: {text}" + (f" [{'; '.join(extra)}]" if extra else ""))
    return "\n".join(lines)
