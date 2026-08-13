"""Слой LLM: чаты, бэкенды и маршрутизация между ними."""

from __future__ import annotations

from ..config import Config
from ..db import Database
from ..utils import log
from .accounts import PROVIDER_TITLES, PROVIDERS, Account, AccountsBackend
from .base import LLMBackend, LLMError
from .openai_compat import OpenAICompatBackend
from .session import ChatSession

__all__ = [
    "Account", "AccountsBackend", "ChatSession", "LLMBackend", "LLMError",
    "LLMRouter", "OpenAICompatBackend", "PROVIDERS", "PROVIDER_TITLES",
    "build_router",
]


class LLMRouter(LLMBackend):
    """Решает на каждом запросе, чем отвечать: аккаунтами или ключом.

    Выбор именно на каждом запросе, а не на старте: аккаунт можно добавить
    через бота в любой момент, и он подхватится без перезапуска. Если
    аккаунты кончились или все в кулдауне, а ключ есть — запрос уходит по ключу.
    """

    name = "router"

    def __init__(self, cfg: Config, accounts: AccountsBackend,
                 key: OpenAICompatBackend):
        self.cfg = cfg
        self.accounts = accounts
        self.key = key

    def _order(self) -> list[LLMBackend]:
        mode = self.cfg.llm_backend
        if mode == "key":
            return [self.key]
        if mode == "accounts":
            return [self.accounts]
        order: list[LLMBackend] = []
        if self.accounts.available:
            order.append(self.accounts)
        if self.key.available:
            order.append(self.key)
        # Ничего не готово — вернём аккаунты, чтобы наверх ушла их ошибка.
        return order or [self.accounts]

    @property
    def available(self) -> bool:
        return any(backend.available for backend in self._order())

    async def ask(self, session: ChatSession, system: str, user: str,
                  max_tokens: int) -> str:
        errors: list[str] = []
        for backend in self._order():
            try:
                return await backend.ask(session, system, user, max_tokens)
            except LLMError as exc:
                errors.append(f"{backend.name}: {exc}")
                log.warning("Бэкенд %s не ответил: %s", backend.name, exc)
        raise LLMError("; ".join(errors) or "нет доступного бэкенда")

    def status(self) -> str:
        parts = [self.accounts.status()]
        if self.key.available:
            parts.append(self.key.status())
        mode = {"auto": "авто", "key": "только ключ", "accounts": "только аккаунты"}
        return f"{mode.get(self.cfg.llm_backend, self.cfg.llm_backend)} · " + " | ".join(parts)

    async def close(self) -> None:
        await self.accounts.close()
        await self.key.close()


async def build_router(cfg: Config, db: Database) -> LLMRouter:
    accounts = AccountsBackend(cfg, db)
    await accounts.reload()
    return LLMRouter(cfg, accounts, OpenAICompatBackend(cfg))
