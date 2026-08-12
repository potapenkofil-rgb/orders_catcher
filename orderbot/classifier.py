"""Двухэтапная LLM-проверка сообщений.

Этап 1 — дешёвый батч-фильтр: пачка сообщений в одном промпте, модель
возвращает номера тех, что похожи на заказ. Настроен на recall (лучше
пропустить сомнительное дальше, чем потерять заказ).

Этап 2 — точная проверка: каждое выжившее сообщение уходит отдельным
запросом, модель отвечает структурированным вердиктом. Настроен на precision.
"""

from __future__ import annotations

import asyncio
import json
import re

from openai import AsyncOpenAI

from .config import Config
from .models import Candidate, Verdict
from .utils import log, truncate

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Дешёвый резервный фильтр — используется, только если LLM недоступна на 1-м этапе.
_FALLBACK_RE = re.compile(
    r"ищ[уеё]м?\s|нужен|нужна|нужно|требуетс|ищем|разработчик|программист|исполнител|"
    r"фрилансер|вакансия|подработк|заказ|проект|бюджет|оплат|заплач|ставка|тз\b|"
    r"техзадани|сделать|написать|доработ|допилить|почин|верстальщик|бэкенд|backend|"
    r"фронтенд|frontend|бот[аеу]?\b|парсер|сайт|скрипт|автоматизац|интеграц|api\b",
    re.IGNORECASE,
)

STAGE1_SYSTEM = """Ты — фильтр первого этапа для IT-фрилансера. Тебе дают пачку сообщений из телеграм-чатов и каналов.

Профиль фрилансера:
{profile}

Задача: оставить номера сообщений, которые МОГУТ быть предложением оплачиваемой работы для этого человека.

Оставляй (при сомнениях — оставляй):
- ищут исполнителя/разработчика/специалиста под задачу
- разовые задачи, проекты, подработки, вакансии, доработка существующего кода
- есть описание задачи, ТЗ, бюджет, оплата или ставка

Выкидывай:
- резюме и самопрезентации («ищу работу», «готов взяться», «мое портфолио», «делаю сайты недорого»)
- отклики на чужие объявления, обсуждения, вопросы по коду, флуд, мемы
- рекламу курсов, бирж, каналов, крипты, ставок, скам
- вакансии и заказы не про IT (курьеры, продажи, дизайн интерьера и т.п.)
- сообщения без конкретики (одно слово, только ссылка, только смайлы)

Отвечай ТОЛЬКО валидным JSON без markdown и пояснений:
{{"ids": [1, 4, 7]}}
Если ничего не подошло: {{"ids": []}}"""

STAGE2_SYSTEM = """Ты — второй, строгий этап проверки объявлений для IT-фрилансера.

Профиль фрилансера:
{profile}

Реши, является ли сообщение реальным предложением оплачиваемой работы, на которое этому человеку имеет смысл откликнуться.

is_order = true, только если ВСЁ верно:
1. Автор ИЩЕТ исполнителя (а не предлагает свои услуги и не ищет работу себе).
2. Задача относится к IT/разработке/автоматизации — то, что можно сделать кодом.
3. Есть конкретика: что нужно сделать, или ТЗ, или бюджет, или явный призыв писать в личку по задаче.

is_order = false для: резюме и самопиара, откликов, рекламы услуг/курсов/бирж/каналов,
вакансий не про IT, обсуждений и вопросов по коду, скама и крипты, пустых сообщений
без сути, объявлений о поиске сотрудников в штат с офисом в другой стране без удалёнки.

category: "order" — разовый заказ/проект, "vacancy" — вакансия/постоянная работа, "other" — не подходит.
confidence — насколько ты уверен, число от 0 до 1.
stack — что именно нужно сделать и на чём (коротко).
budget — бюджет/оплата дословно, если указан, иначе "".
reason — до 15 слов, почему такой вердикт.

Отвечай ТОЛЬКО валидным JSON без markdown и пояснений:
{{"is_order": true, "confidence": 0.9, "category": "order", "stack": "телеграм-бот на Python", "budget": "10000р", "reason": "ищут исполнителя, есть ТЗ и бюджет"}}"""


def extract_json(raw: str) -> dict | None:
    """Достаёт JSON из ответа модели (с фенсами, префиксами и прочим мусором)."""
    if not raw:
        return None
    text = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"ids": data}
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


class Classifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = AsyncOpenAI(
            api_key=cfg.llm_key or "missing",
            base_url=cfg.llm_base_url,
            timeout=cfg.llm_timeout,
            max_retries=0,          # ретраим сами, с логом
        )
        self._sem2 = asyncio.Semaphore(max(1, cfg.stage2_concurrency))
        self.last_error: str = ""

    async def close(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------------- низкий уровень

    async def _ask(self, model: str, system: str, user: str, max_tokens: int,
                   attempts: int = 3) -> str:
        delay = 1.5
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0,
                    max_tokens=max_tokens,
                )
                content = (resp.choices[0].message.content or "").strip()
                if content:
                    self.last_error = ""
                    return content
                last_exc = RuntimeError("пустой ответ модели")
            except Exception as exc:                       # noqa: BLE001 — логируем любую
                last_exc = exc
                log.warning("LLM %s: попытка %s/%s не удалась: %s",
                            model, attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(delay)
                delay *= 2
        self.last_error = str(last_exc) if last_exc else "неизвестная ошибка"
        raise RuntimeError(f"LLM {model} недоступна: {self.last_error}")

    # ------------------------------------------------------------------- этап 1

    async def stage1(self, items: list[Candidate]) -> list[Candidate]:
        """Батч-фильтр. Возвращает подмножество кандидатов."""
        if not items:
            return []

        chunk_size = max(1, self.cfg.stage1_chunk)
        chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
        results = await asyncio.gather(
            *(self._stage1_chunk(chunk) for chunk in chunks),
            return_exceptions=True,
        )

        passed: list[Candidate] = []
        for chunk, result in zip(chunks, results):
            if isinstance(result, BaseException):
                log.error("Этап 1 упал, откатываюсь на ключевые слова: %s", result)
                passed.extend(c for c in chunk if _FALLBACK_RE.search(c.text))
            else:
                passed.extend(result)
        return passed

    async def _stage1_chunk(self, chunk: list[Candidate]) -> list[Candidate]:
        lines = []
        for idx, cand in enumerate(chunk, start=1):
            text = truncate(cand.text.replace("\n", " ").strip(), 400)
            lines.append(f"#{idx} [{truncate(cand.chat_title, 40)}] {text}")
        user = "Сообщения:\n" + "\n".join(lines)

        raw = await self._ask(
            self.cfg.model_stage1,
            STAGE1_SYSTEM.format(profile=self.cfg.profile),
            user,
            max_tokens=400,
        )
        data = extract_json(raw)
        if not data:
            log.warning("Этап 1: не смог распарсить ответ: %s", truncate(raw, 200))
            return [c for c in chunk if _FALLBACK_RE.search(c.text)]

        ids = data.get("ids", [])
        if not isinstance(ids, list):
            return []

        picked: list[Candidate] = []
        for value in ids:
            try:
                num = int(str(value).strip().lstrip("#"))
            except (TypeError, ValueError):
                continue
            if 1 <= num <= len(chunk):
                picked.append(chunk[num - 1])
        return picked

    # ------------------------------------------------------------------- этап 2

    async def stage2(self, cand: Candidate) -> Verdict:
        """Индивидуальная проверка одного сообщения."""
        author = cand.author_label
        chat = cand.chat_title or str(cand.chat_id)
        text = truncate(cand.text, self.cfg.max_text_len)
        user = (
            f"Чат: {chat}\n"
            f"Автор: {author}\n"
            f"Тип: {'канал' if cand.is_channel else 'группа'}\n"
            f"---\n{text}\n---\n"
            "Верни JSON-вердикт."
        )
        async with self._sem2:
            try:
                raw = await self._ask(
                    self.cfg.model_stage2,
                    STAGE2_SYSTEM.format(profile=self.cfg.profile),
                    user,
                    max_tokens=300,
                )
            except Exception as exc:                       # noqa: BLE001
                log.error("Этап 2 не отработал для %s: %s", cand.key, exc)
                return Verdict(is_order=False, reason=f"ошибка проверки: {exc}")

        data = extract_json(raw)
        if not data:
            log.warning("Этап 2: не распарсил ответ: %s", truncate(raw, 200))
            return Verdict(is_order=False, reason="не удалось разобрать ответ модели")
        return Verdict.from_dict(data)
