"""Рост файлов состояния на диске должен быть ограничен.

Демон каждый цикл пишет в state.db мета-поля (время синхронизации, ошибки), поэтому
вопрос «не переполнит ли он диск» сводится к поведению SQLite в WAL-режиме. Проверяем
ровно это: на тысячах транзакций файл БД и журнал WAL должны выйти на плато, а не расти
линейно. Тест идёт секунды и ловит регрессию вида «кто-то добавил таблицу-лог, которая
растёт на каждом цикле».
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from xray_client_balancer.database import StateStore

TAGS = ["client-balancer-1", "client-balancer-2", "client-balancer-3"]


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def test_state_db_and_wal_stay_bounded(tmp_path: Path) -> None:
    database = str(tmp_path / "state.db")
    store = StateStore(database)
    try:
        # как на узле: 150 клиентов sticky-раскладкой
        store.apply_changes(
            {client_id: TAGS[client_id % len(TAGS)] for client_id in range(1, 151)},
            {client_id: f"user{client_id}@example" for client_id in range(1, 151)},
            [],
        )
        samples: list[tuple[int, int, int]] = []
        for cycle in range(1, 3001):
            # ровно то, что делает sync() в установившемся режиме: два set_meta
            store.set_meta("last_successful_sync", str(1700000000 + cycle))
            store.set_meta("last_error", "")
            if cycle % 200 == 0:
                samples.append(
                    (cycle, _size(database), _size(f"{database}-wal"))
                )
    finally:
        store.close()

    db_size, wal_size = _size(database), _size(f"{database}-wal")
    print("цикл  state.db  WAL")
    for cycle, db, wal in samples:
        print(f"{cycle:>5} {db:>9} {wal:>9}")
    print(f"итог: state.db={db_size} WAL={wal_size}")

    # БД: 150 строк + метаданные — это единицы страниц, не мегабайты
    assert db_size < 1024 * 1024, f"state.db вырос до {db_size} байт"
    # WAL самоограничивается checkpoint'ом SQLite (~1000 страниц ≈ 4 МБ);
    # линейный рост на 3000 транзакций дал бы десятки мегабайт
    assert wal_size < 8 * 1024 * 1024, f"WAL вырос до {wal_size} байт"

    # ни одна из 3000 записей метаданных не превратилась в новую строку
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = int(connection.execute("SELECT COUNT(*) FROM meta").fetchone()[0])
    finally:
        connection.close()
    assert rows <= 20, f"таблица meta разрослась: {rows} строк"

    store = StateStore(database)
    try:
        assignments = len(store.load_assignments())
    finally:
        store.close()
    assert assignments == 150
