"""Результат пинг-проверки — общий для ds.ping() (аккаунт) и chat.ping() (конкретный чат)."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class PingResult:
    ok: bool
    latency_ms: float
    error: Optional[str] = None

    def __bool__(self):
        return self.ok

    def __repr__(self):
        state = "ok" if self.ok else f"FAIL ({self.error})"
        return f"<PingResult {state}, {self.latency_ms:.0f}ms>"
