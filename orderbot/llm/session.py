"""Чат с моделью: политика жизни и состояние.

Каждый этап проверки ведёт свой непрерывный чат, а не заводит новый диалог
на каждый запрос. Раз в `ttl` секунд чат меняется на свежий.

Состояние переживает рестарт: id, возраст, история и payload бэкенда
сохраняются в базу и поднимаются обратно, поэтому после перезапуска бот
продолжает тот же чат, а не плодит новый.
"""

from __future__ import annotations

import asyncio
import time

from ..utils import log, truncate


class ChatSession:
    def __init__(self, name: str, ttl: float, history_turns: int,
                 remember_chars: int = 400):
        self.name = name
        self.ttl = max(60.0, ttl)
        self.history_turns = max(0, history_turns)
        self.remember_chars = remember_chars
        self.requests = 0
        self.rotations = 0
        self.session_id = ""
        self.started = 0.0
        self.dirty = False
        # Состояние бэкенда, привязанное к этому чату: для аккаунтов — id
        # аккаунта и id серверного чата. Обнуляется при смене чата.
        self.payload: dict = {}
        self._seq = 0
        self._history: list[dict[str, str]] = []
        self._lock = asyncio.Lock()
        self._start_new()

    # ------------------------------------------------------------------ жизненный цикл

    def _start_new(self) -> None:
        # Номер в id обязателен: две смены внутри одной секунды иначе дали бы
        # одинаковый id, и ответ из старого чата дописался бы в новый.
        self._seq += 1
        self.session_id = f"{self.name}-{int(time.time())}-{self._seq}"
        self.started = time.time()
        self._history.clear()
        self.payload = {}
        self.dirty = True

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.started)

    async def begin(self) -> str:
        """id текущего чата. При необходимости чат сменится на новый."""
        async with self._lock:
            if self.age >= self.ttl:
                self.rotations += 1
                log.info("Чат %s прожил %.0f мин — перехожу в новый",
                         self.session_id, self.age / 60)
                self._start_new()
            self.requests += 1
            return self.session_id

    # ------------------------------------------------------------------ история

    def messages(self, system: str, user: str) -> list[dict[str, str]]:
        """Готовые messages для бэкенда без серверной памяти."""
        return [
            {"role": "system", "content": system},
            *self._history,
            {"role": "user", "content": user},
        ]

    async def remember(self, session_id: str, user: str, answer: str) -> None:
        if self.history_turns <= 0:
            return
        async with self._lock:
            if session_id != self.session_id:
                return                    # чат успел смениться, дописывать некуда
            self._history.append(
                {"role": "user", "content": truncate(user, self.remember_chars)})
            self._history.append(
                {"role": "assistant", "content": truncate(answer, self.remember_chars)})
            extra = len(self._history) - self.history_turns * 2
            if extra > 0:
                del self._history[:extra]

    # ------------------------------------------------------------------ сохранение

    def dump(self) -> dict:
        return {
            "session_id": self.session_id,
            "started": self.started,
            "seq": self._seq,
            "requests": self.requests,
            "rotations": self.rotations,
            "history": list(self._history),
            "payload": dict(self.payload),
        }

    def restore(self, data: dict) -> None:
        if not isinstance(data, dict) or not data.get("session_id"):
            return
        self.session_id = str(data["session_id"])
        self.started = float(data.get("started") or time.time())
        self._seq = int(data.get("seq") or 1)
        self.requests = int(data.get("requests") or 0)
        self.rotations = int(data.get("rotations") or 0)
        self.payload = dict(data.get("payload") or {})
        self._history = [m for m in (data.get("history") or [])
                         if isinstance(m, dict) and m.get("role") and m.get("content")]
        self.dirty = False
        log.info("Чат %s поднят из базы: возраст %.0f мин, %s запр.",
                 self.session_id, self.age / 60, self.requests)

    def info(self) -> str:
        return (f"{self.session_id} · {int(self.age // 60)} мин · "
                f"{self.requests} запр. · смен: {self.rotations}")
