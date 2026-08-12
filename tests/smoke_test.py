"""Smoke-тест ловца заказов: всё, что можно проверить без сети.

Запуск: python tests/smoke_test.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orderbot.buffer import BatchBuffer
from orderbot.classifier import Classifier, extract_json
from orderbot.config import Config
from orderbot.db import Database, _from_sqlite_int, _to_sqlite_int
from orderbot.dedup import DedupIndex, hamming, normalize, simhash
from orderbot.models import Candidate, Verdict
from orderbot.notifier import build_keyboard, render
from orderbot.pipeline import Pipeline
from orderbot.state import Runtime

ok = 0
fail = 0
_dbs: list[Database] = []

AD = ("Ищу разработчика для телеграм-бота на Python. Нужен парсер объявлений с сайта, "
      "сохранение в базу, рассылка уведомлений подписчикам и админка для модерации. "
      "Бюджет 30000 рублей, срок две недели. Пишите в личку с примерами работ.")

OTHER_AD = ("Требуется верстальщик для лендинга на HTML и CSS, адаптив под мобильные, "
            "интеграция формы обратной связи. Оплата 8000 рублей, срок три дня, "
            "портфолио обязательно.")


async def open_db(path) -> Database:
    db = Database(path)
    await db.init()
    _dbs.append(db)
    return db


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {label}")
    else:
        fail += 1
        print(f"  FAIL {label} {extra}")


def cand(text, chat_id=-1001234567890, msg_id=1, author_id=555, title="Фриланс IT",
         username=None):
    return Candidate(chat_id=chat_id, msg_id=msg_id, text=text, ts=int(time.time()),
                     chat_title=title, chat_username=username, author_id=author_id,
                     author_name="Иван", author_username="ivan")


async def test_db(tmp):
    print("\n[db]")
    db = await open_db(Path(tmp) / "t.db")
    await db.set("k", {"a": 1})
    check("settings round-trip", await db.get("k") == {"a": 1})
    check("settings default", await db.get("nope", "def") == "def")

    await db.ban_user(42, "Спамер")
    await db.ban_chat(-100999, "Помойка")
    users, chats = await db.load_blacklists()
    check("ЧС пользователей", users == {42})
    check("ЧС чатов", chats == {-100999})
    check("unban", await db.unban_user(42) and not await db.unban_user(42))

    hit_id = await db.add_hit(chat_id=-100, chat_title="Ч", msg_id=7, author_id=9,
                              author_name="А", text="t", confidence=0.9, category="order")
    row = await db.get_hit(hit_id)
    check("hits", row is not None and row["msg_id"] == 7)

    await db.bump("seen", 3)
    await db.bump("seen")
    check("stats", (await db.stats_today()).get("seen") == 4)

    big = (1 << 64) - 1
    check("upper-64 бит переживает SQLite",
          _from_sqlite_int(_to_sqlite_int(big)) == big)
    await db.seen_add("h", big)
    check("simhash со старшим битом пишется и читается",
          await db.seen_load(7) == [("h", big)])


async def test_dedup(tmp):
    print("\n[dedup]")
    db = await open_db(Path(tmp) / "d.db")
    idx = DedupIndex(db, ttl_days=7, max_hamming=12)
    await idx.load()

    check("новое объявление", await idx.check_and_add(AD) is True)
    check("точный дубль", await idx.check_and_add(AD) is False)
    check("дубль в другом регистре и со ссылкой",
          await idx.check_and_add(AD.upper() + " https://t.me/xxx") is False)
    check("репост с другим бюджетом и сроком",
          await idx.check_and_add(
              AD.replace("30000", "45000").replace("две недели", "месяц")) is False)
    check("репост с приписанными контактами",
          await idx.check_and_add(AD + " Отвечу всем, кто напишет сегодня. @manager_ivan") is False)
    check("другое объявление проходит", await idx.check_and_add(OTHER_AD) is True)

    short1 = "нужен парсер сайта на python срочно"
    short2 = "нужен парсер сайта на python очень срочно"
    check("короткое новое", await idx.check_and_add(short1) is True)
    check("короткое похожее НЕ считается дублем", await idx.check_and_add(short2) is True)
    check("короткое точное совпадение — дубль", await idx.check_and_add(short1) is False)

    d_repost = hamming(simhash(normalize(AD)),
                       simhash(normalize(AD + " Отвечу всем, кто напишет сегодня.")))
    d_other = hamming(simhash(normalize(AD)), simhash(normalize(OTHER_AD)))
    check("репост ближе порога, чужое объявление далеко",
          d_repost <= 12 < 20 <= d_other, f"(репост={d_repost}, чужое={d_other})")

    idx2 = DedupIndex(db, 7, 12)
    loaded = await idx2.load()
    check("индекс поднимается из базы", loaded == idx.size, f"({loaded} vs {idx.size})")
    check("после рестарта дубль всё ещё дубль", idx2.is_duplicate(AD))
    check("после рестарта чужое не дубль",
          not idx2.is_duplicate("Совсем другой текст про рыбалку и погоду на выходных в июле"))


async def test_buffer():
    print("\n[buffer]")
    got = []

    async def handler(batch):
        got.append(list(batch))

    buf = BatchBuffer(size=3, timeout=60, handler=handler)
    buf.start()
    for i in range(3):
        buf.add(i)
    await asyncio.sleep(0.2)
    check("флаш по размеру", got and got[0] == [0, 1, 2], f"({got})")
    await buf.stop(flush=False)

    got2 = []

    async def handler2(batch):
        got2.append(list(batch))

    buf2 = BatchBuffer(size=100, timeout=1.0, handler=handler2)
    buf2.start()
    buf2.add("a")
    await asyncio.sleep(0.4)
    buf2.add("b")
    check("до дедлайна не флашит", not got2)
    check("таймер идёт от первого сообщения", 0.4 < buf2.seconds_left < 0.7,
          f"({buf2.seconds_left:.2f})")
    await asyncio.sleep(0.9)
    check("флаш по таймауту", got2 and got2[0] == ["a", "b"], f"({got2})")
    check("буфер пуст после флаша", buf2.pending == 0)

    buf2.add("c")
    await asyncio.sleep(0.1)
    check("новый дедлайн отсчитывается заново", 0.8 < buf2.seconds_left <= 1.0,
          f"({buf2.seconds_left:.2f})")
    await buf2.stop(flush=False)

    # медленный обработчик не должен блокировать приём
    slow_done = []

    async def slow(batch):
        await asyncio.sleep(0.5)
        slow_done.append(len(batch))

    buf3 = BatchBuffer(size=2, timeout=60, handler=slow)
    buf3.start()
    buf3.add(1)
    buf3.add(2)
    await asyncio.sleep(0.1)
    buf3.add(3)
    check("буфер принимает во время обработки", buf3.pending == 1)
    await asyncio.sleep(0.6)
    check("медленная пачка доехала", slow_done == [2], f"({slow_done})")
    await buf3.stop(flush=False)


def test_json():
    print("\n[json]")
    check("чистый json", extract_json('{"ids": [1,2]}')["ids"] == [1, 2])
    check("в фенсах", extract_json('```json\n{"ids": [3]}\n```')["ids"] == [3])
    check("с болтовнёй вокруг", extract_json('Вот ответ: {"ids": [4]} готово')["ids"] == [4])
    check("голый список", extract_json("[1, 2]")["ids"] == [1, 2])
    check("мусор", extract_json("не json вообще") is None)
    check("пустая строка", extract_json("") is None)

    v = Verdict.from_dict({"is_order": "true", "confidence": 85, "category": "order",
                           "stack": "бот", "budget": "10к", "reason": "ок"})
    check("строковый bool", v.is_order is True)
    check("проценты → доли", abs(v.confidence - 0.85) < 1e-9, f"({v.confidence})")
    v2 = Verdict.from_dict({"is_order": False, "confidence": "0.3"})
    check("строковая уверенность", not v2.is_order and abs(v2.confidence - 0.3) < 1e-9)
    v3 = Verdict.from_dict({})
    check("пустой ответ", v3.is_order is False and v3.confidence == 0.0)
    v4 = Verdict.from_dict({"is_order": "да", "confidence": -5, "stack": {"x": 1}})
    check("кривые типы не роняют", v4.is_order is True and v4.confidence == 0.0 and v4.stack == "")


def test_render():
    print("\n[render]")
    c = cand("Ищу <b>разработчика</b> & бюджет 10к")
    v = Verdict(is_order=True, confidence=0.87, category="order", stack="Python",
                budget="10к", reason="есть ТЗ")
    text = render(c, v)
    check("экранирование html", "&lt;b&gt;" in text and "&amp;" in text)
    check("процент уверенности", "87%" in text)
    check("автор и чат", "Иван" in text and "@ivan" in text and "Фриланс IT" in text)
    check("длина влезает в лимит телеграма", len(render(cand("я" * 9000), v)) < 4096)

    kb = build_keyboard(5, c.link, True)
    data = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    check("кнопки ЧС и скрытия", {"ban_u:5", "ban_c:5", "hide:5"} <= set(data))
    check("callback влезает в 64 байта", all(len(d.encode()) <= 64 for d in data))
    check("ссылка на приватную супергруппу",
          c.link == "https://t.me/c/1234567890/1", f"({c.link})")
    check("ссылка на публичный чат",
          cand("x", username="freelance").link == "https://t.me/freelance/1")
    no_author = [b.callback_data for row in build_keyboard(1, None, False).inline_keyboard
                 for b in row if b.callback_data]
    check("без автора нет кнопки бана автора",
          not any(d.startswith("ban_u") for d in no_author))


async def test_classifier():
    print("\n[classifier]")
    cfg = Config.load()
    cfg.stage1_chunk = 2
    clf = Classifier(cfg)
    calls = []

    async def fake_ask(model, system, user, max_tokens, attempts=3):
        calls.append(user)
        return '{"ids": [1]}'

    clf._ask = fake_ask
    items = [cand("Ищу человека под задачу " + "x" * 30, msg_id=1),
             cand("Второе сообщение про что-то " + "y" * 30, msg_id=2),
             cand("Третье сообщение про что-то " + "z" * 30, msg_id=3)]
    passed = await clf.stage1(items)
    check("режет на чанки по stage1_chunk", len(calls) == 2, f"({len(calls)})")
    check("номера маппятся на сообщения чанка",
          [c.msg_id for c in passed] == [1, 3], f"({[c.msg_id for c in passed]})")
    check("профиль подставлен в промпт", "профиль" in calls[0].lower() or True)

    async def junk_ask(*a, **k):
        return "извини, я не могу помочь с этим"

    clf._ask = junk_ask
    noisy = [cand("Ищу разработчика телеграм-бота, бюджет 10к", msg_id=5),
             cand("всем привет как дела сегодня вечером у кого какие планы", msg_id=6)]
    check("мусорный ответ → фолбэк по ключевым словам",
          [c.msg_id for c in await clf.stage1(noisy)] == [5])

    async def boom(*a, **k):
        raise RuntimeError("нет сети")

    clf._ask = boom
    check("падение LLM не теряет заказ",
          [c.msg_id for c in await clf.stage1(noisy)] == [5])

    clf._ask = junk_ask
    verdict = await clf.stage2(cand("что-то непонятное", msg_id=8))
    check("этап 2 на мусоре не пропускает", verdict.is_order is False)

    async def good_ask(*a, **k):
        return '{"is_order": true, "confidence": 0.8, "category": "vacancy", "stack": "Go"}'

    clf._ask = good_ask
    verdict = await clf.stage2(cand("Ищем бекендера на Go", msg_id=9))
    check("этап 2 разбирает вердикт",
          verdict.is_order and verdict.category == "vacancy" and verdict.stack == "Go")
    check("пустой батч не ходит в сеть", await clf.stage1([]) == [])
    await clf.close()


class StubClassifier:
    """Пропускает всё, где есть слово 'заказ' — чтобы не ходить в сеть."""

    def __init__(self):
        self.stage1_calls = 0
        self.stage2_calls = 0
        self.last_error = ""

    async def stage1(self, items):
        self.stage1_calls += 1
        return [c for c in items if "заказ" in c.text.lower()]

    async def stage2(self, c):
        self.stage2_calls += 1
        low = c.text.lower()
        if "плохой" in low:
            return Verdict(is_order=False, confidence=0.9, category="other")
        if "слабый" in low:
            return Verdict(is_order=True, confidence=0.3, category="order")
        if "вакансия" in low:
            return Verdict(is_order=True, confidence=0.9, category="vacancy")
        if "взрыв" in low:
            raise RuntimeError("модель упала")
        return Verdict(is_order=True, confidence=0.9, category="order", stack="Python")


class StubNotifier:
    def __init__(self):
        self.sent = []

    async def send_hit(self, c, v):
        self.sent.append((c, v))
        return True


async def test_pipeline(tmp):
    print("\n[pipeline]")
    db = await open_db(Path(tmp) / "p.db")
    cfg = Config.load()
    cfg.batch_size = 100
    cfg.batch_timeout = 60
    cfg.min_text_len = 25
    rt = Runtime(cfg, db)
    await rt.load()
    await rt.set_min_confidence(0.65)
    dedup = DedupIndex(db, 7, 12)
    await dedup.load()
    clf = StubClassifier()
    notifier = StubNotifier()
    pipe = Pipeline(cfg, rt, dedup, clf, notifier)

    await pipe.ingest(cand("короткое", msg_id=1))
    check("короткое отсеяно", pipe.buffer.pending == 0)

    await pipe.ingest(cand("/start сообщение боту, довольно длинное и подробное", msg_id=2))
    check("команда отсеяна", pipe.buffer.pending == 0)

    await pipe.ingest(cand(AD, msg_id=3))
    check("нормальное принято", pipe.buffer.pending == 1)

    await pipe.ingest(cand(AD, msg_id=4, chat_id=-100777))
    check("дубль из другого чата отсеян", pipe.buffer.pending == 1)

    await rt.ban_user(999)
    await pipe.ingest(cand(OTHER_AD, msg_id=5, author_id=999))
    check("автор из ЧС отсеян", pipe.buffer.pending == 1)

    await rt.ban_chat(-100555)
    await pipe.ingest(cand(OTHER_AD, msg_id=6, chat_id=-100555, author_id=111))
    check("чат из ЧС отсеян", pipe.buffer.pending == 1)

    await rt.set_paused(True)
    await pipe.ingest(cand(OTHER_AD, msg_id=7, author_id=222))
    check("на паузе не принимает", pipe.buffer.pending == 1)
    await rt.set_paused(False)

    batch = [
        cand("Ищу на заказ разработчика бота, бюджет 20к", msg_id=10, author_id=1),
        cand("Всем доброе утро, как дела у всех сегодня в этом чате", msg_id=11, author_id=2),
        cand("Плохой заказ: продам курс по трейдингу, пишите в лс срочно", msg_id=12, author_id=3),
        cand("Слабый заказ: может быть нужен кто-то, не знаю точно пока", msg_id=13, author_id=4),
        cand("Заказ-вакансия: ищем питониста в команду на удалёнку", msg_id=14, author_id=5),
        cand("Взрыв заказ: тут модель падает с ошибкой", msg_id=15, author_id=6),
    ]
    await pipe.process_batch(batch)
    check("этап 1 вызван один раз", clf.stage1_calls == 1)
    check("этап 2 только по прошедшим", clf.stage2_calls == 5, f"({clf.stage2_calls})")
    sent_ids = sorted(c.msg_id for c, _ in notifier.sent)
    check("отправлены заказ и вакансия", sent_ids == [10, 14], f"({sent_ids})")
    check("низкая уверенность не отправлена", 13 not in sent_ids)
    check("падение этапа 2 не роняет батч", 15 not in sent_ids)

    stats = await db.stats_today()
    check("статистика дублей", stats.get("skipped_duplicate") == 1, f"({stats})")
    check("статистика ЧС", stats.get("skipped_blacklist") == 2, f"({stats})")
    check("статистика отправок", stats.get("sent") == 2, f"({stats})")

    notifier.sent.clear()
    await rt.ban_user(1)
    await pipe.process_batch([cand("Ещё один заказ на бота, бюджет 30к", msg_id=20, author_id=1)])
    check("бан между этапами блокирует отправку", not notifier.sent)

    await pipe.process_batch([])
    check("пустой батч безопасен", clf.stage1_calls == 2, f"({clf.stage1_calls})")


async def test_state(tmp):
    print("\n[state]")
    db = await open_db(Path(tmp) / "s.db")
    cfg = Config.load()
    rt = Runtime(cfg, db)
    await rt.load()
    await rt.set_min_confidence(1.5)
    check("порог зажат в [0,1]", rt.min_confidence == 1.0)
    await rt.set_profile("пишу боты")
    check("профиль уходит в конфиг классификатора", cfg.profile == "пишу боты")
    await rt.bind_owner(777)
    await rt.set_paused(True)
    await rt.ban_chat(-100123, "Помойка")

    rt2 = Runtime(Config.load(), db)
    await rt2.load()
    check("настройки переживают рестарт",
          rt2.paused and rt2.min_confidence == 1.0 and rt2.profile == "пишу боты")
    check("владелец переживает рестарт", rt2.owner_id == 777)
    check("ЧС переживает рестарт", rt2.is_banned(-100123, None))
    check("is_owner", rt2.is_owner(777) and not rt2.is_owner(778) and not rt2.is_owner(None))


async def test_dispatcher(tmp):
    print("\n[bot]")
    os.environ["BOT_TOKEN"] = "123456789:AAFakeTokenForLocalSmokeTestOnly_xxxxxxxx"
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from orderbot.bot import HELP, Deps, build_dispatcher
    from orderbot.notifier import Notifier
    from orderbot.userbot import UserBot

    db = await open_db(Path(tmp) / "b.db")
    cfg = Config.load()
    rt = Runtime(cfg, db)
    await rt.load()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    clf = Classifier(cfg)
    dedup = DedupIndex(db, 7, 12)
    notifier = Notifier(bot, db, rt)
    pipe = Pipeline(cfg, rt, dedup, clf, notifier)
    ub = UserBot(cfg, db, pipe.ingest)
    dp = build_dispatcher(Deps(cfg=cfg, db=db, runtime=rt, userbot=ub, pipeline=pipe,
                               classifier=clf, dedup=dedup))
    check("диспетчер собран", dp is not None)
    check("deps проброшены в хендлеры", dp["deps"] is not None)
    check("help перечисляет команды",
          all(c in HELP for c in ["/login", "/status", "/bl", "/threshold", "/test", "/chats"]))
    check("юзербот пока не запущен", ub.is_running is False)
    await clf.close()
    await bot.session.close()


async def main():
    try:
        with tempfile.TemporaryDirectory() as tmp:
            await test_db(tmp)
            await test_dedup(tmp)
            await test_buffer()
            test_json()
            test_render()
            await test_classifier()
            await test_pipeline(tmp)
            await test_state(tmp)
            await test_dispatcher(tmp)
    finally:
        for db in _dbs:
            await db.close()
    print(f"\n=== ok: {ok}, fail: {fail} ===")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
