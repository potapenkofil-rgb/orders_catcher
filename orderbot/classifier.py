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

from .config import Config
from .db import Database
from .llm import ChatSession, LLMBackend, LLMError
from .models import Candidate, Verdict
from .utils import log, truncate

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Дешёвый резервный фильтр — используется, только если LLM недоступна на 1-м этапе.
_FALLBACK_RE = re.compile(
    r"ищ[уеё]м?\s|нужен|нужна|нужно|требуетс|ищем|разработчик|программист|исполнител|"
    r"фрилансер|вакансия|подработк|заказ|проект|бюджет|оплат|заплач|ставка|тз\b|"
    r"техзадани|сделать|написать|доработ|допилить|почин|верстальщик|бэкенд|backend|"
    r"фронтенд|frontend|бот[аеу]?\b|парсер|сайт|скрипт|автоматизац|интеграц|api\b|"
    # готовый телеграм-софт, который ищут купить
    r"рассылк|рассыл\b|инвайт|ловец\s*чек|чек[оа]лов|крипточек|автоответ|мониторинг|"
    r"юзербот|накрут|отметк\w*\s+в\s+истори|куплю|где\s+взять|софт\b",
    re.IGNORECASE,
)

# Короткие лиды вроде «ищу бота для рассылки» (21 символ) не проходят по длине
# в общем предфильтре, хотя это самые чистые заявки. Пускаем их в обход порога:
# нужен и запрос («ищу», «куплю»), и предмет («бот», «инвайт», «чек») рядом.
SHORT_LEAD_RE = re.compile(
    r"(ищ[уеё]м?|нужен|нужна|нужно|надо|куплю|купить|где\s+взять|где\s+купить|"
    r"подскажите|посоветуйте|интересует|есть\s+ли|скинь\w*|киньте)\b[^\n]{0,40}?"
    r"(бот|софт|скрипт|рассылк|инвайт|чек|парсер|автоответ|мониторинг|накрут|юзербот)",
    re.IGNORECASE,
)

STAGE1_SYSTEM = """Ты — фильтр первого этапа для IT-фрилансера. Тебе дают пачку сообщений из телеграм-чатов и каналов.

Профиль фрилансера:
{profile}

Задача: оставить номера сообщений, которые МОГУТ быть предложением оплачиваемой работы для этого человека.

Оставляй (при сомнениях — оставляй) сообщения ДВУХ видов.

Вид А — ищут исполнителя:
- ищут разработчика/специалиста под задачу
- разовые задачи, проекты, подработки, вакансии, доработка существующего кода
- есть описание задачи, ТЗ, бюджет, оплата или ставка

Вид Б — ищут ГОТОВЫЙ телеграм-софт или бота (купить, где взять, заказать):
- рассылка по группам, рассылка в ЛС, инвайтинг, отметки в историях
- парсер участников чатов, поиск и подбор чатов
- ловец крипточеков, мониторинг чатов по ключевым словам, автоответчик
- накрутка, многофункциональный юзербот, любой другой готовый бот или скрипт
Такие сообщения короткие: «ищу бота для рассылки», «нужен ловец чеков»,
«где взять инвайтер», «куплю софт для парсинга». Всё равно оставляй.

Выкидывай:
- резюме и самопрезентации («ищу работу», «готов взяться», «мое портфолио», «делаю сайты недорого»)
- ПРОДАЖУ и рекламу такого софта: «продам софт», «мой бот для рассылки», прайс,
  тарифы, отзывы, ссылка на своего бота
- вопросы «как это сделать самому», «на чём написать», «подскажите библиотеку»
- отклики на чужие объявления, обсуждения, вопросы по коду, флуд, мемы
- рекламу курсов, бирж, каналов, крипты, ставок, скам
- вакансии и заказы не про IT (курьеры, продажи, дизайн интерьера и т.п.)
- поиск поставщиков, партнёров, людей с чужими аккаунтами; схемы вокруг
  маркетплейсов (сплиты и выкупы на ВБ и Озоне, кэшбэк), обнал, дропы —
  там нужен подельник, а не разработчик, даже если упомянуты боты и QR-коды
- сообщения без конкретики (одно слово, только ссылка, только смайлы)

Отвечай ТОЛЬКО валидным JSON без markdown и пояснений:
{{"ids": [1, 4, 7]}}
Если ничего не подошло: {{"ids": []}}"""

STAGE2_SYSTEM = """Ты — второй, строгий этап проверки объявлений для IT-фрилансера.

Профиль фрилансера:
{profile}

Реши, является ли сообщение лидом — тем, на что этому человеку имеет смысл откликнуться.

is_order = true в ДВУХ случаях.

А) Заказ на разработку. Всё должно быть верно:
1. Автор ИЩЕТ исполнителя (а не предлагает свои услуги и не ищет работу себе).
2. Результат работы — КОД: программа, скрипт, бот, сайт, парсер, интеграция.
   Если автору нужен человек, который будет делать что-то руками, либо чужой
   аккаунт, товар, доступ или посредник — это НЕ заказ, даже если в сообщении
   мелькают QR-коды, боты, автоматизация и прочие технические слова.
3. Есть конкретика: что нужно сделать, или ТЗ, или бюджет, или явный призыв писать в личку по задаче.
   category = "order" (разовая задача) или "vacancy" (вакансия, постоянная работа).

Б) Ищут ГОТОВЫЙ телеграм-софт или бота — даже без ТЗ и без бюджета:
1. Автор ищет, ГДЕ ВЗЯТЬ, КУПИТЬ или ЗАКАЗАТЬ такой софт. Он его не продаёт
   и не спрашивает, как написать самому.
2. Речь про рассылку по группам, рассылку в ЛС, инвайтинг, отметки в историях,
   парсер участников чатов, поиск чатов, ловец крипточеков, мониторинг чатов,
   автоответчик, накрутку, многофункциональный юзербот — или любой другой
   готовый бот/скрипт, который автор хочет получить.
   category = "softbot", stack = перечисли нужные функции.
   Короткого «ищу бота для рассылки по группам» достаточно, это готовый лид.

is_order = false для:
- резюме и самопиара, откликов на чужие объявления
- ПРОДАВЦОВ софта: «продам», «мой софт», прайс, тарифы, отзывы, ссылка на своего бота
- вопросов «как написать такого бота», «на чём делать», «какую библиотеку взять»
- «кто-нибудь пользовался?», обсуждений и жалоб без запроса «где взять»
- рекламы услуг, курсов, бирж, каналов, скама и крипты
- вакансий не про IT, обсуждений, пустых сообщений без сути
- «ищу поставщика», «ищу партнёра», «ищу того, кто оформит/выкупит/примет» —
  тут нужен человек с аккаунтом, товаром или услугой, а не разработчик
- схем вокруг маркетплейсов и платежей: сплиты и выкупы на ВБ и Озоне,
  самовыкупы, кэшбэк-схемы, обнал, дропы, продажа доступов и аккаунтов.
  Даже когда там фигурируют боты, QR-коды и слово «автоматизация» — это
  поиск подельника, а не заказ на разработку

Не додумывай задачу за автора. Если в сообщении не сказано, что именно нужно
запрограммировать, — значит, этого там и нет: is_order = false.

confidence — насколько ты уверен, число от 0 до 1.
Снижай confidence, если автор хочет исключительно бесплатно («скиньте халявный»,
«есть бесплатный?») — платить он, скорее всего, не станет.
stack — что именно нужно, коротко.
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
    def __init__(self, cfg: Config, backend: LLMBackend, db: Database | None = None):
        self.cfg = cfg
        self.backend = backend
        self.db = db
        self._sem2 = asyncio.Semaphore(max(1, cfg.stage2_concurrency))
        self.last_error: str = ""
        self.chat1 = ChatSession("stage1", cfg.chat_ttl, cfg.chat_history_turns)
        self.chat2 = ChatSession("stage2", cfg.chat_ttl, cfg.chat_history_turns)
        self._warned_no_backend = False

    @property
    def available(self) -> bool:
        return self.backend.available

    # ------------------------------------------------------------------ состояние чатов

    async def load_state(self) -> None:
        """Поднимает чаты из базы, чтобы после рестарта не заводить новые."""
        if self.db is None:
            return
        state = await self.db.get("chat_state") or {}
        for chat in (self.chat1, self.chat2):
            chat.restore(state.get(chat.name) or {})

    async def save_state(self) -> None:
        if self.db is None:
            return
        if not (self.chat1.dirty or self.chat2.dirty):
            return
        try:
            await self.db.set("chat_state", {
                self.chat1.name: self.chat1.dump(),
                self.chat2.name: self.chat2.dump(),
            })
        except Exception as exc:                          # noqa: BLE001
            # База может быть уже закрыта при выключении — это не повод падать
            # в finally и прятать настоящую причину остановки.
            log.debug("Состояние чатов не сохранилось: %s", exc)
            return
        self.chat1.dirty = self.chat2.dirty = False

    async def close(self) -> None:
        await self.save_state()
        await self.backend.close()

    # ------------------------------------------------------------------- низкий уровень

    async def _ask(self, system: str, user: str, max_tokens: int,
                   chat: ChatSession, attempts: int = 3) -> str:
        delay = 1.5
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                content = await self.backend.ask(chat, system, user, max_tokens)
                self.last_error = ""
                await self.save_state()
                return content
            except LLMError as exc:
                last_exc = exc
                log.warning("LLM: попытка %s/%s не удалась: %s", attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(delay)
                delay *= 2
        self.last_error = str(last_exc) if last_exc else "неизвестная ошибка"
        await self.save_state()
        raise RuntimeError(f"LLM недоступна: {self.last_error}")

    # ------------------------------------------------------------------- этап 1

    async def stage1(self, items: list[Candidate]) -> list[Candidate]:
        """Батч-фильтр. Возвращает подмножество кандидатов."""
        if not items:
            return []
        if not self.backend.available:
            if not self._warned_no_backend:
                log.error("Нечем проверять сообщения: добавь аккаунт (/addaccount) "
                          "или ключ в .env. Пропускаю батчи.")
                self._warned_no_backend = True
            return []
        self._warned_no_backend = False

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
            STAGE1_SYSTEM.format(profile=self.cfg.profile),
            user,
            max_tokens=400,
            chat=self.chat1,
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
                    STAGE2_SYSTEM.format(profile=self.cfg.profile),
                    user,
                    max_tokens=300,
                    chat=self.chat2,
                )
            except Exception as exc:                       # noqa: BLE001
                log.error("Этап 2 не отработал для %s: %s", cand.key, exc)
                return Verdict(is_order=False, reason=f"ошибка проверки: {exc}")

        data = extract_json(raw)
        if not data:
            log.warning("Этап 2: не распарсил ответ: %s", truncate(raw, 200))
            return Verdict(is_order=False, reason="не удалось разобрать ответ модели")
        return Verdict.from_dict(data)
