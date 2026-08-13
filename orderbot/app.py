"""Сборка всех частей и запуск."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from .bot import Deps, build_dispatcher
from .classifier import Classifier
from .config import Config
from .db import Database
from .dedup import DedupIndex
from .llm import build_router
from .notifier import Notifier
from .pipeline import Pipeline
from .state import Runtime
from .userbot import UserBotManager
from .utils import log, setup_logging

CLEANUP_INTERVAL = 6 * 3600


async def _cleanup_loop(dedup: DedupIndex) -> None:
    """Раз в несколько часов чистим протухшие записи антидубля."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            removed = await dedup.cleanup()
            if removed:
                log.info("Антидубль: удалено %s старых записей", removed)
        except asyncio.CancelledError:
            raise
        except Exception:                                 # noqa: BLE001
            log.exception("Чистка антидубля не удалась")


async def run() -> None:
    cfg = Config.load()
    setup_logging(cfg.debug)

    problems = cfg.validate()
    if problems:
        for problem in problems:
            log.error("Конфиг: %s", problem)
        log.error("Заполни .env (см. .env.example) и запусти снова")
        return

    db = Database(cfg.db_path)
    await db.init()

    runtime = Runtime(cfg, db)
    await runtime.load()

    dedup = DedupIndex(db, cfg.dedup_ttl_days, cfg.dedup_hamming)
    known = await dedup.load()
    log.info("Антидубль: поднято %s записей", known)

    llm = await build_router(cfg, db)
    classifier = Classifier(cfg, llm, db)
    await classifier.load_state()
    log.info("Проверка сообщений: %s", llm.status())

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db, runtime)
    pipeline = Pipeline(cfg, runtime, dedup, classifier, notifier)
    userbot = UserBotManager(cfg, db, pipeline.ingest)
    await userbot.load()

    deps = Deps(
        cfg=cfg,
        db=db,
        runtime=runtime,
        userbot=userbot,
        pipeline=pipeline,
        classifier=classifier,
        dedup=dedup,
        router=llm,
    )
    dp = build_dispatcher(deps)

    pipeline.start()
    cleanup_task = asyncio.create_task(_cleanup_loop(dedup), name="dedup-cleanup")

    if not llm.available:
        log.warning("Нечем проверять сообщения: нет ни аккаунтов, ни ключа")
        if runtime.owner_id:
            await notifier.send_text(
                "⚠️ Проверять сообщения нечем.\n\n"
                "Добавь аккаунт нейросети — /addaccount, либо пропиши "
                "LLM_API_KEY в .env. Пока лиды искаться не будут."
            )

    started = await userbot.start_all()
    total = len(userbot.accounts)
    if started:
        log.info("Подняты телеграм-аккаунты: %s из %s", started, total)
        if runtime.owner_id:
            note = f"♻️ Перезапустился, слушаю чаты. Аккаунтов на связи: {started}"
            if started < total:
                note += f" из {total} — остальные не поднялись, посмотри /accounts"
            if runtime.paused:
                note += "\nСейчас на паузе — /resume"
            await notifier.send_text(note)
    else:
        log.warning("Ни одного телеграм-аккаунта — залогинься через бота: /login")
        if runtime.owner_id:
            await notifier.send_text(
                "♻️ Перезапустился. Телеграм-аккаунтов нет — подключи через /login"
            )

    try:
        await dp.start_polling(bot, handle_signals=True)
    except TelegramUnauthorizedError:
        log.error("Телеграм не принял BOT_TOKEN — проверь токен у @BotFather")
    except TelegramNetworkError as exc:
        log.error("Нет связи с api.telegram.org: %s", exc)
    finally:
        log.info("Останавливаюсь…")
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
        await pipeline.stop()
        await userbot.stop_all()
        await classifier.close()
        await db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
