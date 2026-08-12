"""Датаклассы, которые ходят по пайплайну."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Candidate:
    """Одно сообщение из чата — кандидат в заказы."""

    chat_id: int
    msg_id: int
    text: str
    ts: int
    chat_title: str = ""
    chat_username: str | None = None
    author_id: int | None = None
    author_name: str = ""
    author_username: str | None = None
    is_channel: bool = False

    @property
    def key(self) -> str:
        """Уникальный ключ сообщения (используется как id внутри батча)."""
        return f"{self.chat_id}:{self.msg_id}"

    @property
    def link(self) -> str | None:
        """Ссылка на сообщение в Telegram (t.me)."""
        if self.chat_username:
            return f"https://t.me/{self.chat_username}/{self.msg_id}"
        raw = str(self.chat_id)
        if raw.startswith("-100"):
            return f"https://t.me/c/{raw[4:]}/{self.msg_id}"
        return None

    @property
    def author_label(self) -> str:
        name = self.author_name or "неизвестно"
        if self.author_username:
            return f"{name} (@{self.author_username})"
        return name


@dataclass
class Verdict:
    """Результат 2-го этапа проверки."""

    is_order: bool
    confidence: float = 0.0
    category: str = ""       # order / vacancy / other
    stack: str = ""          # что просят сделать / технологии
    budget: str = ""         # бюджет, если указан
    reason: str = ""         # короткое объяснение модели

    @classmethod
    def from_dict(cls, data: dict) -> "Verdict":
        def _s(key: str) -> str:
            value = data.get(key)
            if value is None or isinstance(value, (dict, list)):
                return ""
            return str(value).strip()

        raw_conf = data.get("confidence", 0)
        try:
            confidence = float(raw_conf)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence > 1:                       # модель могла ответить в процентах
            confidence = confidence / 100.0
        confidence = max(0.0, min(1.0, confidence))

        raw_flag = data.get("is_order", data.get("order", False))
        if isinstance(raw_flag, str):
            is_order = raw_flag.strip().lower() in {"true", "yes", "1", "да"}
        else:
            is_order = bool(raw_flag)

        return cls(
            is_order=is_order,
            confidence=confidence,
            category=_s("category"),
            stack=_s("stack"),
            budget=_s("budget"),
            reason=_s("reason"),
        )
