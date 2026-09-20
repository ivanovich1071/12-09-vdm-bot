"""Хранилище под несколькими потоками и несколькими процессами.

Ход диалога считается в потоке (`asyncio.to_thread`), апдейты Telegram идут
параллельными задачами, а виджет с Mini App — вообще отдельный процесс с тем
же файлом базы. До 20.09 соединение было одно и без замка: `execute` одного
потока и `commit` другого перемежались, и наружу это выходило безобидным
«Что-то пошло не так на моей стороне».
"""

from __future__ import annotations

import sqlite3
import threading

from core.models import Cart, CartItem
from core.storage import Storage


def item(sku: str) -> CartItem:
    return CartItem(sku_1c=sku, name=f"Товар {sku}", price=100, quantity=1)


def test_parallel_writes_do_not_lose_each_other(tmp_path):
    storage = Storage(tmp_path / "t.sqlite3")
    users = [f"u{n}" for n in range(24)]
    errors: list[BaseException] = []

    def work(user: str) -> None:
        try:
            for _ in range(10):
                storage.save_cart(Cart(user_id=user, items=[item("S1"), item("S2")]))
                storage.load_cart(user)
                storage.save_dialog_state(user, "telegram", [{"role": "user", "text": "мячи"}], {})
                storage.dialog_state_stamp(user, "telegram")
        except BaseException as exc:  # noqa: BLE001 — падение потока иначе не видно
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(user,)) for user in users]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    for user in users:
        assert storage.load_cart(user).count == 2


def test_second_process_can_read_while_the_first_writes(tmp_path):
    """WAL: читающему не нужно ждать пишущего.

    Бот и виджет — разные процессы над одним файлом. В журнале по умолчанию
    открытая запись блокирует чтение, и второй процесс ждёт до `busy_timeout`,
    а потом получает «database is locked».
    """
    path = tmp_path / "t.sqlite3"
    storage = Storage(path)
    storage.save_cart(Cart(user_id="u1", items=[item("S1")]))

    mode = sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"

    # Второе соединение — как из соседнего процесса.
    other = Storage(path)
    assert other.load_cart("u1").count == 1
    other.save_cart(Cart(user_id="u2", items=[item("S2")]))
    assert storage.load_cart("u2").count == 1


def test_deleting_a_subject_leaves_nothing_half_done(tmp_path):
    """Удаление по требованию субъекта — одним куском, а не по строке."""
    storage = Storage(tmp_path / "t.sqlite3")
    storage.save_cart(Cart(user_id="u1", items=[item("S1")]))
    storage.save_dialog_state("u1", "telegram", [{"role": "user", "text": "мячи"}], {})

    storage.delete_user_data("u1", "telegram")

    assert storage.load_cart("u1").is_empty
    assert storage.load_dialog_state("u1", "telegram") is None
