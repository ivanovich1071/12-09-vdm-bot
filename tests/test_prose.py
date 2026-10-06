"""Разбивка длинного ответа на короткие сообщения (вопрос 18 опросного листа)."""

from core.ui import MAX_PROSE_PARTS, PROSE_SPLIT_AT, split_prose


def test_short_text_stays_whole():
    assert split_prose("Короткий ответ.") == ["Короткий ответ."]
    assert split_prose("") == []


def test_long_text_splits_at_sentence_boundaries():
    sentence = "Фрезерный станок подойдёт для кабинета технологии и закрывает пункт перечня. "
    text = sentence * 12
    parts = split_prose(text)
    assert 2 <= len(parts) <= MAX_PROSE_PARTS
    assert all(len(part) < PROSE_SPLIT_AT * 2 for part in parts)
    # Предложение не оборвано: каждая часть кончается знаком конца фразы.
    assert all(part[-1] in ".!?…" for part in parts)
    assert " ".join(parts) == text.strip()


def test_text_without_boundaries_stays_whole():
    """Резать нечего — лучше одно длинное сообщение, чем обрыв на полуслове."""
    text = "слово" * 300
    assert len(split_prose(text)) == 1
