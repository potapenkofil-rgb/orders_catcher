"""Общий интерфейс бэкендов LLM."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .session import ChatSession


class LLMError(RuntimeError):
    """Бэкенд не смог ответить — сеть, лимит, бан, кривой ответ."""


class LLMBackend(ABC):
    name: str = "?"

    @abstractmethod
    async def ask(self, session: ChatSession, system: str, user: str,
                  max_tokens: int) -> str:
        """Один вопрос в рамках чата `session`. Кидает LLMError при неудаче."""

    @property
    def available(self) -> bool:
        """Есть ли чем работать: ключ задан / есть живой аккаунт."""
        return True

    def status(self) -> str:
        """Короткая строка для /status."""
        return self.name

    async def close(self) -> None:
        return None
