"""Бэкенд по API-ключу: любой OpenAI-совместимый эндпоинт (по умолчанию aitunnel).

Серверной памяти у такого API нет, поэтому «продолжение чата» — это хвост
истории, который ChatSession подкладывает в messages.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from ..config import Config
from .base import LLMBackend, LLMError
from .session import ChatSession


class OpenAICompatBackend(LLMBackend):
    name = "ключ"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = AsyncOpenAI(
            api_key=cfg.llm_key or "missing",
            base_url=cfg.llm_base_url,
            timeout=cfg.llm_timeout,
            max_retries=0,              # ретраим уровнем выше, с логом
        )

    @property
    def available(self) -> bool:
        return bool(self.cfg.llm_key)

    async def ask(self, session: ChatSession, system: str, user: str,
                  max_tokens: int) -> str:
        model = self.cfg.model_for(session.name)
        session_id = await session.begin()
        messages = session.messages(system, user)
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0,
                max_tokens=max_tokens,
            )
        except Exception as exc:                          # noqa: BLE001
            raise LLMError(str(exc)) from exc

        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise LLMError("пустой ответ модели")
        await session.remember(session_id, user, content)
        return content

    def status(self) -> str:
        base = self.cfg.llm_base_url.replace("https://", "")
        return f"ключ · {base}"

    async def close(self) -> None:
        await self._client.close()
