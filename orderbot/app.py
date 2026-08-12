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
from .notifier import Notifier
from .pipeline import Pipeline
from .state import Runtime
from .userbot import UserBot
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

    classifier = Classifier(cfg)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db, runtime)
    pipeline = Pipeline(cfg, runtime, dedup, classifier, notifier)
    userbot = UserBot(cfg, db, pipeline.ingest)

    deps = Deps(
        cfg=cfg,
        db=db,
        runtime=runtime,
        userbot=userbot,
        pipeline=pipeline,
        classifier=classifier,
        dedup=dedup,
    )
    dp = build_dispatcher(deps)

    pipeline.start()
    cleanup_task = asyncio.create_task(_cleanup_loop(dedup), name="dedup-cleanup")

    if await userbot.try_start_saved():
        account = userbot.account
        log.info("Сессия восстановлена: %s", account.name if account else "?")
        if runtime.owner_id:
            await notifier.send_text(
                "♻️ Перезапустился и слушаю чаты дальше."
                + (" Сейчас на паузе — /resume" if runtime.paused else "")
            )
    else:
        log.warning("Аккаунт не подключён — залогинься через бота: /login")
        if runtime.owner_id:
            await notifier.send_text("♻️ Перезапустился. Аккаунт не подключён — /login")

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
        await userbot.stop()
        await classifier.close()
        await db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
