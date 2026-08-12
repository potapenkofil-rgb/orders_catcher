"""Юзербот на Telethon: слушает все группы и каналы аккаунта.

Вход в аккаунт делается через бота (api_id/api_hash → телефон → код → пароль 2FA),
сессия сохраняется строкой в SQLite, так что после рестарта переавторизация не нужна.
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
class Account:
    user_id: int
    name: str
    username: str | None
    phone: str | None


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
    def __init__(self, cfg: Config, db: Database, on_message: OnMessage):
        self.cfg = cfg
        self.db = db
        self.on_message = on_message
        self.client: TelegramClient | None = None
        self.account: Account | None = None
        # временное состояние процесса логина
        self._pending: TelegramClient | None = None
        self._phone: str = ""
        self._code_hash: str = ""
        self._api: tuple[int, str] | None = None

    # ------------------------------------------------------------------ состояние

    @property
    def is_running(self) -> bool:
        return self.client is not None and self.client.is_connected()

    # ------------------------------------------------------------------ автозапуск

    async def try_start_saved(self) -> bool:
        """Поднимает юзербот из сохранённой сессии. False — сессии нет/протухла."""
        session = await self.db.get("session")
        api_id = await self.db.get("api_id")
        api_hash = await self.db.get("api_hash")
        if not (session and api_id and api_hash):
            return False
        try:
            client = TelegramClient(StringSession(session), int(api_id), str(api_hash), **DEVICE)
            await client.connect()
            if not await client.is_user_authorized():
                log.warning("Сохранённая сессия больше не авторизована")
                await client.disconnect()
                return False
            await self._activate(client)
            return True
        except Exception as exc:                          # noqa: BLE001
            log.error("Не удалось поднять сохранённую сессию: %s", exc)
            return False

    async def _activate(self, client: TelegramClient) -> None:
        self.client = client
        client.add_event_handler(self._handle, events.NewMessage(incoming=True))
        me = await client.get_me()
        self.account = Account(
            user_id=me.id,
            name=_entity_name(me),
            username=me.username,
            phone=me.phone,
        )
        log.info("Юзербот запущен: %s (@%s)", self.account.name, self.account.username)

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:                             # noqa: BLE001
                pass
            self.client = None
            self.account = None
        await self._drop_pending()

    async def logout(self) -> None:
        """Выход из аккаунта + удаление сессии из базы."""
        if self.client is not None:
            try:
                await self.client.log_out()
            except Exception as exc:                      # noqa: BLE001
                log.warning("log_out не прошёл: %s", exc)
            self.client = None
            self.account = None
        await self.db.delete("session")
        await self.db.delete("api_id")
        await self.db.delete("api_hash")

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

    async def submit_code(self, code: str) -> bool:
        """Шаг 2. True — вошли, False — нужен пароль 2FA."""
        if self._pending is None:
            raise LoginError("Сессия входа потерялась, начни заново: /login")
        try:
            await self._pending.sign_in(
                phone=self._phone, code=code, phone_code_hash=self._code_hash
            )
        except SessionPasswordNeededError:
            return False
        except PhoneCodeInvalidError as exc:
            raise LoginError("Код неверный. Пришли ещё раз (можно с пробелами: 1 2 3 4 5)") from exc
        except PhoneCodeExpiredError as exc:
            raise LoginError("Код протух. Начни заново: /login") from exc
        except FloodWaitError as exc:
            raise LoginError(f"Слишком много попыток, подожди {exc.seconds} секунд") from exc
        except Exception as exc:                          # noqa: BLE001
            raise LoginError(f"Ошибка входа: {exc}") from exc
        await self._finish_login()
        return True

    async def submit_password(self, password: str) -> None:
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
        await self._finish_login()

    async def _finish_login(self) -> None:
        client, self._pending = self._pending, None
        assert client is not None and self._api is not None
        session = client.session.save()
        await self.db.set("session", session)
        await self.db.set("api_id", self._api[0])
        await self.db.set("api_hash", self._api[1])
        await self.db.set("logged_in_at", now())
        if self.client is not None:                       # на случай повторного логина
            try:
                await self.client.disconnect()
            except Exception:                             # noqa: BLE001
                pass
        await self._activate(client)
        self._phone = ""
        self._code_hash = ""

    # ------------------------------------------------------------------ чаты

    async def list_sources(self, limit: int = 500) -> list[tuple[str, int, str]]:
        """Список отслеживаемых источников: (название, id, тип)."""
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

    # ------------------------------------------------------------------ обработчик

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
            author_name = ""
            author_username = None
            if not is_broadcast:
                try:
                    sender = await event.get_sender()
                except Exception:                         # noqa: BLE001
                    sender = None
                author_name = _entity_name(sender)
                author_username = getattr(sender, "username", None)
                if getattr(sender, "bot", False) and not text:
                    return
            else:
                author_name = _entity_name(chat)
                author_username = getattr(chat, "username", None)

            cand = Candidate(
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
            )
            await self.on_message(cand)
        except Exception:                                 # noqa: BLE001
            log.exception("Ошибка в обработчике нового сообщения")
