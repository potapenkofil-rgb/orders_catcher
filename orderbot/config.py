"""Конфигурация из переменных окружения / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> None:
    """Мини-парсер .env (чтобы не тянуть python-dotenv).

    Существующие переменные окружения имеют приоритет над файлом.
    """
    path = path or (BASE_DIR / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


DEFAULT_PROFILE = (
    "IT-фрилансер широкого профиля. Пишу код на любом языке и стеке: "
    "телеграм-боты и юзерботы, парсеры и скрейперы, автоматизация, бэкенд и API, "
    "веб-сайты и лендинги, десктоп-утилиты, интеграции, работа с данными, "
    "ML/AI-обвязки, доработка и починка чужого кода. "
    "Отдельно делаю и продаю готовый телеграм-софт: рассылка по группам и в ЛС, "
    "инвайтинг, отметки в историях, парсер участников чатов, поиск чатов, "
    "ловец крипточеков, мониторинг чатов, автоответчик."
)


@dataclass
class Config:
    # --- Telegram ---
    bot_token: str
    owner_id: int | None

    # --- LLM ---
    llm_backend: str           # auto | key | accounts
    account_cooldown: float    # пауза для аккаунта после ошибки, секунды
    llm_key: str
    llm_base_url: str
    model_stage1: str
    model_stage2: str
    llm_timeout: float
    stage1_chunk: int          # сколько сообщений в одном промпте на 1-м этапе
    stage2_concurrency: int    # сколько параллельных проверок на 2-м этапе
    chat_ttl: float            # сколько живёт один чат с моделью (у каждого этапа свой)
    chat_history_turns: int    # сколько прошлых обменов тащим в следующий запрос

    # --- Логика батчинга ---
    batch_size: int            # 100 сообщений
    batch_timeout: float       # или 5 минут — что раньше

    # --- Фильтрация ---
    min_confidence: float
    min_text_len: int
    max_text_len: int          # обрезка длинных полотен перед отправкой в LLM

    # --- Дедупликация ---
    dedup_ttl_days: int
    dedup_hamming: int         # порог похожести simhash (0 = только точные дубли)

    # --- Прочее ---
    db_path: Path
    profile: str
    debug: bool

    @classmethod
    def load(cls) -> "Config":
        load_env_file()
        owner_raw = os.environ.get("OWNER_ID", "").strip()
        db_path = os.environ.get("DB_PATH", "").strip()
        return cls(
            bot_token=os.environ.get("BOT_TOKEN", "").strip(),
            owner_id=int(owner_raw) if owner_raw.lstrip("-").isdigit() else None,
            llm_backend=(os.environ.get("LLM_BACKEND", "auto").strip().lower() or "auto"),
            account_cooldown=_float("ACCOUNT_COOLDOWN_MINUTES", 20.0) * 60,
            llm_key=(os.environ.get("LLM_API_KEY") or os.environ.get("AITUNNEL_KEY") or "").strip(),
            llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.aitunnel.ru/v1").strip(),
            model_stage1=os.environ.get("MODEL_STAGE1", "gemini-3.6-flash").strip(),
            model_stage2=os.environ.get("MODEL_STAGE2", "claude-haiku-4.5").strip(),
            llm_timeout=_float("LLM_TIMEOUT", 90.0),
            stage1_chunk=_int("STAGE1_CHUNK", 25),
            stage2_concurrency=_int("STAGE2_CONCURRENCY", 4),
            chat_ttl=_float("CHAT_TTL_HOURS", 6.0) * 3600,
            chat_history_turns=_int("CHAT_HISTORY_TURNS", 1),
            batch_size=_int("BATCH_SIZE", 100),
            batch_timeout=_float("BATCH_TIMEOUT", 300.0),
            min_confidence=_float("MIN_CONFIDENCE", 0.65),
            min_text_len=_int("MIN_TEXT_LEN", 25),
            max_text_len=_int("MAX_TEXT_LEN", 3000),
            dedup_ttl_days=_int("DEDUP_TTL_DAYS", 7),
            dedup_hamming=_int("DEDUP_HAMMING", 12),
            db_path=Path(db_path) if db_path else BASE_DIR / "data" / "orderbot.db",
            profile=os.environ.get("PROFILE", DEFAULT_PROFILE).strip(),
            debug=bool(os.environ.get("DEBUG", "").strip()),
        )

    def model_for(self, stage: str) -> str:
        """Модель для этапа — нужна только бэкенду по ключу."""
        return self.model_stage2 if stage == "stage2" else self.model_stage1

    def validate(self) -> list[str]:
        """Фатальные проблемы конфига (пустой список = можно стартовать).

        Отсутствие ключа фатальным не считается: аккаунты нейросети добавляются
        через бота уже на ходу, и ради этого бот должен подняться.
        """
        problems: list[str] = []
        if not self.bot_token:
            problems.append("BOT_TOKEN не задан (получи токен у @BotFather)")
        if self.llm_backend not in ("auto", "key", "accounts"):
            problems.append("LLM_BACKEND должен быть auto, key или accounts")
        if self.batch_size < 1:
            problems.append("BATCH_SIZE должен быть >= 1")
        if self.batch_timeout < 5:
            problems.append("BATCH_TIMEOUT должен быть >= 5 секунд")
        if self.chat_ttl < 60:
            problems.append("CHAT_TTL_HOURS слишком мал — минимум 0.02 (около минуты)")
        return problems
