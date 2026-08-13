"""Бэкенд на залогиненных аккаунтах чат-нейросетей (DeepSeek, Qwen).

Логин по email + паролю, дальше проверка сообщений идёт через веб-API аккаунта,
а не через платный ключ. Аккаунтов может быть сколько угодно: они выбираются по
принципу «кто меньше работал», при ошибке или лимите аккаунт уходит в кулдаун,
а запрос повторяется на следующем.

Чат в таком API — настоящий диалог внутри аккаунта, он виден в списке чатов.
Поэтому чат один на этап и держится до смены (CHAT_TTL_HOURS), а системный
промпт уходит только первым сообщением — дальше модель помнит его сама.

Библиотеки лежат в orderbot/vendor (см. vendor/README.md) и импортируются
лениво: без аккаунтов их зависимости (requests, numpy, py_mini_racer) не нужны.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database
from ..utils import log, now, truncate
from .base import LLMBackend, LLMError
from .session import ChatSession

PROVIDERS = ("deepseek", "qwen")
PROVIDER_TITLES = {"deepseek": "DeepSeek", "qwen": "Qwen"}

_AUTH_HINTS = ("auth", "token", "401", "unauthorized", "логин", "пароль", "credential")
_LIMIT_HINTS = ("limit", "лимит", "429", "rate", "too many", "busy", "перегруж", "quota")


@dataclass
class Account:
    id: int
    provider: str
    email: str
    password: str
    disabled: bool = False
    cooldown_until: int = 0
    last_error: str = ""
    uses: int = 0

    @property
    def usable(self) -> bool:
        return not self.disabled and self.cooldown_until <= now()

    @property
    def title(self) -> str:
        return PROVIDER_TITLES.get(self.provider, self.provider)

    def state(self) -> str:
        if self.disabled:
            return "⛔ выключен"
        left = self.cooldown_until - now()
        if left > 0:
            return f"⏳ пауза {left // 60 + 1} мин: {truncate(self.last_error, 60)}"
        return f"✅ готов · {self.uses} запр."


class ProviderAdapter:
    """Синхронная обёртка над вендорной библиотекой. Работает в отдельном потоке."""

    provider = ""

    def make_client(self, account: Account, store_path: Path) -> Any:
        raise NotImplementedError

    def new_chat(self, client: Any) -> Any:
        return client.chats.new()

    def resume(self, client: Any, chat_id: str) -> Any:
        return client.chats.get(chat_id)

    def chat_id(self, chat: Any) -> str:
        return str(chat.id)

    def send(self, chat: Any, prompt: str) -> str:
        raise NotImplementedError


class DeepSeekAdapter(ProviderAdapter):
    provider = "deepseek"

    def make_client(self, account: Account, store_path: Path) -> Any:
        from ..vendor.betterdeepseek.client import DeepSeek
        from ..vendor.betterdeepseek.store import FileStore

        return DeepSeek(
            email=account.email,
            password=account.password,
            store=FileStore(str(store_path)),
            default_think=False,          # рассуждения только замедляют классификацию
            default_search=False,
        )

    def send(self, chat: Any, prompt: str) -> str:
        return chat.send(prompt, think=False, search=False)


class QwenAdapter(ProviderAdapter):
    provider = "qwen"

    def make_client(self, account: Account, store_path: Path) -> Any:
        from ..vendor.betterqwen.client import Qwen
        from ..vendor.betterqwen.store import FileStore

        return Qwen(
            email=account.email,
            password=account.password,
            store=FileStore(str(store_path)),
            default_thinking=False,
            default_search=False,
        )

    def send(self, chat: Any, prompt: str) -> str:
        return chat.send(prompt, thinking=False, search=False)


ADAPTERS: dict[str, ProviderAdapter] = {
    "deepseek": DeepSeekAdapter(),
    "qwen": QwenAdapter(),
}


class AccountsBackend(LLMBackend):
    name = "аккаунты"

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.accounts: list[Account] = []
        self.last_error = ""
        self._clients: dict[int, Any] = {}
        self._chats: dict[tuple[int, str], Any] = {}
        self._account_locks: dict[int, asyncio.Lock] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ аккаунты

    async def reload(self) -> None:
        rows = await self.db.llm_accounts()
        self.accounts = [
            Account(
                id=row["id"], provider=row["provider"], email=row["email"],
                password=row["password"], disabled=bool(row["disabled"]),
                cooldown_until=row["cooldown_until"], last_error=row["last_error"],
                uses=row["uses"],
            )
            for row in rows
        ]

    @property
    def available(self) -> bool:
        return any(a.usable for a in self.accounts)

    def status(self) -> str:
        if not self.accounts:
            return "аккаунты · ни одного не добавлено"
        ready = sum(1 for a in self.accounts if a.usable)
        return f"аккаунты · готовы {ready} из {len(self.accounts)}"

    def forget_client(self, account_id: int) -> None:
        """Сбросить залогиненный клиент — например, после удаления аккаунта."""
        self._clients.pop(account_id, None)
        for key in [k for k in self._chats if k[0] == account_id]:
            self._chats.pop(key, None)

    # ------------------------------------------------------------------ запрос

    async def ask(self, session: ChatSession, system: str, user: str,
                  max_tokens: int) -> str:
        await session.begin()
        lock = self._session_locks.setdefault(session.name, asyncio.Lock())
        # Внутри одного чата запросы идут строго по очереди: параллельная отправка
        # в один и тот же диалог сломала бы цепочку сообщений на сервере.
        async with lock:
            return await self._ask_locked(session, system, user)

    async def _ask_locked(self, session: ChatSession, system: str, user: str) -> str:
        errors: list[str] = []
        for _ in range(2):
            account = self._pick(session)
            if account is None:
                break
            account_lock = self._account_locks.setdefault(account.id, asyncio.Lock())
            async with account_lock:
                try:
                    text = await asyncio.to_thread(self._ask_sync, account, session,
                                                   system, user)
                except Exception as exc:                  # noqa: BLE001
                    errors.append(f"{account.email}: {exc}")
                    log.warning("Аккаунт %s не ответил: %s", account.email, exc)
                    await self._penalize(account, exc)
                    continue
            if text and text.strip():
                account.uses += 1
                await self.db.llm_account_used(account.id)
                self.last_error = ""
                return text.strip()
            errors.append(f"{account.email}: пустой ответ")
            await self._penalize(account, RuntimeError("пустой ответ"))

        self.last_error = "; ".join(errors) or "нет доступных аккаунтов"
        raise LLMError(self.last_error)

    def _pick(self, session: ChatSession) -> Account | None:
        """Аккаунт для этого чата: привязанный, иначе наименее загруженный."""
        bound = session.payload.get("account_id")
        if bound is not None:
            for account in self.accounts:
                if account.id == bound and account.usable:
                    return account
            self._unbind(session)                          # аккаунт отвалился

        usable = [a for a in self.accounts if a.usable]
        if not usable:
            return None
        account = min(usable, key=lambda a: a.uses)
        session.payload["account_id"] = account.id
        session.dirty = True
        return account

    @staticmethod
    def _unbind(session: ChatSession) -> None:
        # Карту «аккаунт → чат» не трогаем: аккаунт выйдет из кулдауна, снова
        # окажется выбранным — и продолжит свой прежний чат, а не заведёт новый.
        session.payload.pop("account_id", None)
        session.dirty = True

    def _ask_sync(self, account: Account, session: ChatSession,
                  system: str, user: str) -> str:
        """Синхронная часть: логин, чат, отправка. Крутится в отдельном потоке."""
        adapter = ADAPTERS[account.provider]
        client = self._client_for(account, adapter)

        # На каждый аккаунт в рамках одного чата-сессии — свой серверный чат.
        # Их максимум столько, сколько аккаунтов, и все они живут до смены чата.
        chats: dict = session.payload.setdefault("chats", {})
        greeted: dict = session.payload.setdefault("greeted", {})
        key = str(account.id)

        chat_id = chats.get(key)
        chat = self._chats.get((account.id, chat_id)) if chat_id else None
        if chat is None and chat_id:
            try:
                chat = adapter.resume(client, chat_id)     # после рестарта бота
                log.info("Продолжаю чат %s в аккаунте %s", chat_id, account.email)
            except Exception as exc:                       # noqa: BLE001
                log.warning("Чат %s не восстановился (%s) — создам новый", chat_id, exc)
                chat = None
                greeted.pop(key, None)
        if chat is None:
            chat = adapter.new_chat(client)
            chat_id = adapter.chat_id(chat)
            chats[key] = chat_id
            greeted[key] = False
            session.dirty = True
            log.info("Новый чат %s в аккаунте %s", chat_id, account.email)
        self._chats[(account.id, chat_id)] = chat

        # Системный промпт уходит только первым сообщением: дальше он уже
        # в контексте диалога, и повторять его в каждом запросе незачем.
        prompt = user if greeted.get(key) else f"{system}\n\n{user}"
        text = adapter.send(chat, prompt)
        greeted[key] = True
        return text

    def _client_for(self, account: Account, adapter: ProviderAdapter) -> Any:
        client = self._clients.get(account.id)
        if client is None:
            log.info("Логинюсь в %s как %s", account.title, account.email)
            client = adapter.make_client(account, self._store_path(account))
            self._clients[account.id] = client
        return client

    def _store_path(self, account: Account) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in account.email)
        path = Path(self.cfg.db_path).parent / "accounts" / f"{account.provider}-{safe}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def _penalize(self, account: Account, exc: BaseException) -> None:
        text = str(exc)
        low = f"{type(exc).__name__} {text}".lower()
        minutes = self.cfg.account_cooldown / 60
        if any(hint in low for hint in _LIMIT_HINTS):
            minutes = max(minutes, 30)
        if any(hint in low for hint in _AUTH_HINTS):
            # Токен мог протухнуть — на следующей попытке логинимся заново.
            self.forget_client(account.id)
            minutes = 2
        account.cooldown_until = now() + int(minutes * 60)
        account.last_error = truncate(text, 200)
        await self.db.llm_account_fail(account.id, account.cooldown_until,
                                       account.last_error)

    # ------------------------------------------------------------------ проверка

    async def check(self, provider: str, email: str, password: str) -> str:
        """Логин + пробное сообщение. Возвращает ответ модели или кидает LLMError."""
        adapter = ADAPTERS.get(provider)
        if adapter is None:
            raise LLMError(f"неизвестный провайдер: {provider}")
        probe = Account(id=-1, provider=provider, email=email, password=password)

        def run() -> str:
            client = adapter.make_client(probe, self._store_path(probe))
            chat = adapter.new_chat(client)
            return adapter.send(chat, "Ответь одним словом: ок")

        try:
            return (await asyncio.to_thread(run)).strip()
        except ImportError as exc:
            raise LLMError(
                f"не хватает библиотек для аккаунтов ({exc}). "
                "Поставь: pip install -r requirements-accounts.txt"
            ) from exc
        except Exception as exc:                           # noqa: BLE001
            raise LLMError(str(exc)) from exc

    async def close(self) -> None:
        self._clients.clear()
        self._chats.clear()
