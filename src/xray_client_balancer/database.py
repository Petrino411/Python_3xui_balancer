"""SQLite-хранилище sticky-назначений (§17, §18).

Таблица client_assignments, ключ — внутренний ID клиента панели (стабильный при
смене email). Все изменения — в транзакции.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .models import Assignment

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS client_assignments (
    client_id    INTEGER PRIMARY KEY,
    email        TEXT NOT NULL,
    balancer_tag TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assignments_balancer ON client_assignments (balancer_tag);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class StateStore:
    """Обёртка над SQLite: назначения, метаданные, счётчики защит."""

    def __init__(self, path: str, *, create_dirs: bool = True) -> None:
        self.path = path
        if create_dirs and path not in (":memory:", ""):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        # executescript сам управляет транзакцией, поэтому без BEGIN/COMMIT
        self._conn.executescript(SCHEMA)
        current = self.get_meta("schema_version")
        if current is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            logger.info("База создана: %s (schema_version=%d)", self.path, SCHEMA_VERSION)
        elif current != str(SCHEMA_VERSION):
            raise RuntimeError(
                f"версия схемы БД {current} не поддерживается сервисом ({SCHEMA_VERSION})"
            )

    # ------------------------------------------------------------------ utils

    class _Tx:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def __enter__(self) -> sqlite3.Connection:
            self._conn.execute("BEGIN IMMEDIATE")
            return self._conn

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            if not self._conn.in_transaction:
                return
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")

    def transaction(self) -> "StateStore._Tx":
        return StateStore._Tx(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ assignments

    def load_assignments(self) -> dict[int, Assignment]:
        rows = self._conn.execute(
            "SELECT client_id, email, balancer_tag, created_at, updated_at FROM client_assignments"
        ).fetchall()
        return {
            int(r["client_id"]): Assignment(
                client_id=int(r["client_id"]),
                email=str(r["email"]),
                balancer_tag=str(r["balancer_tag"]),
                created_at=int(r["created_at"]),
                updated_at=int(r["updated_at"]),
            )
            for r in rows
        }

    def apply_changes(
        self,
        new_assignments: dict[int, str],
        email_updates: dict[int, str],
        removed_client_ids: Iterable[int],
        *,
        now: int | None = None,
    ) -> dict[str, int]:
        """Применить изменения назначений одной транзакцией. Возвращает статистику."""
        ts = int(now if now is not None else time.time())
        removed = list(removed_client_ids)
        stats = {"added": 0, "emails": 0, "removed": 0}
        with self.transaction() as conn:
            for client_id, tag in sorted(new_assignments.items()):
                email = email_updates.get(client_id)
                if email is None:
                    row = conn.execute(
                        "SELECT email FROM client_assignments WHERE client_id=?", (client_id,)
                    ).fetchone()
                    email = row["email"] if row else ""
                cur = conn.execute(
                    """
                    INSERT INTO client_assignments (client_id, email, balancer_tag, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(client_id) DO UPDATE SET
                        balancer_tag = excluded.balancer_tag,
                        email = excluded.email,
                        updated_at = excluded.updated_at
                    """,
                    (client_id, email, tag, ts, ts),
                )
                if cur.rowcount:
                    stats["added"] += 1
            for client_id, email in sorted(email_updates.items()):
                if client_id in new_assignments:
                    continue
                cur = conn.execute(
                    "UPDATE client_assignments SET email=?, updated_at=? WHERE client_id=? AND email<>?",
                    (email, ts, client_id, email),
                )
                stats["emails"] += cur.rowcount
            for client_id in sorted(set(removed)):
                cur = conn.execute(
                    "DELETE FROM client_assignments WHERE client_id=?", (client_id,)
                )
                stats["removed"] += cur.rowcount
        return stats

    def replace_all(self, assignments: dict[int, tuple[str, str]], *, now: int | None = None) -> None:
        """Полная замена таблицы назначений (используется командой rebalance)."""
        ts = int(now if now is not None else time.time())
        with self.transaction() as conn:
            conn.execute("DELETE FROM client_assignments")
            conn.executemany(
                "INSERT INTO client_assignments (client_id, email, balancer_tag, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (cid, email, tag, ts, ts)
                    for cid, (email, tag) in sorted(assignments.items())
                ],
            )

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def bump_meta_counter(self, key: str) -> int:
        value = int(self.get_meta(key) or "0") + 1
        self.set_meta(key, str(value))
        return value

    def reset_meta_counter(self, key: str) -> None:
        self.set_meta(key, "0")

    def meta(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM meta").fetchall()
        return {str(r["key"]): str(r["value"]) for r in rows}

    @property
    def database_size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0
