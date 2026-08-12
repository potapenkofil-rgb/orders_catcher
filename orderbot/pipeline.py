"""Пайплайн: сообщение → предфильтр → батч → этап 1 → этап 2 → уведомление."""

from __future__ import annotations

import asyncio
import time

from .buffer import BatchBuffer
from .classifier import Classifier
from .config import Config
from .dedup import DedupIndex
from .models import Candidate, Verdict
from .notifier import Notifier
from .state import Runtime
from .utils import log


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        runtime: Runtime,
        dedup: DedupIndex,
        classifier: Classifier,
        notifier: Notifier,
    ):
        self.cfg = cfg
        self.runtime = runtime
        self.dedup = dedup
        self.classifier = classifier
        self.notifier = notifier
        self.buffer: BatchBuffer[Candidate] = BatchBuffer(
            cfg.batch_size, cfg.batch_timeout, self.process_batch
        )
        self.last_batch_at: float = 0.0
        self.batches_done: int = 0

    def start(self) -> None:
        self.buffer.start()

    async def stop(self) -> None:
        await self.buffer.stop(flush=False)

    # ------------------------------------------------------------------ приём сообщений

    async def ingest(self, cand: Candidate) -> None:
        """Дешёвый предфильтр перед батчем — тут не тратим ни копейки на LLM."""
        db = self.runtime.db
        await db.bump("seen")

        if self.runtime.paused:
            return
        if self.runtime.is_banned(cand.chat_id, cand.author_id):
            await db.bump("skipped_blacklist")
            return

        text = (cand.text or "").strip()
        if len(text) < self.cfg.min_text_len:
            return
        if text.startswith("/"):                       # команды ботов
            return

        # Дубликаты режем до LLM: одно объявление часто висит в десятке чатов.
        if not await self.dedup.check_and_add(text):
            await db.bump("skipped_duplicate")
            return

        await db.bump("queued")
        self.buffer.add(cand)

    # ------------------------------------------------------------------ обработка батча

    async def process_batch(self, batch: list[Candidate]) -> None:
        if not batch:
            return
        started = time.monotonic()
        db = self.runtime.db

        passed = await self.classifier.stage1(batch)
        await db.bump("stage1_passed", len(passed))
        log.info("Этап 1: %s из %s прошли", len(passed), len(batch))

        if not passed:
            self._finish(started, batch, 0, 0)
            return

        verdicts = await asyncio.gather(
            *(self.classifier.stage2(cand) for cand in passed),
            return_exceptions=True,
        )

        sent = 0
        confirmed = 0
        for cand, verdict in zip(passed, verdicts):
            if isinstance(verdict, BaseException):
                log.error("Этап 2 упал для %s: %s", cand.key, verdict)
                continue
            if not self._accept(verdict):
                continue
            confirmed += 1
            # ЧС мог пополниться, пока крутились этапы, — перепроверяем перед отправкой.
            if self.runtime.is_banned(cand.chat_id, cand.author_id):
                continue
            if await self.notifier.send_hit(cand, verdict):
                sent += 1

        await db.bump("stage2_passed", confirmed)
        await db.bump("sent", sent)
        self._finish(started, batch, confirmed, sent)

    def _accept(self, verdict: Verdict) -> bool:
        if not verdict.is_order:
            return False
        if verdict.confidence < self.runtime.min_confidence:
            log.debug("Отбраковка по порогу: %.2f < %.2f",
                      verdict.confidence, self.runtime.min_confidence)
            return False
        if verdict.category == "other":
            return False
        return True

    def _finish(self, started: float, batch: list[Candidate], confirmed: int, sent: int) -> None:
        self.batches_done += 1
        self.last_batch_at = time.time()
        log.info(
            "Батч обработан за %.1f с: %s сообщений → %s подтверждено → %s отправлено",
            time.monotonic() - started, len(batch), confirmed, sent,
        )
