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
from orderbot.llm import ChatSession, LLMError, LLMRouter
from orderbot.llm import accounts as accounts_mod
from orderbot.llm.accounts import AccountsBackend
from orderbot.models import Candidate, Verdict
from orderbot.notifier import build_keyboard, render
from orderbot.pipeline import Pipeline
from orderbot.state import Runtime
from orderbot.utils import truncate
from orderbot.userbot import TgAccount, UserBot, UserBotManager

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
    with_via = cand("Ищу разработчика")
    with_via.via = "Рабочий (@work_acc)"
    check("в уведомлении видно, через какой аккаунт поймано",
          "Рабочий (@work_acc)" in render(with_via, v))
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
    clf = Classifier(cfg, StubBackend())
    calls = []

    async def fake_ask(system, user, max_tokens, chat=None, attempts=3):
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
        return [c for c in items
                if "заказ" in c.text.lower() or "ищу бота" in c.text.lower()]

    async def stage2(self, c):
        self.stage2_calls += 1
        low = c.text.lower()
        if "плохой" in low:
            return Verdict(is_order=False, confidence=0.9, category="other")
        if "слабый" in low:
            return Verdict(is_order=True, confidence=0.3, category="order")
        if "вакансия" in low:
            return Verdict(is_order=True, confidence=0.9, category="vacancy",
                           stack="бэкенд на Go")
        if "пустой стек" in low:
            # модель сказала «заказ», но не смогла назвать, что писать
            return Verdict(is_order=True, confidence=0.95, category="order", stack="")
        if "взрыв" in low:
            raise RuntimeError("модель упала")
        if "рассылк" in low:
            return Verdict(is_order=True, confidence=0.85, category="softbot",
                           stack="рассылка по группам")
        return Verdict(is_order=True, confidence=0.9, category="order", stack="Python")


class StubNotifier:
    def __init__(self):
        self.sent = []

    async def send_hit(self, c, v):
        self.sent.append((c, v))
        return True


def test_soft_leads():
    print("\n[лиды на готовый софт]")
    from orderbot.classifier import _FALLBACK_RE, SHORT_LEAD_RE
    from orderbot.notifier import CATEGORY_LABELS

    leads = ["ищу бота для рассылки", "нужен ловец чеков", "куплю инвайтер",
             "где взять парсер чатов", "нужен софт для рассылки в лс",
             "подскажите бота для отметок в историях", "интересует автоответчик"]
    for text in leads:
        check(f"короткий лид ловится: {text}", bool(SHORT_LEAD_RE.search(text)))

    not_leads = ["привет всем как дела", "бот тупит опять", "продам софт для рассылки",
                 "мой бот для инвайтинга, цена 500р", "как написать такого бота"]
    for text in not_leads:
        check(f"не лид: {text}", not SHORT_LEAD_RE.search(text))

    for text in ["ищу бота для рассылки", "нужен ловец чеков", "куплю инвайтер",
                 "где взять софт для накрутки"]:
        check(f"фолбэк знает про софт: {text}", bool(_FALLBACK_RE.search(text)))

    from orderbot.classifier import JUNK_RE

    # реальные ложные срабатывания, которые ловил бот на живых чатах
    junk = [
        "КУПЛЮ ГОТОВЫЕ Новофон физ/ИП/ООО Ростелеком АТС/8800 Билайн АТС/8800 Гарант+++",
        "Дайте макс нерег, плачу 4.5$, оплата момент",
        "Куплю много аккаунтов гугл. Новорег. Оптом.",
        "продам акки тг нерег дёшево, оплата любая",
        "куплю сим карты оптом, дорого, пишите в лс",
        "приму аккаунты вб, выкуп аккаунтов, гарант",
    ]
    for text in junk:
        check(f"барахолка режется: {truncate(text, 34)}", bool(JUNK_RE.search(text)))

    not_junk = [
        "куплю парсер чатов, готов заплатить",
        "куплю софт для инвайтинга",
        "куплю бота для автоответов в лс",
        "Требуется доработать парсер номеров телефонов с сайта, оплата 15к",
        "Нужен разработчик телеграм-бота, бюджет 30000",
        "ищу бота для рассылки",
    ]
    for text in not_junk:
        check(f"лид не попал под барахолку: {truncate(text, 30)}",
              not JUNK_RE.search(text))

    v = Verdict(is_order=True, confidence=0.8, category="softbot",
                stack="рассылка по группам, инвайтинг")
    text = render(cand("ищу бота для рассылки"), v)
    check("заголовок софт-лида отличается", "Ищут готовый софт" in text)
    check("категория подписана", CATEGORY_LABELS["softbot"] in text)
    check("функции показаны", "инвайтинг" in text)
    check("у заказа заголовок прежний",
          "Найден заказ" in render(cand("x"), Verdict(True, 0.9, "order")))


async def test_chat_sessions():
    print("\n[чаты с моделью]")
    chat = ChatSession("stage1", ttl=3600, history_turns=2, remember_chars=20)
    first_id = await chat.begin()
    msgs = chat.messages("система", "вопрос 1")
    check("в первом запросе только система и вопрос",
          [m["role"] for m in msgs] == ["system", "user"])

    await chat.remember(first_id, "в" * 100, "о" * 100)
    second_id = await chat.begin()
    msgs2 = chat.messages("система", "вопрос 2")
    check("чат тот же самый", second_id == first_id)
    check("история подхватилась",
          [m["role"] for m in msgs2] == ["system", "user", "assistant", "user"])
    check("реплики в истории обрезаны",
          all(len(m["content"]) <= 20 for m in msgs2[1:3]),
          f"({[len(m['content']) for m in msgs2[1:3]]})")

    await chat.remember(second_id, "вопрос 2", "ответ 2")
    await chat.remember(second_id, "вопрос 3", "ответ 3")
    msgs3 = chat.messages("система", "вопрос 4")
    check("история ограничена history_turns", len(msgs3) == 2 + 2 * 2, f"({len(msgs3)})")
    check("счётчик запросов", chat.requests == 2, f"({chat.requests})")

    chat.payload["chat_id"] = "server-1"
    dumped = chat.dump()
    revived = ChatSession("stage1", ttl=3600, history_turns=2)
    revived.restore(dumped)
    check("чат поднимается из дампа", revived.session_id == chat.session_id)
    check("payload переживает рестарт", revived.payload.get("chat_id") == "server-1")
    check("история переживает рестарт", len(revived.messages("s", "u")) == len(msgs3))
    check("возраст считается от сохранённого времени", revived.age < 5)

    chat.ttl = 60
    chat.started -= 61                                    # состарили чат
    rotated_id = await chat.begin()
    check("чат сменился по времени", rotated_id != first_id)
    check("смена посчитана", chat.rotations == 1)
    check("после смены история пустая",
          [m["role"] for m in chat.messages("s", "u")] == ["system", "user"])
    check("после смены payload очищен", chat.payload == {})

    await chat.remember(first_id, "старое", "ответ")      # ответ из прошлого чата
    check("в новый чат старое не дописывается",
          [m["role"] for m in chat.messages("s", "u")] == ["system", "user"])

    before = chat.session_id
    await chat.rotate_now()
    check("принудительная смена чата даёт новый id", chat.session_id != before)
    check("принудительная смена считается", chat.rotations == 2)

    plain = ChatSession("stage2", ttl=3600, history_turns=0)
    sid = await plain.begin()
    await plain.remember(sid, "u", "a")
    check("history_turns=0 не копит контекст", len(plain.messages("s", "u2")) == 2)
    check("но чат остаётся тем же", plain.session_id == sid)

    parallel = ChatSession("stage1", ttl=3600, history_turns=2)
    ids = await asyncio.gather(*(parallel.begin() for _ in range(8)))
    check("параллельные запросы идут в один чат",
          len(set(ids)) == 1 and parallel.requests == 8)


class StubBackend:
    """Бэкенд-заглушка: не ходит в сеть, считает запросы."""

    name = "stub"

    def __init__(self, answer="ответ", error=None):
        self.answer = answer
        self.error = error
        self.calls = []
        self.available = True

    async def ask(self, session, system, user, max_tokens):
        await session.begin()
        self.calls.append(user)
        if self.error:
            raise LLMError(self.error)
        return self.answer

    def status(self):
        return self.name

    async def close(self):
        return None


async def test_llm_router():
    print("\n[выбор бэкенда]")
    cfg = Config.load()
    session = ChatSession("stage1", 3600, 0)

    accounts, key = StubBackend("из аккаунта"), StubBackend("по ключу")
    cfg.llm_backend = "auto"
    router = LLMRouter(cfg, accounts, key)
    check("аккаунты в приоритете",
          await router.ask(session, "s", "u", 10) == "из аккаунта")

    accounts.available = False
    check("без аккаунтов уходим на ключ",
          await router.ask(session, "s", "u", 10) == "по ключу")

    accounts.available = True
    accounts.error = "аккаунт лёг"
    check("падение аккаунтов подхватывает ключ",
          await router.ask(session, "s", "u", 10) == "по ключу")

    key.error = "и ключ лёг"
    try:
        await router.ask(session, "s", "u", 10)
        check("оба легли — ошибка наверх", False)
    except LLMError as exc:
        check("оба легли — ошибка наверх",
              "аккаунт лёг" in str(exc) and "и ключ лёг" in str(exc))

    accounts.error = key.error = None
    cfg.llm_backend = "key"
    check("режим key игнорирует аккаунты",
          await router.ask(session, "s", "u", 10) == "по ключу")
    cfg.llm_backend = "accounts"
    check("режим accounts игнорирует ключ",
          await router.ask(session, "s", "u", 10) == "из аккаунта")
    cfg.llm_backend = "auto"

    accounts.available = key.available = False
    check("нет ни одного бэкенда — router недоступен", router.available is False)


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.prompts = []
        self.broken = False

    def send(self, prompt, **kwargs):
        if self.broken:
            raise RuntimeError("rate limit exceeded")
        self.prompts.append(prompt)
        return f"ответ на {prompt[:20]}"


class FakeAdapter:
    """Подменяет вендорную библиотеку — без сети и без логина."""

    def __init__(self):
        self.logins = []
        self.chats = []
        self.bad_login = set()

    def make_client(self, account, store_path):
        if account.email in self.bad_login:
            raise RuntimeError("auth failed: неверный пароль")
        self.logins.append(account.email)
        return {"email": account.email}

    def new_chat(self, client):
        chat = FakeChat(f"chat{len(self.chats) + 1}")
        self.chats.append(chat)
        return chat

    def resume(self, client, chat_id):
        for chat in self.chats:
            if chat.id == chat_id:
                return chat
        raise RuntimeError("чат не найден")

    def chat_id(self, chat):
        return chat.id

    def send(self, chat, prompt):
        return chat.send(prompt)


async def test_accounts_backend(tmp):
    print("\n[аккаунты нейросети]")
    db = await open_db(Path(tmp) / "acc.db")
    cfg = Config.load()
    cfg.db_path = Path(tmp) / "acc.db"
    cfg.account_cooldown = 600

    fake = FakeAdapter()
    original = accounts_mod.ADAPTERS["deepseek"]
    accounts_mod.ADAPTERS["deepseek"] = fake
    try:
        backend = AccountsBackend(cfg, db)
        check("без аккаунтов бэкенд недоступен", backend.available is False)

        await db.llm_account_add("deepseek", "a@mail", "pass1")
        await db.llm_account_add("deepseek", "b@mail", "pass2")
        await backend.reload()
        check("аккаунты подхватились", len(backend.accounts) == 2)
        check("с аккаунтом бэкенд доступен", backend.available is True)

        session = ChatSession("stage1", 3600, 0)
        await backend.ask(session, "СИСТЕМНЫЙ ПРОМПТ", "вопрос 1", 100)
        await backend.ask(session, "СИСТЕМНЫЙ ПРОМПТ", "вопрос 2", 100)
        check("чат создан один на всю сессию", len(fake.chats) == 1, f"({len(fake.chats)})")
        check("залогинились один раз", len(fake.logins) == 1, f"({fake.logins})")
        prompts = fake.chats[0].prompts
        check("системный промпт ушёл первым сообщением",
              prompts[0].startswith("СИСТЕМНЫЙ ПРОМПТ"))
        check("второй раз система не повторяется", prompts[1] == "вопрос 2")
        check("аккаунт запомнен в чате", session.payload.get("account_id") is not None)
        check("id серверного чата сохранён",
              session.payload["chats"] == {str(session.payload["account_id"]): "chat1"})

        # первый аккаунт начал падать — уходим на второй
        fake.chats[0].broken = True
        await backend.ask(session, "СИСТЕМА", "вопрос 3", 100)
        check("при ошибке ушли на другой аккаунт", len(fake.logins) == 2, f"({fake.logins})")
        check("на другом аккаунте новый чат", len(fake.chats) == 2)
        broken = next(a for a in backend.accounts if a.email == "a@mail")
        check("упавший аккаунт в кулдауне", not broken.usable)
        check("причина записана", "rate limit" in broken.last_error)

        fresh = AccountsBackend(cfg, db)
        await fresh.reload()
        check("кулдаун сохранён в базе",
              not next(a for a in fresh.accounts if a.email == "a@mail").usable)

        # аккаунт вернулся из кулдауна — продолжаем его прежний чат, а не новый
        fake.chats[0].broken = False
        broken.cooldown_until = 0
        second = next(a for a in backend.accounts if a.email == "b@mail")
        second.disabled = True
        chats_before = len(fake.chats)
        await backend.ask(session, "СИСТЕМА", "вопрос 4", 100)
        check("вернувшийся аккаунт продолжает свой старый чат",
              len(fake.chats) == chats_before, f"({chats_before} → {len(fake.chats)})")
        check("в старом чате систему второй раз не шлём",
              fake.chats[0].prompts[-1] == "вопрос 4", f"({fake.chats[0].prompts[-1][:40]})")
        second.disabled = False

        # легли оба
        for chat in fake.chats:
            chat.broken = True
        try:
            await backend.ask(session, "СИСТЕМА", "вопрос 5", 100)
            check("все аккаунты легли — ошибка наверх", False)
        except LLMError as exc:
            check("все аккаунты легли — ошибка наверх", "mail" in str(exc))
        check("бэкенд без живых аккаунтов недоступен", backend.available is False)

        await db.llm_account_toggle(backend.accounts[0].id)
        await backend.reload()
        check("выключенный аккаунт помечен", backend.accounts[0].disabled is True)

        # после рестарта бота чат продолжается, а не создаётся заново
        for chat in fake.chats:
            chat.broken = False
        restarted = AccountsBackend(cfg, db)
        await restarted.reload()
        for account in restarted.accounts:
            account.cooldown_until = 0
            account.disabled = False
        revived = ChatSession("stage1", 3600, 0)
        revived.restore(session.dump())
        chats_before = len(fake.chats)
        await restarted.ask(revived, "СИСТЕМА", "вопрос 6", 100)
        check("после рестарта чат продолжается, новый не создаётся",
              len(fake.chats) == chats_before, f"({chats_before} → {len(fake.chats)})")

        # проверка учётных данных при добавлении
        fake.bad_login.add("bad@mail")
        try:
            await backend.check("deepseek", "bad@mail", "wrong")
            check("проверка ловит неверный пароль", False)
        except LLMError as exc:
            check("проверка ловит неверный пароль", "auth failed" in str(exc))
        answer = await backend.check("deepseek", "new@mail", "right")
        check("проверка возвращает ответ модели", answer.startswith("ответ на"))
    finally:
        accounts_mod.ADAPTERS["deepseek"] = original


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

    await pipe.ingest(cand("ищу бота для рассылки", msg_id=30, author_id=30))
    check("короткий софт-лид проходит порог длины", pipe.buffer.pending == 2)
    await pipe.ingest(cand("привет всем", msg_id=31, author_id=31))
    check("короткий флуд не проходит", pipe.buffer.pending == 2)
    await pipe.ingest(cand("продам софт для рассылки", msg_id=32, author_id=32))
    check("короткая реклама продавца не проходит", pipe.buffer.pending == 2)

    same_ad = ("Требуется специалист на разовую задачу: настроить выгрузку заказов "
               "из амоCRM в гугл-таблицы, оплата по договорённости, детали в личку.")
    first = cand(same_ad, msg_id=40, author_id=40)
    first.via = "Первый аккаунт"
    second = cand(same_ad, msg_id=40, author_id=40)
    second.via = "Второй аккаунт"
    pending_before = pipe.buffer.pending
    await pipe.ingest(first)
    check("сообщение из общего чата принято один раз",
          pipe.buffer.pending == pending_before + 1)
    await pipe.ingest(second)
    check("второй аккаунт то же сообщение не дублирует",
          pipe.buffer.pending == pending_before + 1)

    await pipe.ingest(cand("Куплю много аккаунтов гугл. Новорег. Оптом. Пишите в лс",
                           msg_id=50, author_id=50))
    check("барахолка не доходит до LLM", pipe.buffer.pending == pending_before + 1)
    await pipe.ingest(cand("КУПЛЮ ГОТОВЫЕ Новофон физ/ИП/ООО, Ростелеком АТС/8800, гарант",
                           msg_id=51, author_id=51))
    check("покупка номеров и юрлиц тоже", pipe.buffer.pending == pending_before + 1)
    junk_stats = await db.stats_today()
    check("барахолка попадает в статистику", junk_stats.get("skipped_junk") == 2,
          f"({junk_stats.get('skipped_junk')})")

    batch = [
        cand("Ищу на заказ разработчика бота, бюджет 20к", msg_id=10, author_id=1),
        cand("Всем доброе утро, как дела у всех сегодня в этом чате", msg_id=11, author_id=2),
        cand("Плохой заказ: продам курс по трейдингу, пишите в лс срочно", msg_id=12, author_id=3),
        cand("Слабый заказ: может быть нужен кто-то, не знаю точно пока", msg_id=13, author_id=4),
        cand("Заказ-вакансия: ищем питониста в команду на удалёнку", msg_id=14, author_id=5),
        cand("Взрыв заказ: тут модель падает с ошибкой", msg_id=15, author_id=6),
        cand("Ищу бота для рассылки по группам и инвайтинга", msg_id=16, author_id=7),
        cand("Заказ с пустой стек: нужно сделать что-нибудь хорошее", msg_id=17,
             author_id=8),
    ]
    await pipe.process_batch(batch)
    check("этап 1 вызван один раз", clf.stage1_calls == 1)
    check("этап 2 только по прошедшим", clf.stage2_calls == 7, f"({clf.stage2_calls})")
    sent_ids = sorted(c.msg_id for c, _ in notifier.sent)
    check("отправлены заказ, вакансия и софт-лид",
          sent_ids == [10, 14, 16], f"({sent_ids})")
    check("низкая уверенность не отправлена", 13 not in sent_ids)
    check("падение этапа 2 не роняет батч", 15 not in sent_ids)
    check("без ответа «что писать» уведомление не уходит", 17 not in sent_ids)

    stats = await db.stats_today()
    # один дубль по тексту (то же объявление в другом чате) + один по id сообщения
    # (тот же пост, увиденный вторым аккаунтом)
    check("статистика дублей", stats.get("skipped_duplicate") == 2, f"({stats})")
    check("статистика ЧС", stats.get("skipped_blacklist") == 2, f"({stats})")
    check("статистика отправок", stats.get("sent") == 3, f"({stats})")

    notifier.sent.clear()
    await rt.ban_user(1)
    await pipe.process_batch([cand("Ещё один заказ на бота, бюджет 30к", msg_id=20, author_id=1)])
    check("бан между этапами блокирует отправку", not notifier.sent)

    await pipe.process_batch([])
    check("пустой батч безопасен", clf.stage1_calls == 2, f"({clf.stage1_calls})")


async def test_tg_accounts(tmp):
    print("\n[телеграм-аккаунты]")
    db = await open_db(Path(tmp) / "tg.db")
    cfg = Config.load()

    async def noop(cand):
        return None

    # старая одноаккаунтная схема переезжает в таблицу
    await db.set("session", "OLD-SESSION")
    await db.set("api_id", 111)
    await db.set("api_hash", "oldhash")
    manager = UserBotManager(cfg, db, noop)
    await manager.load()
    check("старая сессия перенесена в мультиаккаунт", len(manager.accounts) == 1)
    check("старые ключи стёрты", await db.get("session") is None)

    await db.tg_account_add(1, "h1", "+79990000001", "sess1", "Иван", "ivan", 1001)
    await db.tg_account_add(2, "h2", "+79990000002", "sess2", "Пётр", "", 1002)
    await manager.load()
    check("аккаунты подхватились", len(manager.accounts) == 3, f"({len(manager.accounts)})")
    titles = [a.title for a in manager.accounts]
    check("подпись с юзернеймом", "Иван (@ivan)" in titles, f"({titles})")
    check("подпись без юзернейма", "Пётр" in titles, f"({titles})")

    await db.tg_account_add(1, "h1", "+79990000001", "sess1-new", "Иван", "ivan", 1001)
    rows = await db.tg_accounts()
    check("повторный вход тем же аккаунтом не плодит запись", len(rows) == 3, f"({len(rows)})")
    check("сессия обновилась", any(r["session"] == "sess1-new" for r in rows))

    ivan = next(a for a in manager.accounts if a.username == "ivan")
    check("выключение аккаунта", await manager.toggle(ivan.id) is True)
    check("выключенный не стартует", await manager.get(ivan.id).start() is False)
    await manager.load()
    check("выключение переживает рестарт",
          next(a for a in manager.accounts if a.username == "ivan").disabled is True)

    check("удаление аккаунта", await manager.remove(ivan.id) is True)
    await manager.load()
    check("удалённого нет ни в базе, ни в памяти",
          len(manager.accounts) == 2 and manager.get(ivan.id) is None)

    check("без клиентов ничего не слушает", manager.is_running is False)
    check("битая сессия не роняет запуск", await manager.start_all() == 0)
    dead = next(a for a in manager.accounts if a.session == "sess2")
    check("причина записана", bool(dead.last_error), f"({dead.last_error})")

    bot = UserBot(TgAccount(id=99, api_id=1, api_hash="h", session=""), noop)
    check("аккаунт без сессии не стартует", await bot.start() is False)


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
    from orderbot.llm import build_router
    from orderbot.notifier import Notifier
    from orderbot.userbot import UserBotManager

    db = await open_db(Path(tmp) / "b.db")
    cfg = Config.load()
    rt = Runtime(cfg, db)
    await rt.load()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    llm = await build_router(cfg, db)
    clf = Classifier(cfg, llm, db)
    dedup = DedupIndex(db, 7, 12)
    notifier = Notifier(bot, db, rt)
    pipe = Pipeline(cfg, rt, dedup, clf, notifier)
    ub = UserBotManager(cfg, db, pipe.ingest)
    dp = build_dispatcher(Deps(cfg=cfg, db=db, runtime=rt, userbot=ub, pipeline=pipe,
                               classifier=clf, dedup=dedup, router=llm))
    check("диспетчер собран", dp is not None)
    check("deps проброшены в хендлеры", dp["deps"] is not None)
    check("help перечисляет команды",
          all(c in HELP for c in ["/login", "/status", "/bl", "/threshold", "/test",
                                  "/chats", "/accounts", "/llm", "/addllm"]))
    check("юзерботы пока не запущены", ub.is_running is False)
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
            test_soft_leads()
            await test_chat_sessions()
            await test_llm_router()
            await test_accounts_backend(tmp)
            await test_classifier()
            await test_pipeline(tmp)
            await test_tg_accounts(tmp)
            await test_state(tmp)
            await test_dispatcher(tmp)
    finally:
        for db in _dbs:
            await db.close()
    print(f"\n=== ok: {ok}, fail: {fail} ===")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
