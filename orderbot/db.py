"""SQLite-хранилище: сессия аккаунта, чёрные списки, дедуп, находки, статистика."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from .utils import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bl_users (
    user_id  INTEGER PRIMARY KEY,
    label    TEXT NOT NULL DEFAULT '',
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bl_chats (
    chat_id  INTEGER PRIMARY KEY,
    label    TEXT NOT NULL DEFAULT '',
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS seen (
    hash       TEXT PRIMARY KEY,
    simhash    INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_created ON seen(created_at);

CREATE TABLE IF NOT EXISTS hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    chat_title  TEXT NOT NULL DEFAULT '',
    msg_id      INTEGER NOT NULL,
    author_id   INTEGER,
    author_name TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 0,
    category    TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    api_id     INTEGER NOT NULL,
    api_hash   TEXT NOT NULL,
    phone      TEXT NOT NULL DEFAULT '',
    session    TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    username   TEXT NOT NULL DEFAULT '',
    user_id    INTEGER UNIQUE,
    disabled   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    added_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT NOT NULL,
    email          TEXT NOT NULL,
    password       TEXT NOT NULL,
    added_at       INTEGER NOT NULL,
    disabled       INTEGER NOT NULL DEFAULT 0,
    cooldown_until INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT '',
    uses           INTEGER NOT NULL DEFAULT 0,
    UNIQUE(provider, email)
);

CREATE TABLE IF NOT EXISTS stats (
    day     TEXT NOT NULL,
    metric  TEXT NOT NULL,
    value   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, metric)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.init() не был вызван")
        return self._db

    # ------------------------------------------------------------------ settings

    async def get(self, key: str, default: Any = None) -> Any:
        async with self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    async def set(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await self.db.commit()

    async def delete(self, key: str) -> None:
        await self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await self.db.commit()

    # ----------------------------------------------------------------- blacklist

    async def ban_user(self, user_id: int, label: str = "") -> None:
        await self.db.execute(
            "INSERT INTO bl_users(user_id, label, added_at) VALUES(?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET label = excluded.label",
            (user_id, label, now()),
        )
        await self.db.commit()

    async def ban_chat(self, chat_id: int, label: str = "") -> None:
        await self.db.execute(
            "INSERT INTO bl_chats(chat_id, label, added_at) VALUES(?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET label = excluded.label",
            (chat_id, label, now()),
        )
        await self.db.commit()

    async def unban_user(self, user_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM bl_users WHERE user_id = ?", (user_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def unban_chat(self, chat_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM bl_chats WHERE chat_id = ?", (chat_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def banned_users(self) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT user_id, label FROM bl_users ORDER BY added_at DESC"
        ) as cur:
            return list(await cur.fetchall())

    async def banned_chats(self) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT chat_id, label FROM bl_chats ORDER BY added_at DESC"
        ) as cur:
            return list(await cur.fetchall())

    async def load_blacklists(self) -> tuple[set[int], set[int]]:
        """Читает оба ЧС целиком — держим их в памяти для быстрой фильтрации."""
        users = {row["user_id"] for row in await self.banned_users()}
        chats = {row["chat_id"] for row in await self.banned_chats()}
        return users, chats

    # --------------------------------------------------------------------- dedup

    async def seen_load(self, ttl_days: int) -> list[tuple[str, int]]:
        cutoff = now() - ttl_days * 86400
        async with self.db.execute(
            "SELECT hash, simhash FROM seen WHERE created_at >= ?", (cutoff,)
        ) as cur:
            return [(row["hash"], _from_sqlite_int(row["simhash"]))
                    for row in await cur.fetchall()]

    async def seen_add(self, text_hash: str, sim: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO seen(hash, simhash, created_at) VALUES(?, ?, ?)",
            (text_hash, _to_sqlite_int(sim), now()),
        )
        await self.db.commit()

    async def seen_cleanup(self, ttl_days: int) -> int:
        cutoff = now() - ttl_days * 86400
        cur = await self.db.execute("DELETE FROM seen WHERE created_at < ?", (cutoff,))
        await self.db.commit()
        return cur.rowcount

    # ---------------------------------------------------------------------- hits

    async def add_hit(
        self,
        *,
        chat_id: int,
        chat_title: str,
        msg_id: int,
        author_id: int | None,
        author_name: str,
        text: str,
        confidence: float,
        category: str,
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO hits(chat_id, chat_title, msg_id, author_id, author_name, "
            "text, confidence, category, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, chat_title, msg_id, author_id, author_name, text,
             confidence, category, now()),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def get_hit(self, hit_id: int) -> aiosqlite.Row | None:
        async with self.db.execute("SELECT * FROM hits WHERE id = ?", (hit_id,)) as cur:
            return await cur.fetchone()

    # --------------------------------------------------------- телеграм-аккаунты

    async def tg_account_add(self, api_id: int, api_hash: str, phone: str,
                             session: str, label: str, username: str,
                             user_id: int | None) -> int:
        """Добавляет или обновляет аккаунт. Ключ — user_id, повторно не заведётся."""
        cur = await self.db.execute(
            "INSERT INTO tg_accounts(api_id, api_hash, phone, session, label, "
            "username, user_id, added_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET api_id = excluded.api_id, "
            "api_hash = excluded.api_hash, phone = excluded.phone, "
            "session = excluded.session, label = excluded.label, "
            "username = excluded.username, disabled = 0, last_error = ''",
            (api_id, api_hash, phone, session, label, username, user_id, now()),
        )
        await self.db.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        async with self.db.execute(
            "SELECT id FROM tg_accounts WHERE user_id = ?", (user_id,)
        ) as c:
            row = await c.fetchone()
        return int(row["id"]) if row else 0

    async def tg_accounts(self) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM tg_accounts ORDER BY added_at"
        ) as cur:
            return list(await cur.fetchall())

    async def tg_account_delete(self, account_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM tg_accounts WHERE id = ?", (account_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def tg_account_toggle(self, account_id: int) -> bool:
        await self.db.execute(
            "UPDATE tg_accounts SET disabled = 1 - disabled WHERE id = ?", (account_id,)
        )
        await self.db.commit()
        async with self.db.execute(
            "SELECT disabled FROM tg_accounts WHERE id = ?", (account_id,)
        ) as cur:
            row = await cur.fetchone()
        return bool(row["disabled"]) if row else False

    async def tg_account_error(self, account_id: int, error: str) -> None:
        await self.db.execute(
            "UPDATE tg_accounts SET last_error = ? WHERE id = ?", (error, account_id)
        )
        await self.db.commit()

    # -------------------------------------------------------------- аккаунты LLM

    async def llm_account_add(self, provider: str, email: str, password: str) -> int:
        cur = await self.db.execute(
            "INSERT INTO llm_accounts(provider, email, password, added_at) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(provider, email) DO UPDATE SET "
            "password = excluded.password, disabled = 0, cooldown_until = 0, last_error = ''",
            (provider, email, password, now()),
        )
        await self.db.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        async with self.db.execute(
            "SELECT id FROM llm_accounts WHERE provider = ? AND email = ?",
            (provider, email),
        ) as c:
            row = await c.fetchone()
        return int(row["id"]) if row else 0

    async def llm_accounts(self) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM llm_accounts ORDER BY added_at"
        ) as cur:
            return list(await cur.fetchall())

    async def llm_account(self, account_id: int) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM llm_accounts WHERE id = ?", (account_id,)
        ) as cur:
            return await cur.fetchone()

    async def llm_account_delete(self, account_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM llm_accounts WHERE id = ?", (account_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def llm_account_toggle(self, account_id: int) -> bool:
        """Включает/выключает аккаунт. Возвращает новое состояние disabled."""
        await self.db.execute(
            "UPDATE llm_accounts SET disabled = 1 - disabled, cooldown_until = 0 "
            "WHERE id = ?", (account_id,),
        )
        await self.db.commit()
        row = await self.llm_account(account_id)
        return bool(row["disabled"]) if row else False

    async def llm_account_fail(self, account_id: int, cooldown_until: int,
                               error: str) -> None:
        await self.db.execute(
            "UPDATE llm_accounts SET cooldown_until = ?, last_error = ? WHERE id = ?",
            (cooldown_until, error, account_id),
        )
        await self.db.commit()

    async def llm_account_used(self, account_id: int) -> None:
        await self.db.execute(
            "UPDATE llm_accounts SET uses = uses + 1, last_error = '', cooldown_until = 0 "
            "WHERE id = ?", (account_id,),
        )
        await self.db.commit()

    # --------------------------------------------------------------------- stats

    async def bump(self, metric: str, value: int = 1) -> None:
        day = _today()
        await self.db.execute(
            "INSERT INTO stats(day, metric, value) VALUES(?, ?, ?) "
            "ON CONFLICT(day, metric) DO UPDATE SET value = value + excluded.value",
            (day, metric, value),
        )
        await self.db.commit()

    async def stats_today(self) -> dict[str, int]:
        async with self.db.execute(
            "SELECT metric, value FROM stats WHERE day = ?", (_today(),)
        ) as cur:
            return {row["metric"]: row["value"] for row in await cur.fetchall()}

    async def stats_total(self) -> dict[str, int]:
        async with self.db.execute(
            "SELECT metric, SUM(value) AS value FROM stats GROUP BY metric"
        ) as cur:
            return {row["metric"]: row["value"] for row in await cur.fetchall()}


_UINT64 = 1 << 64
_INT64_MAX = 1 << 63


def _to_sqlite_int(value: int) -> int:
    """simhash беззнаковый, а INTEGER в SQLite — знаковый 64-битный.

    Без этой свёртки старшие значения дают OverflowError при вставке.
    """
    value &= _UINT64 - 1
    return value - _UINT64 if value >= _INT64_MAX else value


def _from_sqlite_int(value: int) -> int:
    return value + _UINT64 if value < 0 else value


def _today() -> str:
    import time as _time

    return _time.strftime("%Y-%m-%d", _time.localtime())
