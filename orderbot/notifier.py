"""Отправка уведомлений владельцу + клавиатура с ЧС."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Database
from .models import Candidate, Verdict
from .state import Runtime
from .utils import esc, log, truncate

CATEGORY_LABELS = {
    "order": "разовый заказ",
    "vacancy": "вакансия",
    "other": "прочее",
}

MAX_MESSAGE_CHARS = 2800


def build_keyboard(hit_id: int, link: str | None, has_author: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if link:
        rows.append([InlineKeyboardButton(text="🔗 Открыть сообщение", url=link)])
    ban_row = []
    if has_author:
        ban_row.append(InlineKeyboardButton(text="🚫 Автора в ЧС", callback_data=f"ban_u:{hit_id}"))
    ban_row.append(InlineKeyboardButton(text="🚫 Чат в ЧС", callback_data=f"ban_c:{hit_id}"))
    rows.append(ban_row)
    rows.append([InlineKeyboardButton(text="🗑 Скрыть", callback_data=f"hide:{hit_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render(cand: Candidate, verdict: Verdict) -> str:
    percent = int(round(verdict.confidence * 100))
    category = CATEGORY_LABELS.get(verdict.category, verdict.category or "заказ")

    head = f"💼 <b>Найден заказ</b> · {percent}%"
    lines = [head, f"🏷 {esc(category)}"]
    if verdict.stack:
        lines.append(f"🛠 {esc(truncate(verdict.stack, 200))}")
    if verdict.budget:
        lines.append(f"💰 {esc(truncate(verdict.budget, 100))}")
    lines.append("")
    lines.append(f"💬 <b>Чат:</b> {esc(truncate(cand.chat_title or str(cand.chat_id), 80))}")
    lines.append(f"👤 <b>Автор:</b> {esc(truncate(cand.author_label, 80))}")
    if cand.author_id:
        lines.append(f"🆔 <code>{cand.author_id}</code>")
    lines.append("")
    lines.append(f"<blockquote>{esc(truncate(cand.text, MAX_MESSAGE_CHARS))}</blockquote>")
    if verdict.reason:
        lines.append("")
        lines.append(f"<i>{esc(truncate(verdict.reason, 200))}</i>")
    return "\n".join(lines)


class Notifier:
    def __init__(self, bot: Bot, db: Database, runtime: Runtime):
        self.bot = bot
        self.db = db
        self.runtime = runtime

    async def send_hit(self, cand: Candidate, verdict: Verdict) -> bool:
        if self.runtime.owner_id is None:
            log.warning("Некому слать уведомление: владелец не привязан (/start)")
            return False

        hit_id = await self.db.add_hit(
            chat_id=cand.chat_id,
            chat_title=cand.chat_title,
            msg_id=cand.msg_id,
            author_id=cand.author_id,
            author_name=cand.author_label,
            text=truncate(cand.text, 4000),
            confidence=verdict.confidence,
            category=verdict.category,
        )
        keyboard = build_keyboard(hit_id, cand.link, cand.author_id is not None)
        return await self.send_text(render(cand, verdict), keyboard)

    async def send_text(self, text: str, keyboard: InlineKeyboardMarkup | None = None) -> bool:
        if self.runtime.owner_id is None:
            return False
        for attempt in range(3):
            try:
                await self.bot.send_message(
                    self.runtime.owner_id,
                    text,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                return True
            except TelegramRetryAfter as exc:
                log.warning("Телеграм просит подождать %s с", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                log.error("Владелец заблокировал бота — уведомления не доходят")
                return False
            except Exception as exc:                     # noqa: BLE001
                log.error("Не смог отправить уведомление (попытка %s): %s", attempt + 1, exc)
                await asyncio.sleep(2)
        return False
