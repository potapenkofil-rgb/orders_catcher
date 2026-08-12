"""Разделяемое состояние: ЧС, пауза, порог уверенности.

Держим в памяти, чтобы фильтрация на горячем пути не ходила в базу,
и синхронно пишем в SQLite, чтобы состояние переживало рестарт.
"""

from __future__ import annotations

from .config import Config
from .db import Database
from .utils import now


class Runtime:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.paused = False
        self.banned_users: set[int] = set()
        self.banned_chats: set[int] = set()
        self.min_confidence = cfg.min_confidence
        self.profile = cfg.profile
        self.owner_id: int | None = cfg.owner_id
        self.started_at = now()

    async def load(self) -> None:
        self.banned_users, self.banned_chats = await self.db.load_blacklists()
        self.paused = bool(await self.db.get("paused", False))
        self.min_confidence = float(await self.db.get("min_confidence", self.cfg.min_confidence))
        self.profile = str(await self.db.get("profile", self.cfg.profile))
        self.cfg.profile = self.profile          # классификатор читает профиль из конфига
        stored_owner = await self.db.get("owner_id")
        # OWNER_ID из окружения имеет приоритет над сохранённым.
        if self.owner_id is None and stored_owner:
            self.owner_id = int(stored_owner)

    # ---------------------------------------------------------------- владелец

    async def bind_owner(self, user_id: int) -> None:
        self.owner_id = user_id
        await self.db.set("owner_id", user_id)

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and self.owner_id == user_id

    # ------------------------------------------------------------------- пауза

    async def set_paused(self, value: bool) -> None:
        self.paused = value
        await self.db.set("paused", value)

    # -------------------------------------------------------------------- порог

    async def set_min_confidence(self, value: float) -> None:
        self.min_confidence = max(0.0, min(1.0, value))
        await self.db.set("min_confidence", self.min_confidence)

    async def set_profile(self, value: str) -> None:
        self.profile = value.strip()
        self.cfg.profile = self.profile
        await self.db.set("profile", self.profile)

    # ---------------------------------------------------------------------- ЧС

    async def ban_user(self, user_id: int, label: str = "") -> None:
        self.banned_users.add(user_id)
        await self.db.ban_user(user_id, label)

    async def ban_chat(self, chat_id: int, label: str = "") -> None:
        self.banned_chats.add(chat_id)
        await self.db.ban_chat(chat_id, label)

    async def unban_user(self, user_id: int) -> bool:
        self.banned_users.discard(user_id)
        return await self.db.unban_user(user_id)

    async def unban_chat(self, chat_id: int) -> bool:
        self.banned_chats.discard(chat_id)
        return await self.db.unban_chat(chat_id)

    def is_banned(self, chat_id: int, user_id: int | None) -> bool:
        return chat_id in self.banned_chats or (
            user_id is not None and user_id in self.banned_users
        )
