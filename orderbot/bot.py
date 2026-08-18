"""Управляющий бот: вход в аккаунт, настройки, ЧС, статистика."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from .classifier import Classifier
from .config import Config
from .db import Database
from .dedup import DedupIndex
from .llm import PROVIDER_TITLES, PROVIDERS, LLMError, LLMRouter
from .models import Candidate
from .pipeline import Pipeline
from .state import Runtime
from .userbot import LoginError, TgAccount, UserBotManager
from .utils import esc, human_ts, log, now, truncate

DIGITS_RE = re.compile(r"\D+")

HELP = """<b>Ловец заказов</b>

Слушаю все группы и каналы твоего аккаунта и присылаю то, что похоже на заказ.
Проверка в два этапа: сначала пачка сообщений (100 штук или 5 минут — что раньше),
потом каждое выжившее сообщение проверяется отдельно.

<b>Телеграм-аккаунты</b> (можно несколько)
/login — подключить ещё один аккаунт (api_id, api_hash, телефон, код, 2FA)
/session — подключить готовой строкой сессии, если код не доходит
/accounts — список аккаунтов: включить, выключить, отключить
/chats — какие чаты и каналы слушаю

<b>Аккаунты нейросети</b>
/llm — список аккаунтов, включить/выключить/удалить
/addllm — добавить аккаунт (DeepSeek или Qwen) по email и паролю

<b>Управление</b>
/status — что происходит прямо сейчас
/newchat — начать чаты с моделью заново (после смены промптов)
/pause — приостановить приём
/resume — продолжить
/stats — статистика

<b>Фильтры</b>
/bl — чёрные списки (с кнопками «убрать»)
/unban_user id — убрать пользователя из ЧС
/unban_chat id — убрать чат из ЧС
/threshold 0.65 — минимальная уверенность для уведомления
/profile текст — чем ты занимаешься (влияет на отбор)
/test текст — прогнать текст через обе проверки

/cancel — отменить текущий диалог"""


@dataclass
class Deps:
    cfg: Config
    db: Database
    runtime: Runtime
    userbot: UserBotManager
    pipeline: Pipeline
    classifier: Classifier
    dedup: DedupIndex
    router: LLMRouter


class AddAccount(StatesGroup):
    provider = State()
    email = State()
    password = State()


class ImportSession(StatesGroup):
    api_id = State()
    api_hash = State()
    session = State()


class Login(StatesGroup):
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()


class OwnerOnly(BaseMiddleware):
    """Пускает только владельца. Первый, кто напишет /start, им и становится."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None
        if self.runtime.owner_id is None:
            text = getattr(event, "text", "") or ""
            if text.startswith("/start"):
                await self.runtime.bind_owner(user.id)
                log.info("Владелец привязан: %s (@%s)", user.id, user.username)
            else:
                return None
        if not self.runtime.is_owner(user.id):
            log.debug("Чужой пользователь %s проигнорирован", user.id)
            return None
        return await handler(event, data)


router = Router(name="orderbot")


async def _quiet_delete(message: Message) -> None:
    """Стираем сообщения с секретами из переписки."""
    try:
        await message.delete()
    except Exception:                                     # noqa: BLE001
        pass


# --------------------------------------------------------------------------- базовое

@router.message(CommandStart())
async def cmd_start(message: Message, deps: Deps) -> None:
    hint = ("" if deps.userbot.is_running
            else "\n\n⚠️ Телеграм-аккаунтов нет — начни с /login")
    await message.answer(HELP + hint)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменил.")


# ----------------------------------------------------------------------------- логин

@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext, deps: Deps) -> None:
    await state.set_state(Login.api_id)
    already = len(deps.userbot.accounts)
    prefix = (f"Сейчас подключено аккаунтов: {already}. Добавляю ещё один.\n\n"
              if already else "")
    await message.answer(
        prefix
        + "<b>Шаг 1 из 4.</b> Пришли <code>api_id</code>.\n\n"
        "Взять тут: my.telegram.org → API development tools. "
        "Это число вроде <code>1234567</code>.\n\n"
        "Отменить — /cancel"
    )


@router.message(Login.api_id)
async def login_api_id(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("api_id — это число. Попробуй ещё раз или /cancel")
        return
    await state.update_data(api_id=int(raw))
    await state.set_state(Login.api_hash)
    await message.answer(
        "<b>Шаг 2 из 4.</b> Пришли <code>api_hash</code> — строка из 32 символов с той же страницы."
    )


@router.message(Login.api_hash)
async def login_api_hash(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if len(raw) < 16:
        await message.answer("Непохоже на api_hash. Попробуй ещё раз или /cancel")
        return
    await state.update_data(api_hash=raw)
    await state.set_state(Login.phone)
    await _quiet_delete(message)
    await message.answer(
        "<b>Шаг 3 из 4.</b> Пришли номер телефона аккаунта в формате "
        "<code>+79991234567</code>."
    )


@router.message(Login.phone)
async def login_phone(message: Message, state: FSMContext, deps: Deps) -> None:
    phone = re.sub(r"[^\d+]", "", message.text or "")
    if not phone.startswith("+"):
        phone = "+" + phone.lstrip("+")
    if len(phone) < 9:
        await message.answer("Непохоже на номер. Формат: +79991234567")
        return
    if phone.startswith("+8") and len(phone) == 12:
        await message.answer(
            "⚠️ Номер начинается с <b>8</b> — это местный формат. "
            "Нужен международный: <code>+7</code> вместо восьмёрки.\n"
            "Пришли ещё раз или /cancel"
        )
        return
    data = await state.get_data()
    await _quiet_delete(message)
    try:
        where = await deps.userbot.start_login(data["api_id"], data["api_hash"], phone)
    except LoginError as exc:
        await state.clear()
        await message.answer(f"❌ {esc(str(exc))}\n\nНачни заново: /login")
        return
    await state.update_data(phone=phone)
    await state.set_state(Login.code)
    await message.answer(
        f"<b>Шаг 4 из 4.</b> Код на <code>{esc(phone)}</code> отправлен "
        f"<b>{esc(where)}</b>.\n\n"
        "⚠️ Пришли его <b>с разделителями</b>: <code>1-2-3-4-5</code> или "
        "<code>1 2 3 4 5</code>.\n"
        "Если отправить пятизначное число как есть, Телеграм увидит код в переписке "
        "и сразу его аннулирует.\n\n"
        "Не пришёл никуда за пару минут — /resend (попробую по SMS). "
        "Если и так пусто, скорее всего Телеграм не доставляет код на серверный "
        "IP — тогда /cancel и смотри /session."
    )


@router.message(Login.code, Command("resend"))
async def login_resend(message: Message, deps: Deps) -> None:
    try:
        where = await deps.userbot.resend_code()
    except LoginError as exc:
        await message.answer(f"❌ {esc(str(exc))}")
        return
    await message.answer(f"Отправил ещё раз — {esc(where)}.")


@router.message(Login.code)
async def login_code(message: Message, state: FSMContext, deps: Deps) -> None:
    code = DIGITS_RE.sub("", message.text or "")
    if not code:
        await message.answer("Не вижу цифр. Пришли код, например <code>1-2-3-4-5</code>")
        return
    await _quiet_delete(message)
    try:
        account = await deps.userbot.submit_code(code)
    except LoginError as exc:
        await message.answer(f"❌ {esc(str(exc))}")
        return
    if account is not None:
        await state.clear()
        await _login_success(message, deps, account)
        return
    await state.set_state(Login.password)
    await message.answer("🔐 Включена двухфакторка. Пришли облачный пароль.")


@router.message(Login.password)
async def login_password(message: Message, state: FSMContext, deps: Deps) -> None:
    password = (message.text or "").strip()
    await _quiet_delete(message)
    if not password:
        await message.answer("Пустой пароль. Попробуй ещё раз или /cancel")
        return
    try:
        account = await deps.userbot.submit_password(password)
    except LoginError as exc:
        await message.answer(f"❌ {esc(str(exc))}")
        return
    await state.clear()
    await _login_success(message, deps, account)


async def _login_success(message: Message, deps: Deps, account: TgAccount) -> None:
    total = len(deps.userbot.accounts)
    await message.answer(
        f"✅ Вошёл как <b>{esc(account.title)}</b>.\n"
        f"Всего аккаунтов на связи: {total}.\n\n"
        "Слушаю все их группы и каналы. Одно и то же сообщение из общего чата "
        "придёт один раз.\n\n"
        "Подключить ещё — /login, список — /accounts, источники — /chats"
    )


def _tg_keyboard(account: TgAccount) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="▶️ Включить" if account.disabled else "⏸ Выключить",
            callback_data=f"tg_toggle:{account.id}",
        ),
        InlineKeyboardButton(text="🚪 Отключить", callback_data=f"tg_del:{account.id}"),
    ]])


def _tg_state(deps: Deps, account: TgAccount) -> str:
    bot = deps.userbot.get(account.id)
    if account.disabled:
        return "⛔ выключен"
    if bot is not None and bot.is_running:
        return "✅ слушает чаты"
    error = f": {truncate(account.last_error, 60)}" if account.last_error else ""
    return f"⚠️ не на связи{error}"


@router.message(Command("accounts"))
async def cmd_tg_accounts(message: Message, deps: Deps) -> None:
    accounts = deps.userbot.accounts
    if not accounts:
        await message.answer("Телеграм-аккаунтов нет. Подключить — /login")
        return
    await message.answer(f"<b>Телеграм-аккаунты ({len(accounts)})</b>")
    for account in accounts:
        await message.answer(
            f"<b>{esc(account.title)}</b>\n{esc(_tg_state(deps, account))}",
            reply_markup=_tg_keyboard(account),
        )


@router.message(Command("session"))
async def cmd_session(message: Message, state: FSMContext) -> None:
    await state.set_state(ImportSession.api_id)
    await message.answer(
        "<b>Подключение готовой сессией.</b>\n\n"
        "Нужно, когда Телеграм не доставляет код входа — так бывает, если запрос "
        "идёт с серверного IP. Логинишься на своём компьютере, а сюда отдаёшь "
        "готовую строку сессии.\n\n"
        "На компьютере:\n"
        "<code>pip install telethon</code>\n"
        "<code>python tools/make_session.py</code>\n\n"
        "Скрипт спросит api_id, api_hash, телефон и код, потом напечатает строку. "
        "Она даёт полный доступ к аккаунту — никому больше не показывай.\n\n"
        "Пришли <code>api_id</code>. Отменить — /cancel"
    )


@router.message(ImportSession.api_id)
async def session_api_id(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("api_id — это число. Попробуй ещё раз или /cancel")
        return
    await state.update_data(api_id=int(raw))
    await state.set_state(ImportSession.api_hash)
    await message.answer("Теперь <code>api_hash</code> — тот же, с которым делал сессию.")


@router.message(ImportSession.api_hash)
async def session_api_hash(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if len(raw) < 16:
        await message.answer("Непохоже на api_hash. Попробуй ещё раз или /cancel")
        return
    await state.update_data(api_hash=raw)
    await state.set_state(ImportSession.session)
    await _quiet_delete(message)
    await message.answer("Теперь саму строку сессии. Сообщение с ней я удалю.")


@router.message(ImportSession.session)
async def session_string(message: Message, state: FSMContext, deps: Deps) -> None:
    session = (message.text or "").strip()
    await _quiet_delete(message)
    if len(session) < 40:
        await message.answer("Строка слишком короткая. Попробуй ещё раз или /cancel")
        return
    data = await state.get_data()
    note = await message.answer("Проверяю сессию…")
    try:
        account = await deps.userbot.add_by_session(
            data["api_id"], data["api_hash"], session
        )
    except LoginError as exc:
        await note.edit_text(f"❌ {esc(str(exc))}\n\nПопробовать снова — /session")
        return
    await state.clear()
    await note.edit_text(
        f"✅ Подключил <b>{esc(account.title)}</b>.\n"
        f"Всего аккаунтов на связи: {len(deps.userbot.accounts)}.\n\n"
        "Источники — /chats"
    )


@router.message(Command("logout"))
async def cmd_logout(message: Message, deps: Deps) -> None:
    if not deps.userbot.accounts:
        await message.answer("Аккаунтов и так нет.")
        return
    await message.answer("Отключить можно любой — кнопкой «🚪 Отключить» под ним:")
    await cmd_tg_accounts(message, deps)


@router.callback_query(F.data.startswith("tg_del:"))
async def cb_tg_delete(call: CallbackQuery, deps: Deps) -> None:
    account_id = int(call.data.split(":", 1)[1])
    await deps.userbot.remove(account_id)
    await call.answer("Отключил, сессия стёрта")
    try:
        await call.message.edit_text(f"{call.message.html_text}\n\n🚪 Отключён")
    except Exception:                                     # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("tg_toggle:"))
async def cb_tg_toggle(call: CallbackQuery, deps: Deps) -> None:
    account_id = int(call.data.split(":", 1)[1])
    disabled = await deps.userbot.toggle(account_id)
    await call.answer("Выключил" if disabled else "Включил")
    bot = deps.userbot.get(account_id)
    if bot is None:
        return
    try:
        await call.message.edit_text(
            f"<b>{esc(bot.account.title)}</b>\n{esc(_tg_state(deps, bot.account))}",
            reply_markup=_tg_keyboard(bot.account),
        )
    except Exception:                                     # noqa: BLE001
        pass


# ---------------------------------------------------------------------------- статус

@router.message(Command("status"))
async def cmd_status(message: Message, deps: Deps) -> None:
    rt = deps.runtime
    accounts = deps.userbot.accounts
    running = deps.userbot.running
    lines = ["<b>Статус</b>", ""]
    if running:
        lines.append(f"✅ Телеграм-аккаунтов на связи: {len(running)} из {len(accounts)}")
        for bot in running[:5]:
            lines.append(f"   • {esc(bot.account.title)}")
        if len(running) > 5:
            lines.append(f"   • …и ещё {len(running) - 5}")
    elif accounts:
        lines.append(f"⚠️ Ни один из {len(accounts)} аккаунтов не на связи — /accounts")
    else:
        lines.append("❌ Телеграм-аккаунтов нет — /login")
    lines.append(f"{'⏸ На паузе' if rt.paused else '▶️ Слушаю чаты'}")
    lines.append("")
    lines.append(f"📦 В буфере: {deps.pipeline.buffer.pending} из {deps.cfg.batch_size}")
    if deps.pipeline.buffer.pending:
        lines.append(f"⏱ До проверки: {int(deps.pipeline.buffer.seconds_left)} с")
    lines.append(f"🔁 Обработано пачек: {deps.pipeline.batches_done}")
    if deps.pipeline.last_batch_at:
        lines.append(f"🕑 Последняя: {human_ts(int(deps.pipeline.last_batch_at))}")
    lines.append("")
    lines.append(f"🚫 В ЧС: {len(rt.banned_users)} польз., {len(rt.banned_chats)} чатов")
    lines.append(f"🎯 Порог уверенности: {rt.min_confidence:.2f}")
    lines.append(f"🧠 Проверка: {esc(deps.router.status())}")
    if deps.cfg.llm_key and deps.cfg.llm_backend != "accounts":
        lines.append(f"   модели по ключу: {esc(deps.cfg.model_stage1)} → "
                     f"{esc(deps.cfg.model_stage2)}")
    lines.append(f"💬 Чат этапа 1: {esc(deps.classifier.chat1.info())}")
    lines.append(f"💬 Чат этапа 2: {esc(deps.classifier.chat2.info())}")
    lines.append(f"🔄 Смена чатов: раз в {deps.cfg.chat_ttl / 3600:.0f} ч")
    lines.append(f"🧾 Помню сообщений (антидубль): {deps.dedup.size}")
    if deps.classifier.last_error:
        lines.append(f"\n⚠️ Последняя ошибка LLM: {esc(truncate(deps.classifier.last_error, 200))}")
    await message.answer("\n".join(lines))


@router.message(Command("pause"))
async def cmd_pause(message: Message, deps: Deps) -> None:
    await deps.runtime.set_paused(True)
    await message.answer("⏸ Приостановил. Сообщения не собираю. Продолжить — /resume")


@router.message(Command("resume"))
async def cmd_resume(message: Message, deps: Deps) -> None:
    await deps.runtime.set_paused(False)
    await message.answer("▶️ Продолжаю слушать чаты.")


@router.message(Command("newchat"))
async def cmd_newchat(message: Message, deps: Deps) -> None:
    await deps.classifier.reset_chats()
    await message.answer(
        "Начал чаты с моделью заново. Системный промпт уйдёт свежий "
        "уже со следующей пачки.\n\n"
        f"Этап 1: <code>{esc(deps.classifier.chat1.session_id)}</code>\n"
        f"Этап 2: <code>{esc(deps.classifier.chat2.session_id)}</code>"
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, deps: Deps) -> None:
    today = await deps.db.stats_today()
    total = await deps.db.stats_total()

    def fmt(source: dict[str, int]) -> str:
        return (
            f"  сообщений: {source.get('seen', 0)}\n"
            f"  в очередь: {source.get('queued', 0)}\n"
            f"  дублей отброшено: {source.get('skipped_duplicate', 0)}\n"
            f"  барахолки отброшено: {source.get('skipped_junk', 0)}\n"
            f"  по ЧС отброшено: {source.get('skipped_blacklist', 0)}\n"
            f"  прошли этап 1: {source.get('stage1_passed', 0)}\n"
            f"  прошли этап 2: {source.get('stage2_passed', 0)}\n"
            f"  отправлено: {source.get('sent', 0)}"
        )

    await message.answer(
        f"<b>Сегодня</b>\n{fmt(today)}\n\n<b>За всё время</b>\n{fmt(total)}"
    )


@router.message(Command("chats"))
async def cmd_chats(message: Message, deps: Deps) -> None:
    if not deps.userbot.is_running:
        await message.answer("Ни один телеграм-аккаунт не на связи — /login")
        return
    await message.answer("Собираю список…")
    per_account = await deps.userbot.list_sources()
    banned = deps.runtime.banned_chats
    seen: set[int] = set()
    lines: list[str] = []
    for account, sources in per_account:
        lines.append(f"<b>{esc(account.title)} — {len(sources)}</b>")
        for title, chat_id, kind in sources:
            mark = "🚫 " if chat_id in banned else ("↺ " if chat_id in seen else "")
            seen.add(chat_id)
            lines.append(f"{mark}{esc(truncate(title, 45))} · <i>{kind}</i> · "
                         f"<code>{chat_id}</code>")
        lines.append("")
    if not seen:
        await message.answer("Ни групп, ни каналов не нашёл.")
        return
    lines.insert(0, f"<b>Уникальных источников: {len(seen)}</b> "
                    "(↺ — чат виден и другому аккаунту)")
    lines.insert(1, "")
    await _send_chunks(message, lines)


async def _send_chunks(message: Message, lines: list[str], limit: int = 3500) -> None:
    buf: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) > limit and buf:
            await message.answer("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        await message.answer("\n".join(buf))


# ------------------------------------------------------------------------ настройки

@router.message(Command("threshold"))
async def cmd_threshold(message: Message, command: CommandObject, deps: Deps) -> None:
    arg = (command.args or "").strip().replace(",", ".")
    if not arg:
        await message.answer(
            f"Текущий порог: <b>{deps.runtime.min_confidence:.2f}</b>\n"
            "Изменить: <code>/threshold 0.7</code> (0 — слать всё, 1 — только железобетонное)"
        )
        return
    try:
        value = float(arg)
    except ValueError:
        await message.answer("Нужно число от 0 до 1, например <code>/threshold 0.7</code>")
        return
    await deps.runtime.set_min_confidence(value)
    await message.answer(f"Порог теперь <b>{deps.runtime.min_confidence:.2f}</b>")


@router.message(Command("profile"))
async def cmd_profile(message: Message, command: CommandObject, deps: Deps) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            "<b>Твой профиль для отбора:</b>\n\n"
            f"{esc(deps.runtime.profile)}\n\n"
            "Изменить: <code>/profile пишу боты и парсеры на Python, беру заказы от 5к</code>"
        )
        return
    await deps.runtime.set_profile(arg)
    await message.answer("Профиль обновлён — новые проверки пойдут уже с ним.")


@router.message(Command("test"))
async def cmd_test(message: Message, command: CommandObject, deps: Deps) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer("Пришли текст: <code>/test ищу разработчика бота, бюджет 10к</code>")
        return
    cand = Candidate(
        chat_id=message.chat.id,
        msg_id=message.message_id,
        text=text,
        ts=now(),
        chat_title="ручная проверка",
        author_name=message.from_user.full_name if message.from_user else "",
    )
    note = await message.answer("Проверяю…")
    stage1 = await deps.classifier.stage1([cand])
    if not stage1:
        await note.edit_text("Этап 1: <b>не прошло</b> — на заказ не похоже.")
        return
    verdict = await deps.classifier.stage2(cand)
    await note.edit_text(
        "Этап 1: <b>прошло</b>\n"
        f"Этап 2: <b>{'заказ' if verdict.is_order else 'не заказ'}</b> "
        f"({verdict.confidence:.2f}, порог {deps.runtime.min_confidence:.2f})\n"
        f"Категория: {esc(verdict.category or '—')}\n"
        f"Стек: {esc(verdict.stack or '—')}\n"
        f"Бюджет: {esc(verdict.budget or '—')}\n"
        f"Почему: {esc(verdict.reason or '—')}"
    )


# ------------------------------------------------------------- аккаунты нейросети

def _llm_keyboard(account) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="▶️ Включить" if account.disabled else "⏸ Выключить",
            callback_data=f"llm_toggle:{account.id}",
        ),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"llm_del:{account.id}"),
    ]])


@router.message(Command("llm"))
async def cmd_llm_accounts(message: Message, deps: Deps) -> None:
    await deps.router.accounts.reload()
    accounts = deps.router.accounts.accounts
    if not accounts:
        await message.answer(
            "Аккаунтов нейросети нет.\n\n"
            "Добавить: /addllm — понадобится email и пароль от аккаунта "
            "DeepSeek или Qwen.\n"
            + ("Пока проверяю по ключу из .env." if deps.cfg.llm_key
               else "Ключа в .env тоже нет — проверять сообщения сейчас нечем.")
        )
        return
    await message.answer(f"<b>Аккаунты нейросети ({len(accounts)})</b>")
    for account in accounts:
        await message.answer(
            f"<b>{esc(account.title)}</b> · {esc(account.email)}\n{esc(account.state())}",
            reply_markup=_llm_keyboard(account),
        )


@router.message(Command("addllm"))
async def cmd_addllm(message: Message, state: FSMContext) -> None:
    await state.set_state(AddAccount.provider)
    buttons = [[InlineKeyboardButton(text=PROVIDER_TITLES[p], callback_data=f"llm_prov:{p}")]
               for p in PROVIDERS]
    await message.answer(
        "Какой аккаунт добавляем?\n\n"
        "Логиниться буду по email и паролю — теми же, что и на сайте.\n"
        "Отменить — /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(AddAccount.provider, F.data.startswith("llm_prov:"))
async def cb_account_provider(call: CallbackQuery, state: FSMContext) -> None:
    provider = call.data.split(":", 1)[1]
    if provider not in PROVIDERS:
        await call.answer("Неизвестный провайдер", show_alert=True)
        return
    await state.update_data(provider=provider)
    await state.set_state(AddAccount.email)
    await call.answer()
    await call.message.answer(
        f"Аккаунт {PROVIDER_TITLES[provider]}. Пришли email."
    )


@router.message(AddAccount.email)
async def add_account_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if "@" not in email or len(email) < 5:
        await message.answer("Непохоже на email. Попробуй ещё раз или /cancel")
        return
    await state.update_data(email=email)
    await state.set_state(AddAccount.password)
    await message.answer("Теперь пароль от этого аккаунта. Сообщение с ним я удалю.")


@router.message(AddAccount.password)
async def add_account_password(message: Message, state: FSMContext, deps: Deps) -> None:
    password = (message.text or "").strip()
    await _quiet_delete(message)
    if not password:
        await message.answer("Пустой пароль. Попробуй ещё раз или /cancel")
        return
    data = await state.get_data()
    provider, email = data["provider"], data["email"]

    note = await message.answer("Проверяю вход… это займёт полминуты.")
    try:
        answer = await deps.router.accounts.check(provider, email, password)
    except LLMError as exc:
        await note.edit_text(
            f"❌ Не вышло: {esc(truncate(str(exc), 300))}\n\n"
            "Проверь email и пароль. Попробовать снова — /addllm"
        )
        await state.clear()
        return

    await deps.db.llm_account_add(provider, email, password)
    await deps.router.accounts.reload()
    await state.clear()
    await note.edit_text(
        f"✅ Аккаунт {esc(PROVIDER_TITLES[provider])} · {esc(email)} добавлен.\n"
        f"Ответ на проверочный вопрос: <i>{esc(truncate(answer, 100))}</i>\n\n"
        "Теперь проверка сообщений идёт через него. Все аккаунты — /llm"
    )


@router.callback_query(F.data.startswith("llm_del:"))
async def cb_account_delete(call: CallbackQuery, deps: Deps) -> None:
    account_id = int(call.data.split(":", 1)[1])
    deps.router.accounts.forget_client(account_id)
    await deps.db.llm_account_delete(account_id)
    await deps.router.accounts.reload()
    await call.answer("Удалил")
    try:
        await call.message.edit_text(f"{call.message.html_text}\n\n🗑 Удалён")
    except Exception:                                     # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("llm_toggle:"))
async def cb_account_toggle(call: CallbackQuery, deps: Deps) -> None:
    account_id = int(call.data.split(":", 1)[1])
    disabled = await deps.db.llm_account_toggle(account_id)
    await deps.router.accounts.reload()
    await call.answer("Выключил" if disabled else "Включил")
    account = next((a for a in deps.router.accounts.accounts if a.id == account_id), None)
    if account is None:
        return
    try:
        await call.message.edit_text(
            f"<b>{esc(account.title)}</b> · {esc(account.email)}\n{esc(account.state())}",
            reply_markup=_llm_keyboard(account),
        )
    except Exception:                                     # noqa: BLE001
        pass


# ------------------------------------------------------------------------------- ЧС

@router.message(Command("bl"))
async def cmd_blacklist(message: Message, deps: Deps) -> None:
    users = await deps.db.banned_users()
    chats = await deps.db.banned_chats()
    if not users and not chats:
        await message.answer("Чёрные списки пусты.")
        return

    if users:
        rows = [
            [InlineKeyboardButton(
                text=f"✅ {truncate(row['label'] or str(row['user_id']), 40)}",
                callback_data=f"unban_u:{row['user_id']}",
            )]
            for row in users[:20]
        ]
        await message.answer(
            f"<b>Пользователи в ЧС ({len(users)})</b>\nНажми, чтобы убрать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    if chats:
        rows = [
            [InlineKeyboardButton(
                text=f"✅ {truncate(row['label'] or str(row['chat_id']), 40)}",
                callback_data=f"unban_c:{row['chat_id']}",
            )]
            for row in chats[:20]
        ]
        await message.answer(
            f"<b>Чаты в ЧС ({len(chats)})</b>\nНажми, чтобы убрать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.message(Command("unban_user"))
async def cmd_unban_user(message: Message, command: CommandObject, deps: Deps) -> None:
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.answer("Нужен id: <code>/unban_user 123456789</code>")
        return
    ok = await deps.runtime.unban_user(int(arg))
    await message.answer("Убрал из ЧС." if ok else "Такого в ЧС и не было.")


@router.message(Command("unban_chat"))
async def cmd_unban_chat(message: Message, command: CommandObject, deps: Deps) -> None:
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.answer("Нужен id: <code>/unban_chat -1001234567890</code>")
        return
    ok = await deps.runtime.unban_chat(int(arg))
    await message.answer("Убрал из ЧС." if ok else "Такого в ЧС и не было.")


# ------------------------------------------------------------------------- кнопки

@router.callback_query(F.data.startswith("ban_u:"))
async def cb_ban_user(call: CallbackQuery, deps: Deps) -> None:
    hit = await deps.db.get_hit(int(call.data.split(":", 1)[1]))
    if hit is None or hit["author_id"] is None:
        await call.answer("Не нашёл автора этого сообщения", show_alert=True)
        return
    await deps.runtime.ban_user(int(hit["author_id"]), hit["author_name"])
    await call.answer("Автор в чёрном списке")
    await _mark(call, f"🚫 Автор в ЧС: {esc(hit['author_name'] or str(hit['author_id']))}")


@router.callback_query(F.data.startswith("ban_c:"))
async def cb_ban_chat(call: CallbackQuery, deps: Deps) -> None:
    hit = await deps.db.get_hit(int(call.data.split(":", 1)[1]))
    if hit is None:
        await call.answer("Не нашёл это сообщение", show_alert=True)
        return
    await deps.runtime.ban_chat(int(hit["chat_id"]), hit["chat_title"])
    await call.answer("Чат в чёрном списке")
    await _mark(call, f"🚫 Чат в ЧС: {esc(hit['chat_title'] or str(hit['chat_id']))}")


@router.callback_query(F.data.startswith("hide:"))
async def cb_hide(call: CallbackQuery) -> None:
    try:
        await call.message.delete()
    except Exception:                                     # noqa: BLE001
        await call.answer("Не смог удалить (слишком старое сообщение)", show_alert=True)
        return
    await call.answer("Скрыл")


@router.callback_query(F.data.startswith("unban_u:"))
async def cb_unban_user(call: CallbackQuery, deps: Deps) -> None:
    await deps.runtime.unban_user(int(call.data.split(":", 1)[1]))
    await call.answer("Убрал из ЧС")
    await _drop_button(call)


@router.callback_query(F.data.startswith("unban_c:"))
async def cb_unban_chat(call: CallbackQuery, deps: Deps) -> None:
    await deps.runtime.unban_chat(int(call.data.split(":", 1)[1]))
    await call.answer("Убрал из ЧС")
    await _drop_button(call)


async def _mark(call: CallbackQuery, note: str) -> None:
    """Дописывает пометку к уведомлению и убирает кнопки ЧС."""
    message = call.message
    if message is None:
        return
    keyboard = None
    if message.reply_markup:
        rows = [
            row for row in message.reply_markup.inline_keyboard
            if not any((btn.callback_data or "").startswith(("ban_u:", "ban_c:")) for btn in row)
        ]
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    try:
        await message.edit_text(
            f"{message.html_text}\n\n{note}",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:                                     # noqa: BLE001
        try:
            await message.edit_reply_markup(reply_markup=keyboard)
        except Exception:                                 # noqa: BLE001
            pass


async def _drop_button(call: CallbackQuery) -> None:
    """Убирает нажатую кнопку из списка ЧС."""
    message = call.message
    if message is None or message.reply_markup is None:
        return
    rows = [
        row for row in message.reply_markup.inline_keyboard
        if not any(btn.callback_data == call.data for btn in row)
    ]
    try:
        await message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        )
    except Exception:                                     # noqa: BLE001
        pass


# --------------------------------------------------------------------------- прочее

@router.message(StateFilter(None), F.text)
async def fallback(message: Message) -> None:
    await message.answer("Не знаю такой команды. Что умею — /help")


def build_dispatcher(deps: Deps) -> Dispatcher:
    dp = Dispatcher()
    guard = OwnerOnly(deps.runtime)
    router.message.middleware(guard)
    router.callback_query.middleware(guard)
    dp.include_router(router)
    dp["deps"] = deps
    return dp
