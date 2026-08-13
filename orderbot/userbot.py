"""Юзерботы на Telethon: слушают все группы и каналы подключённых аккаунтов.

Аккаунтов может быть сколько угодно. Вход делается через бота
(api_id/api_hash → телефон → код → пароль 2FA), сессия каждого аккаунта
сохраняется строкой в SQLite, так что после рестарта переавторизация не нужна.

Одно и то же сообщение, увиденное двумя аккаунтами сразу, отсекается дальше
по пайплайну — здесь каждый аккаунт просто отдаёт всё, что видит.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User

from .config import Config
from .db import Database
from .models import Candidate
from .utils import log, now

OnMessage = Callable[[Candidate], Awaitable[None]]

DEVICE = dict(
    device_model="Desktop",
    system_version="Windows 10",
    app_version="5.3.1",
)


class LoginError(Exception):
    """Ошибка на любом шаге авторизации — текст уже пригоден для показа юзеру."""


@dataclass
class TgAccount:
    id: int
    api_id: int
    api_hash: str
    phone: str = ""
    session: str = ""
    label: str = ""
    username: str = ""
    user_id: int | None = None
    disabled: bool = False
    last_error: str = ""

    @property
    def title(self) -> str:
        if self.username:
            return f"{self.label or self.phone} (@{self.username})"
        return self.label or self.phone or f"аккаунт {self.id}"


def _entity_name(entity) -> str:
    if entity is None:
        return ""
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or (entity.username or f"id{entity.id}")
    title = getattr(entity, "title", None)
    return title or f"id{getattr(entity, 'id', '?')}"


class UserBot:
    """Один телеграм-аккаунт: слушает его группы и каналы."""

    def __init__(self, account: TgAccount, on_message: OnMessage):
        self.account = account
        self.on_message = on_message
        self.client: TelegramClient | None = None

    @property
    def is_running(self) -> bool:
        return self.client is not None and self.client.is_connected()

    async def start(self) -> bool:
        """Поднимает клиент из сохранённой сессии. False — сессия протухла."""
        if self.account.disabled or not self.account.session:
            return False
        try:
            client = TelegramClient(
                StringSession(self.account.session),
                self.account.api_id, self.account.api_hash, **DEVICE,
            )
            await client.connect()
            if not await client.is_user_authorized():
                self.account.last_error = "сессия больше не авторизована"
                log.warning("Аккаунт %s: %s", self.account.title, self.account.last_error)
                await client.disconnect()
                return False
            self.adopt(client)
            return True
        except Exception as exc:                          # noqa: BLE001
            self.account.last_error = str(exc)
            log.error("Аккаунт %s не поднялся: %s", self.account.title, exc)
            return False

    def adopt(self, client: TelegramClient) -> None:
        """Берёт уже авторизованный клиент (сразу после логина) и вешает обработчик."""
        self.client = client
        client.add_event_handler(self._handle, events.NewMessage(incoming=True))
        self.account.last_error = ""
        log.info("Слушаю чаты аккаунта %s", self.account.title)

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:                             # noqa: BLE001
                pass
            self.client = None

    async def logout(self) -> None:
        if self.client is not None:
            try:
                await self.client.log_out()
            except Exception as exc:                      # noqa: BLE001
                log.warning("log_out для %s не прошёл: %s", self.account.title, exc)
            self.client = None

    async def list_sources(self, limit: int = 500) -> list[tuple[str, int, str]]:
        """Группы и каналы аккаунта: (название, id, тип)."""
        if self.client is None:
            return []
        result: list[tuple[str, int, str]] = []
        async for dialog in self.client.iter_dialogs(limit=limit):
            entity = dialog.entity
            if isinstance(entity, Channel):
                kind = "канал" if entity.broadcast else "супергруппа"
            elif isinstance(entity, Chat):
                kind = "группа"
            else:
                continue
            result.append((dialog.name or _entity_name(entity), dialog.id, kind))
        return result

    async def _handle(self, event) -> None:
        try:
            if not (event.is_group or event.is_channel):
                return                                    # личка не интересует
            if event.out:
                return                                    # свои же сообщения
            text = (event.raw_text or "").strip()
            if not text:
                return

            chat = await event.get_chat()
            is_broadcast = bool(getattr(chat, "broadcast", False))

            author_id = event.sender_id
            if is_broadcast:
                author_name = _entity_name(chat)
                author_username = getattr(chat, "username", None)
            else:
                try:
                    sender = await event.get_sender()
                except Exception:                         # noqa: BLE001
                    sender = None
                author_name = _entity_name(sender)
                author_username = getattr(sender, "username", None)

            await self.on_message(Candidate(
                chat_id=event.chat_id,
                msg_id=event.id,
                text=text,
                ts=int(event.date.timestamp()) if event.date else now(),
                chat_title=_entity_name(chat),
                chat_username=getattr(chat, "username", None),
                author_id=author_id,
                author_name=author_name,
                author_username=author_username,
                is_channel=is_broadcast,
                via=self.account.title,
            ))
        except Exception:                                 # noqa: BLE001
            log.exception("Ошибка в обработчике нового сообщения")


class UserBotManager:
    """Все телеграм-аккаунты сразу: запуск, вход, выход, список чатов."""

    def __init__(self, cfg: Config, db: Database, on_message: OnMessage):
        self.cfg = cfg
        self.db = db
        self.on_message = on_message
        self.bots: dict[int, UserBot] = {}
        # временное состояние процесса логина
        self._pending: TelegramClient | None = None
        self._phone = ""
        self._code_hash = ""
        self._api: tuple[int, str] | None = None

    # ------------------------------------------------------------------ состояние

    @property
    def is_running(self) -> bool:
        return any(bot.is_running for bot in self.bots.values())

    @property
    def running(self) -> list[UserBot]:
        return [bot for bot in self.bots.values() if bot.is_running]

    @property
    def accounts(self) -> list[TgAccount]:
        return [bot.account for bot in self.bots.values()]

    def get(self, account_id: int) -> UserBot | None:
        return self.bots.get(account_id)

    # ------------------------------------------------------------------ загрузка

    async def load(self) -> None:
        """Читает аккаунты из базы (клиенты пока не поднимает)."""
        await self._migrate_single_account()
        self.bots = {}
        for row in await self.db.tg_accounts():
            account = TgAccount(
                id=row["id"], api_id=row["api_id"], api_hash=row["api_hash"],
                phone=row["phone"], session=row["session"], label=row["label"],
                username=row["username"], user_id=row["user_id"],
                disabled=bool(row["disabled"]), last_error=row["last_error"],
            )
            self.bots[account.id] = UserBot(account, self.on_message)

    async def _migrate_single_account(self) -> None:
        """Переносит сессию из старой одноаккаунтной схемы, если она осталась."""
        session = await self.db.get("session")
        api_id = await self.db.get("api_id")
        api_hash = await self.db.get("api_hash")
        if not (session and api_id and api_hash):
            return
        await self.db.tg_account_add(int(api_id), str(api_hash), "", str(session), "", "", None)
        for key in ("session", "api_id", "api_hash"):
            await self.db.delete(key)
        log.info("Перенёс аккаунт из старой одноаккаунтной схемы")

    async def start_all(self) -> int:
        started = 0
        for bot in self.bots.values():
            if bot.account.disabled:
                continue
            if await bot.start():
                started += 1
            elif bot.account.last_error:
                await self.db.tg_account_error(bot.account.id, bot.account.last_error)
        return started

    async def stop_all(self) -> None:
        for bot in self.bots.values():
            await bot.stop()
        await self._drop_pending()

    # ------------------------------------------------------------------ управление

    async def remove(self, account_id: int) -> bool:
        bot = self.bots.pop(account_id, None)
        if bot is None:
            return False
        await bot.logout()
        await self.db.tg_account_delete(account_id)
        return True

    async def toggle(self, account_id: int) -> bool:
        """Включает/выключает аккаунт. Возвращает новое состояние disabled."""
        bot = self.bots.get(account_id)
        if bot is None:
            return False
        disabled = await self.db.tg_account_toggle(account_id)
        bot.account.disabled = disabled
        if disabled:
            await bot.stop()
        else:
            await bot.start()
        return disabled

    async def list_sources(self) -> list[tuple[TgAccount, list[tuple[str, int, str]]]]:
        return [(bot.account, await bot.list_sources()) for bot in self.running]

    # ------------------------------------------------------------------ логин

    async def _drop_pending(self) -> None:
        if self._pending is not None:
            try:
                await self._pending.disconnect()
            except Exception:                             # noqa: BLE001
                pass
        self._pending = None
        self._phone = ""
        self._code_hash = ""

    async def start_login(self, api_id: int, api_hash: str, phone: str) -> None:
        """Шаг 1: запрашивает код подтверждения на телефон."""
        await self._drop_pending()
        self._api = (api_id, api_hash)
        self._phone = phone
        client = TelegramClient(StringSession(), api_id, api_hash, **DEVICE)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            self._code_hash = sent.phone_code_hash
            self._pending = client
        except ApiIdInvalidError as exc:
            await client.disconnect()
            raise LoginError("Неверные api_id / api_hash. Проверь их на my.telegram.org") from exc
        except PhoneNumberInvalidError as exc:
            await client.disconnect()
            raise LoginError("Неверный номер телефона. Формат: +79991234567") from exc
        except FloodWaitError as exc:
            await client.disconnect()
            raise LoginError(f"Телеграм просит подождать {exc.seconds} секунд перед новой попыткой") from exc
        except Exception as exc:                          # noqa: BLE001
            await client.disconnect()
            raise LoginError(f"Не удалось запросить код: {exc}") from exc

    async def submit_code(self, code: str) -> TgAccount | None:
        """Шаг 2. Аккаунт — вошли, None — нужен пароль 2FA."""
        if self._pending is None:
            raise LoginError("Сессия входа потерялась, начни заново: /login")
        try:
            await self._pending.sign_in(
                phone=self._phone, code=code, phone_code_hash=self._code_hash
            )
        except SessionPasswordNeededError:
            return None
        except PhoneCodeInvalidError as exc:
            raise LoginError("Код неверный. Пришли ещё раз (можно с пробелами: 1 2 3 4 5)") from exc
        except PhoneCodeExpiredError as exc:
            raise LoginError("Код протух. Начни заново: /login") from exc
        except FloodWaitError as exc:
            raise LoginError(f"Слишком много попыток, подожди {exc.seconds} секунд") from exc
        except Exception as exc:                          # noqa: BLE001
            raise LoginError(f"Ошибка входа: {exc}") from exc
        return await self._finish_login()

    async def submit_password(self, password: str) -> TgAccount:
        """Шаг 3 (если включена двухфакторка)."""
        if self._pending is None:
            raise LoginError("Сессия входа потерялась, начни заново: /login")
        try:
            await self._pending.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise LoginError("Пароль неверный, попробуй ещё раз") from exc
        except FloodWaitError as exc:
            raise LoginError(f"Слишком много попыток, подожди {exc.seconds} секунд") from exc
        except Exception as exc:                          # noqa: BLE001
            raise LoginError(f"Ошибка входа: {exc}") from exc
        return await self._finish_login()

    async def _finish_login(self) -> TgAccount:
        client, self._pending = self._pending, None
        assert client is not None and self._api is not None
        me = await client.get_me()

        # Тот же аккаунт мог быть подключён раньше — старый клиент отцепляем.
        for existing in list(self.bots.values()):
            if existing.account.user_id == me.id:
                await existing.stop()
                self.bots.pop(existing.account.id, None)

        account_id = await self.db.tg_account_add(
            self._api[0], self._api[1], self._phone, client.session.save(),
            _entity_name(me), me.username or "", me.id,
        )
        account = TgAccount(
            id=account_id, api_id=self._api[0], api_hash=self._api[1],
            phone=self._phone, session=client.session.save(),
            label=_entity_name(me), username=me.username or "", user_id=me.id,
        )
        bot = UserBot(account, self.on_message)
        bot.adopt(client)
        self.bots[account_id] = bot
        self._phone = ""
        self._code_hash = ""
        return account
