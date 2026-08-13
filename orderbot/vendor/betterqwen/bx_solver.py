"""Генератор анти-бот заголовков chat.qwen.ai: bx-ua, bx-umidtoken, bx-v.

Защита chat.qwen.ai — фирменная система Alibaba FireEye/baxia (те же скрипты,
что на Taobao/AliExpress). Заголовки:

  bx-ua          — динамический токен (формат "231!..."), НОВЫЙ на каждый запрос,
                   вплетает fingerprint браузера + reqUrl + timestamp/nonce.
  bx-umidtoken   — стабильный device-id (формат "T2gA...="), один и тот же на все
                   запросы устройства.
  bx-v           — константа версии baxia ("2.5.36").

Требуются на защищённых эндпоинтах (chat/completions, chats/new, chats/list…).
Если их не слать — сервер сначала пропускает, потом эскалирует до slider-капчи
(накопительный risk-score, не одноразовый гейт).

КАК РЕШЕНО (без браузера, чистый Python + py_mini_racer/V8, как WAF у DeepSeek):
реальный `fireyejs.js` (скачан с g.alicdn.com, лежит в vendor/) исполняется в V8
поверх браузерного шима (vendor/bx_shim.js). Тот же вызов, что делает сама
страница — `window.__fyModule.getFYToken({reqUrl})` — возвращает готовый bx-ua.
Шим замокан ровно настолько, чтобы FireEye прошёл свой fingerprint (canvas/WebGL/
navigator/screen/matchMedia/canPlayType + анти-tamper захват нативных аксессоров).
Подробности и история грабель — CLAUDE.md.

Кроме bx-ua, есть ВТОРОЙ независимый анти-бот слой: risk-control cookie **isg**
(и родственные) — ставится не сервером через Set-Cookie, а самим `sufei_data.js`
локально через `document.cookie` (подтверждено — воспроизводится в V8 без сети).
`chat/completions` (в отличие от лёгких chats/new) возвращал generic "Internal
error..." без валидной isg — похоже, эту cookie тоже проверяют. Решается тем же
приёмом: `sufei_data.js` тоже гонится в этом V8-контексте, `document.cookie`
шима теперь настоящий jar (накопление по имени), `get_cookies()` отдаёт
получившиеся risk-cookies наружу — client.py кладёт их в requests.Session.

bx-umidtoken: компонент umid FireEye считает асинхронно, в чистом V8 это не
воспроизвелось. Он device-стабилен и долгоживущий, поэтому синтезируется и
персистится (generate_umid) — полностью автономно, ничего из DevTools брать
не надо. Нужен ли серверу валидный (не синтезированный) umid — предстоит
проверить на живом (тестовом) аккаунте, если проблема с isg не решит всё сама.
"""

import base64
import os
import secrets
import threading

from py_mini_racer import py_mini_racer

BX_V = "2.5.36"


def generate_umid():
    """Синтез device-стабильного bx-umidtoken формата "T2gA...=".

    FireEye считает настоящий umid в подсистеме et/LTK при раннем old-load через
    полную AWSC-оркестрацию (awsc.js-loader + сетевая подгрузка модулей) — в
    чистом V8 этот путь не воспроизвёлся. umid — это device-continuity сигнал
    (в трафике одинаков на все запросы устройства), а не криптоподпись запроса
    (это делает bx-ua). Поэтому генерируем формат-валидный токен ОДИН раз и
    переиспользуем (персистится в Store) — так с точки зрения сервера мы =
    стабильное устройство.

    ВНИМАНИЕ: примет ли сервер синтезированный umid или требует «настоящий» —
    проверяется на живом (тестовом) аккаунте. Если отклонит — надо доводить
    et/LTK-путь в V8 (см. CLAUDE.md). bx-ua это не затрагивает — он валиден сам
    по себе.
    """
    body = base64.urlsafe_b64encode(secrets.token_bytes(45)).decode().rstrip("=")
    return "T2gA" + body[:60] + "="

_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")

# Параметры UBInit — ровно те, что вызывает baxia-entry на реальной странице.
_UBINIT = ("window.__fyModule.UBInit({AsynSwitch:true,SyncSwitch:true,interval:600,"
           "TraceInterval:10,TraceMax:300,validTime:3600})")


def _read(name):
    with open(os.path.join(_VENDOR, name), encoding="utf-8") as f:
        return f.read()


class BxSolver:
    """Держит один инициализированный V8-контекст с загруженным FireEye.

    Инициализация ~50мс, генерация bx-ua ~35-90мс. Контекст py_mini_racer не
    потокобезопасен — все вызовы под общим Lock, так что один BxSolver можно
    спокойно шарить между потоками (как это делает Qwen-клиент).
    """

    def __init__(self, umid=""):
        self._lock = threading.Lock()
        self._umid = umid or ""
        self._ctx = py_mini_racer.MiniRacer()
        self._ctx.eval(_read("bx_shim.js"))
        self._ctx.eval(_read("sufei_data.js"))  # ставит risk-cookie isg через document.cookie
        self._ctx.eval(_read("fireyejs.js"))
        if self._ctx.eval("typeof window.__fyModule") != "object":
            raise RuntimeError("bx_solver: fireyejs не создал window.__fyModule "
                               "(изменился challenge/шим?) — см. CLAUDE.md")
        self._ctx.eval(_UBINIT)
        # load/DOMContentLoaded — часть anti-бот логики (sufei_data) ждёт их
        self._ctx.eval("window.__fireLoadEvents()")

    def get_cookies(self):
        """Risk-control cookies (isg и родственные), которые анти-бот SDK
        поставил через document.cookie при инициализации. dict {name: value}."""
        import json
        with self._lock:
            raw = self._ctx.eval("window.__getCookieJarJSON()")
        return json.loads(raw) if raw else {}

    def get_bx_ua(self, req_url):
        """Свежий bx-ua для конкретного URL запроса (полный, с https://…)."""
        with self._lock:
            # JSON-экранируем URL, чтобы не сломать JS-строку
            js = "window.__fyModule.getFYToken({reqUrl:" + _js_str(req_url) + "})"
            token = self._ctx.eval(js)
        if not token:
            raise RuntimeError("bx_solver: getFYToken вернул пусто — вероятно, AWS/"
                               "Alibaba обновили fireyejs.js, шим больше не проходит "
                               "fingerprint (см. CLAUDE.md, отладка через error-beacon)")
        return token

    def get_umid(self):
        """bx-umidtoken (device-стабильный). Сначала пробуем сам FireEye (обычно
        пусто в чистом V8 — см. generate_umid), иначе — переданный/сгенерированный
        при создании стабильный umid."""
        with self._lock:
            token = self._ctx.eval("window.__fyModule.getUidToken()") or ""
        return token or self._umid

    def headers(self, req_url):
        """Готовый набор bx-* заголовков для запроса."""
        h = {"bx-ua": self.get_bx_ua(req_url), "bx-v": BX_V}
        umid = self.get_umid()
        if umid:
            h["bx-umidtoken"] = umid
        return h


def _js_str(s):
    """Безопасный JS-строковый литерал из Python-строки."""
    import json
    return json.dumps(str(s))
