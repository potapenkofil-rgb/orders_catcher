"""Мелкие утилиты: логирование, экранирование, время."""

from __future__ import annotations

import html
import logging
import time

log = logging.getLogger("orderbot")


def setup_logging(debug: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Телетон и aiogram слишком болтливы на DEBUG
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)   # логирует каждый SQL-вызов


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    """HTML-экранирование для parse_mode=HTML."""
    return html.escape(text or "", quote=False)


def truncate(text: str, limit: int, tail: str = "…") -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(tail))].rstrip() + tail


def human_ts(ts: int) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many
