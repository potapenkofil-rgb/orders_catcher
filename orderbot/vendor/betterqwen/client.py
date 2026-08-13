"""Qwen — главный клиент библиотеки (chat.qwen.ai)."""

import json
import os
import re
import secrets
import sys
import time
import threading
import uuid

import requests

from . import auth
from .bx_solver import BxSolver, BX_V, generate_umid
from .chat import ChatManager
from .exceptions import ApiError, AuthError, FileUploadError, QwenError, WafCaptchaError
from .files import normalize_input
from .ping import PingResult
from .store import MemoryStore

_FILE_DEBUG = bool(os.environ.get("QWEN_DEBUG"))


def _fdbg(label, obj):
    if _FILE_DEBUG:
        sys.stderr.write(f"[qwen-debug] {label}: {json.dumps(obj, ensure_ascii=False)[:1500]}\n")


BASE = "https://chat.qwen.ai/api/v2"
ORIGIN = "https://chat.qwen.ai"

# Версия фронта Qwen (заголовок Version). Фронтовый axios-интерсептор ставит
# его на КАЖДЫЙ запрос; без него completions отвечает "Internal error"
# (chats/new терпит и без него, а completions — нет). Совпадает с версией
# бандла qwen-chat-fe (см. assets.alicdn.com/g/qwenweb/qwen-chat-fe/<VER>/).
# Хардкод здесь — только fallback: живьём эта версия обновляется на CDN за
# один день несколько раз подряд (замечено: .74 → .75 → .80), поэтому
# _fetch_fe_version() ниже вытаскивает актуальную версию прямо из HTML
# chat.qwen.ai/ при каждом создании клиента (как это делает браузер, просто
# загружая страницу) — эта константа используется, только если запрос не удался.
FE_VERSION = "0.2.80"

_FE_VERSION_RE = re.compile(r"qwen-chat-fe/([\d.]+)/js/main\.js")


def _fetch_fe_version(session, fallback=FE_VERSION):
    try:
        r = session.get(ORIGIN + "/", timeout=10)
        m = _FE_VERSION_RE.search(r.text)
        if m:
            return m.group(1)
    except requests.exceptions.RequestException:
        pass
    return fallback


# Дефолт-модель, если не удалось получить список с сервера.
DEFAULT_MODEL = "qwen3.7-plus"


_SEC_CH_UA = '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'


def _do_signup(name, email, password, umid=None, solve_captcha=True):
    """POST /api/v1/auths/signup с bx-заголовками.

    Если WAF возвращает капчу и solve_captcha=True — решает локально через V8
    (waf_solver) и повторяет запрос. Без solve_captcha пробрасывает WafCaptchaError.

    Возвращает (signup_response_dict, requests.Session, umid_str, BxSolver).
    """
    session = requests.Session()
    umid = umid or generate_umid()
    bx = BxSolver(umid=umid)

    # 1. bx-cookies (isg и родственные) — ДО любых запросов
    for ck, cv in bx.get_cookies().items():
        session.cookies.set(ck, cv, domain=".qwen.ai", path="/")

    # Базовые browser-realistic заголовки для всей сессии
    session.headers.update({
        "user-agent": auth.UA,
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })

    # 2. Главная страница — fe_version + server cookies
    fe_version = _fetch_fe_version(session)

    # 3. Страница /auth — WAF видит навигацию перед API-вызовом
    try:
        session.get(ORIGIN + "/auth", timeout=10, headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "upgrade-insecure-requests": "1",
        })
    except Exception:
        pass

    # 4. Signup API
    signup_url = f"{auth.BASE_V1}/auths/signup"
    signup_headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": ORIGIN,
        "referer": ORIGIN + "/auth",
        "source": "web",
        "version": fe_version,
        "x-request-id": str(uuid.uuid4()),
        "timezone": time.strftime("%a %b %d %Y %H:%M:%S GMT%z"),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    signup_headers.update(bx.headers(signup_url))

    try:
        data = auth.register(session, name, email, password, headers=signup_headers)
    except WafCaptchaError as waf:
        if not solve_captcha:
            raise
        from . import waf_solver as _waf
        request_info, u_asig = _waf.solve_waf_captcha(waf.html_body, session=session)
        _fdbg("waf_solved", {"u_asig": u_asig})
        payload = {"name": name, "email": email, "password": auth.hash_password(password)}
        r = _waf.retry_after_captcha(
            session, signup_url, payload, request_info, u_asig, extra_headers=signup_headers
        )
        auth._check_waf_captcha(r)
        try:
            data = r.json()
        except ValueError:
            raise AuthError(f"signup (после капчи) не-JSON (HTTP {r.status_code}): {r.text[:300]}")
        if r.status_code >= 400:
            raise AuthError(f"signup (после капчи) HTTP {r.status_code}: {data or r.text[:300]}")
        if isinstance(data, dict) and data.get("success") is False:
            err = data.get("data", {})
            raise AuthError("signup отклонён: " + str(err.get("details") or err.get("code") or data))

    return data, session, umid, bx


class Qwen:
    """Клиент chat.qwen.ai.

    Аутентификация — cookie-сессия (email/password → Set-Cookie). Анти-бот
    заголовки bx-ua/bx-umidtoken/bx-v считает BxSolver (см. bx_solver.py) —
    без браузера, чистый Python.

    umid — device-стабильный bx-umidtoken. Полностью автономно: если не передан,
    генерируется сам (generate_umid) и сохраняется в Store, чтобы оставаться
    стабильным между запусками (одно "устройство"). Ничего вручную из DevTools
    брать НЕ надо. bx-ua (динамический, главный анти-бот токен) считается в V8.
    """

    def __init__(self, email=None, password=None, umid="", store=None,
                 default_model=DEFAULT_MODEL, default_thinking=True, default_search=True):
        self.email = email
        self.password = password
        self.default_model = default_model
        self.default_thinking = default_thinking
        self.default_search = default_search

        self.store = store or MemoryStore()
        self._auth_lock = threading.Lock()

        # umid: явный аргумент > сохранённый в Store > свежесгенерированный.
        # Сгенерированный сохраняем — device-стабильность между запусками.
        self.umid = umid or self.store.get("umid", "")
        if not self.umid:
            self.umid = generate_umid()
            self.store.set("umid", self.umid)
        self._bx = BxSolver(umid=self.umid)

        self.session = requests.Session()
        self.fe_version = _fetch_fe_version(self.session)
        self.session.headers.update({
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": ORIGIN,
            "referer": ORIGIN + "/",
            "source": "web",
            "version": self.fe_version,
            "user-agent": auth.UA,
            "bx-v": BX_V,
        })

        # risk-control cookies (isg и родственные) — ставит анти-бот SDK через
        # document.cookie при инициализации BxSolver, не сервер. Кладём их в
        # сессию ДО восстановления сохранённых/реальных cookie — если сервер
        # когда-нибудь пришлёт свой Set-Cookie с тем же именем, он перезапишет.
        for name, value in self._bx.get_cookies().items():
            self.session.cookies.set(name, value, domain=".qwen.ai", path="/")

        # восстановить cookie-сессию + Bearer из store, иначе — логин
        saved = self.store.get("cookies")
        tok = self.store.get("token")
        if saved and tok:
            self.session.cookies.update(saved)
            self.session.headers["authorization"] = f"Bearer {tok}"
        elif email and password:
            # нет сохранённого Bearer (или первый запуск) — логинимся, чтобы его получить
            if saved:
                self.session.cookies.update(saved)
            self._login()
        else:
            raise AuthError("Нужно передать email=...+password=..., либо store с "
                            "сохранённой cookie-сессией.")

        self.chats = ChatManager(self)

    # ── авторизация ──────────────────────────────────────────────────────

    def _login(self):
        if not (self.email and self.password):
            raise AuthError("Сессия недействительна, а email/password не заданы — обновить некому.")
        with self._auth_lock:
            data = auth.login(self.session, self.email, self.password)
            _fdbg("signin response", data)
            token = auth.extract_token(data)
            if token:
                self.session.headers["authorization"] = f"Bearer {token}"
                self.store.set("token", token)
            self.store.set("cookies", self.session.cookies.get_dict())

    def relogin(self):
        """Принудительно перелогиниться (например, если сессия протухла)."""
        self._login()

    @classmethod
    def register(cls, name, email, password, store=None, umid="", **kwargs):
        """Зарегистрировать новый аккаунт и вернуть Qwen-клиент.

        WAF slider-капча решается автоматически (V8 + PIL, без внешних сервисов).
        Qwen требует email-верификации. С реальным email — пройди ссылку из
        письма вручную, затем Qwen(email=..., password=...).
        Для полного автомата — register_with_tempmail().
        """
        data, session, umid, bx = _do_signup(
            name, email, password, umid=umid or None
        )
        _fdbg("signup response", data)
        token = auth.extract_token(data)
        if not token:
            raise AuthError(
                f"Регистрация выполнена, но требует подтверждения email ({email}). "
                "Перейди по ссылке из письма, затем: Qwen(email=..., password=...). "
                "Для автономной регистрации — register_with_tempmail()."
            )
        return cls(email=email, password=password, store=store, umid=umid, **kwargs)

    # ── низкоуровневые запросы ───────────────────────────────────────────

    def _headers_for(self, url, with_bx=True):
        """Заголовки под конкретный запрос: свежий x-request-id + bx-* (bx-ua
        считается от полного URL запроса — так делает и сама страница)."""
        h = {
            "x-request-id": str(uuid.uuid4()),
            "timezone": time.strftime("%a %b %d %Y %H:%M:%S GMT%z"),
        }
        if with_bx:
            h.update(self._bx.headers(url))
        return h

    def _request(self, method, path, with_bx=True, retry_on_auth=True, **kwargs):
        url = f"{BASE}{path}"
        timeout = kwargs.pop("timeout", 30)
        headers = kwargs.pop("headers", {})
        headers = {**self._headers_for(url, with_bx=with_bx), **headers}
        r = self.session.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if r.status_code in (401, 403) and retry_on_auth and self.email and self.password:
            self._login()
            return self._request(method, path, with_bx=with_bx, retry_on_auth=False,
                                 timeout=timeout, headers=kwargs.pop("headers", {}), **kwargs)
        try:
            _detect_captcha(r)
        except WafCaptchaError as waf:
            _fdbg("waf_captcha", f"{method} {path}")
            from . import waf_solver as _waf
            request_info, u_asig = _waf.solve_waf_captcha(waf.html_body, session=self.session)
            _fdbg("waf_solved", {"u_asig": u_asig})
            r = _waf.retry_after_captcha(
                self.session, url, kwargs.get("json"), request_info, u_asig,
                extra_headers=headers,
            )
            _detect_captcha(r)
        if r.status_code >= 400:
            raise ApiError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}",
                           status_code=r.status_code)
        return r

    # ── модели ────────────────────────────────────────────────────────────

    def models(self):
        """Список доступных моделей Qwen (id + краткое описание/возможности)."""
        r = self._request("GET", "/models/", with_bx=False)
        data = r.json().get("data", {})
        items = data.get("data") if isinstance(data, dict) else data
        out = []
        for m in items or []:
            info = m.get("info", {}) or {}
            meta = info.get("meta", {}) or {}
            out.append({
                "id": m.get("id"),
                "name": m.get("name"),
                "capabilities": meta.get("capabilities", {}),
                "chat_types": meta.get("chat_type", []),
                "max_context": meta.get("max_context_length"),
            })
        return out

    def model_ids(self):
        return [m["id"] for m in self.models() if m.get("id")]

    # ── чаты ──────────────────────────────────────────────────────────────

    def create_chat(self, model=None, chat_type="t2t", chat_mode="normal"):
        """Создать новый чат на сервере, вернуть chat_id."""
        model = model or self.default_model
        payload = {
            "chatId": "", "models": [model], "project_id": "",
            "timestamp": int(time.time() * 1000), "chat_type": chat_type, "chat_mode": chat_mode,
        }
        r = self._request("POST", "/chats/new", json=payload)
        body = r.json()
        _fdbg("chats/new response", body)
        chat_id = _extract_chat_id(body)
        if not chat_id:
            raise ApiError(f"chats/new: не нашёл chat_id в ответе: {r.text[:300]}")
        return chat_id

    def new_chat(self, model=None, tags=None):
        """Шорткат: создать чат и вернуть объект Chat."""
        return self.chats.new(model=model, tags=tags)

    # ── файлы: загрузка в OSS + разбор ────────────────────────────────────

    _PARSE_READY = ("success", "completed")
    _PARSE_TERMINAL = _PARSE_READY + ("failed", "error", "out_of_limit")

    def _get_sts_token(self, filename, filesize, filetype):
        """POST /files/getstsToken -> временные STS-креды Alibaba OSS."""
        r = self._request("POST", "/files/getstsToken", json={
            "filename": filename, "filesize": str(filesize), "filetype": filetype,
        })
        body = r.json()
        _fdbg("getstsToken response", body)
        if not body.get("success") or not body.get("data"):
            raise FileUploadError(f"getstsToken отклонён: {body.get('data') or body}")
        d = body["data"]
        return {
            "accessKeyId": d["access_key_id"], "accessKeySecret": d["access_key_secret"],
            "stsToken": d["security_token"], "bucket": d["bucketname"],
            "region": d.get("region", ""), "endpoint": d["endpoint"],
            "fileId": d["file_id"], "filePath": d["file_path"], "fileUrl": d["file_url"],
        }

    def _parse_file(self, file_id):
        """POST /files/parse — запустить разбор загруженного файла."""
        r = self._request("POST", "/files/parse", json={"file_id": file_id}, with_bx=False)
        _fdbg("files/parse response", r.json())

    def _parse_status(self, file_ids):
        """POST /files/parse/status -> статусы разбора по списку file_id."""
        r = self._request("POST", "/files/parse/status",
                          json={"file_id_list": list(file_ids)}, with_bx=False)
        body = r.json()
        _fdbg("files/parse/status response", body)
        data = body.get("data", {})
        entries = data if isinstance(data, list) else (data.get("list") or data.get("items") or [])
        out = {}
        for e in entries:
            fid = e.get("file_id") or e.get("id")
            if fid:
                out[fid] = str(e.get("parse_status") or e.get("status") or "").lower()
        return out

    def _wait_for_parse(self, file_ids, timeout=60):
        if not file_ids:
            return
        start = time.monotonic()
        pending = list(file_ids)
        while pending and time.monotonic() - start < timeout:
            statuses = self._parse_status(pending)
            pending = [f for f in pending if statuses.get(f, "") not in self._PARSE_TERMINAL]
            if pending:
                time.sleep(1.5)

    def upload_file(self, file, filename=None, content_type=None, vision=False,
                    wait=True, parse_timeout=60):
        """Загружает файл на Qwen (через Alibaba OSS), возвращает file-объект для
        сообщения (кладётся в files=[...]).

        file — путь, bytes, (filename, bytes[, content_type]) или файловый объект.

        vision=True — файл-картинка, которую модель "видит" (file_class="vision",
        showType/type="image"). Требует vision-модель (qwen3.7-plus и др. с
        capability vision). vision=False — обычный файл/документ: сервер извлекает
        из него текст (file_class="document"), для картинок это OCR.

        wait=True — дождаться, пока сервер закончит разбор файла (files/parse) —
        желательно перед отправкой сообщения.
        """
        fname, data, ctype = normalize_input(file, filename=filename, content_type=content_type)
        sts = self._get_sts_token(fname, len(data), ctype)

        from . import oss_upload
        oss_upload.put_object(sts, sts["filePath"], data, ctype, debug=_FILE_DEBUG)

        self._parse_file(sts["fileId"])
        if wait:
            self._wait_for_parse([sts["fileId"]], timeout=parse_timeout)

        if vision:
            file_class, show_type = "vision", "image"
        else:
            file_class, show_type = "document", "file"
        # для картинок в vision показываем как image; иначе file/video/audio по MIME
        top_type = show_type
        if not vision:
            if ctype.startswith("image/"):
                top_type = "image"
            elif ctype.startswith("video/"):
                top_type, show_type = "video", "video"
            elif ctype.startswith("audio/"):
                top_type, show_type = "audio", "audio"

        return {
            "type": top_type, "id": sts["fileId"], "url": sts["fileUrl"], "name": fname,
            "size": len(data), "file_type": ctype, "showType": show_type,
            "file_class": file_class, "status": "uploaded", "greenNet": "success",
            "progress": 0, "error": "", "collection_name": "",
        }

    # ── стриминг сообщения ────────────────────────────────────────────────

    def _stream_completion(self, chat_id, prompt, parent_id, model, thinking, search,
                           result_holder, chat_type="t2t", files=None, cancel_event=None):
        model = model or self.default_model
        msg_id = str(uuid.uuid4())
        child_id = str(uuid.uuid4())
        now = int(time.time())
        payload = {
            "stream": True, "version": "2.1", "incremental_output": True,
            "chat_id": chat_id, "chat_mode": "normal", "model": model,
            "parent_id": parent_id,
            "messages": [{
                "id": None, "fid": msg_id, "parentId": parent_id, "childrenIds": [child_id],
                "role": "user", "content": prompt, "user_action": "chat",
                "files": files or [],
                "timestamp": now, "models": [model], "model": "", "chat_type": chat_type,
                "feature_config": {
                    "thinking_enabled": thinking, "output_schema": "phase",
                    "research_mode": "normal", "auto_thinking": thinking,
                    "thinking_mode": "Auto", "thinking_format": "summary",
                    "auto_search": search,
                },
                "extra": {"meta": {"subChatType": chat_type}},
                "sub_chat_type": chat_type, "parent_id": parent_id,
            }],
            "timestamp": now,
        }
        _fdbg("chat/completions payload", payload)

        url = f"{BASE}/chat/completions?chat_id={chat_id}"
        headers = {**self._headers_for(url), "accept": "application/json",
                   "x-accel-buffering": "no"}
        r = self.session.post(url, headers=headers, json=payload, stream=True, timeout=(15, 300))
        if r.status_code in (401, 403) and self.email and self.password:
            self._login()
            headers = {**self._headers_for(url), "accept": "application/json",
                       "x-accel-buffering": "no"}
            r = self.session.post(url, headers=headers, json=payload, stream=True, timeout=(15, 300))
        if r.status_code >= 400:
            raise ApiError(f"chat/completions -> HTTP {r.status_code}: {r.text[:300]}",
                           status_code=r.status_code)

        # Без этого отмена (кнопка "Стоп") срабатывает только между уже
        # пришедшими SSE-чанками — если модель "думает" молча (thinking-фаза
        # без вывода) или сервер просто помедлил, отмена может ждать секунды.
        # Сторож в отдельном потоке рвёт соединение сразу по cancel_event —
        # iter_lines() ниже проснётся с исключением немедленно, а не когда
        # придут (или не придут) следующие данные.
        stream_finished = threading.Event()
        if cancel_event is not None:
            def _abort_on_cancel():
                while not stream_finished.is_set():
                    if cancel_event.wait(timeout=0.3):
                        try:
                            r.close()
                        except Exception:
                            pass
                        return
            threading.Thread(target=_abort_on_cancel, daemon=True).start()

        try:
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if _FILE_DEBUG:
                    sys.stderr.write(f"[qwen-debug] sse: {chunk[:400]}\n")

                created = data.get("response.created")
                if created:
                    result_holder["id"] = created.get("response_id") or created.get("parent_id")
                    continue

                # Сервер иногда шлёт SSE-событие вида {"error": {"code": ...,
                # "details": ...}, "response_id": ...} вместо choices (например,
                # "invalid_input" — увидено вживую на qwen3.8-max-preview). Без
                # этой проверки цикл ниже просто не находит "choices" и молча
                # отдаёт пустой ответ — ошибка полностью терялась.
                error = data.get("error")
                if error:
                    detail = error.get("details") or error.get("message") or str(error)
                    code = error.get("code", "")
                    raise ApiError(f"chat/completions вернул ошибку{f' ({code})' if code else ''}: {detail}")

                if "response_id" in data:
                    result_holder["id"] = data["response_id"]

                for choice in data.get("choices", []):
                    delta = choice.get("delta", {})
                    content = delta.get("content", "")
                    phase = delta.get("phase", "")
                    if not content:
                        continue
                    kind = "think" if phase in ("think", "thinking", "thinking_summary") else "response"
                    yield kind, content
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            return  # то, что уже отдано через yield — не теряется
        except Exception:
            # r.close() из сторожа может всплыть и другим типом исключения
            # в зависимости от того, на каком именно шаге чтения он попал —
            # если это была отмена, тоже просто тихо завершаемся.
            if cancel_event is not None and cancel_event.is_set():
                return
            raise
        finally:
            stream_finished.set()

    # ── пинг ──────────────────────────────────────────────────────────────

    def ping(self):
        """Жива ли сессия и отвечает ли API. Ничего не создаёт. Не кидает исключений."""
        t0 = time.monotonic()
        try:
            self._request("GET", "/users/user/settings", with_bx=False)
            return PingResult(ok=True, latency_ms=(time.monotonic() - t0) * 1000)
        except (QwenError, requests.exceptions.RequestException) as e:
            return PingResult(ok=False, latency_ms=(time.monotonic() - t0) * 1000, error=str(e))


def register_with_tempmail(name=None, password=None, store=None,
                           verify_timeout=120, **kwargs):
    """Полностью автономная регистрация через одноразовую почту mail.tm.

    Создаёт ящик на mail.tm, регистрирует аккаунт Qwen, решает WAF-капчу
    локально (V8 + PIL, без внешних сервисов), ждёт верификационное письмо,
    кликает ссылку — всё без участия пользователя.

    verify_timeout — секунды ожидания письма активации (по умолчанию 120).
    Возвращает (qwen_client, credentials) = {name, email, password}.
    Сохрани credentials — почта одноразовая.
    """
    from . import tempmail

    try:
        mailbox = tempmail.create_mailbox()
    except tempmail.TempMailError:
        mailbox = tempmail.create_mailbox()  # ретрай — домены mail.tm иногда временно недоступны

    name = name or f"Qwen Test {secrets.token_hex(3)}"
    password = password or tempmail.random_password()

    data, session, umid, bx = _do_signup(name, mailbox.address, password)
    _fdbg("signup response", data)

    token = auth.extract_token(data)
    if not token:
        # Qwen требует email-верификации — ждём письмо, извлекаем ссылку
        msg = mailbox.wait_for_message(timeout=verify_timeout)
        if msg is None:
            raise AuthError(
                f"Верификационное письмо не пришло за {verify_timeout}с "
                f"(email: {mailbox.address}). "
                "Проверь, не блокирует ли Qwen домены mail.tm — тогда нужен другой провайдер."
            )
        # Ищем ссылку с qwen.ai; запасной вариант — любая ссылка с verify/confirm/activ
        links = tempmail.extract_links(msg, domain_filter="qwen.ai")
        if not links:
            links = [l for l in tempmail.extract_links(msg)
                     if any(kw in l.lower() for kw in ("verify", "confirm", "activ", "email"))]
        if not links:
            raise AuthError(
                f"Ссылка активации не найдена в письме. "
                f"Subject: {msg.get('subject', '?')} | "
                f"Все ссылки: {tempmail.extract_links(msg)}"
            )
        verify_link = links[0]
        _fdbg("verify_link", verify_link)
        auth.verify_email(session, verify_link)

    credentials = {"name": name, "email": mailbox.address, "password": password}
    client = Qwen(email=mailbox.address, password=password, store=store, umid=umid, **kwargs)
    _fdbg("register_with_tempmail credentials", credentials)
    return client, credentials


# ── вспомогательные ──────────────────────────────────────────────────────

def _extract_chat_id(body):
    """chat_id из ответа /chats/new. Формат ответа вживую не был снят (главный
    аккаунт ушёл в капчу при разведке), поэтому пробуем несколько мест."""
    if not isinstance(body, dict):
        return None
    data = body.get("data", body)
    if isinstance(data, dict):
        for key in ("id", "chat_id", "chatId"):
            if data.get(key):
                return data[key]
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("id", "chat_id", "chatId"):
                if inner.get(key):
                    return inner[key]
    for key in ("id", "chat_id", "chatId"):
        if body.get(key):
            return body[key]
    return None


def _detect_captcha(r):
    """Aliyun WAF отдаёт HTML со slider-капчей вместо JSON, когда risk-score
    высок. Бросает WafCaptchaError с HTML — caller может решить через waf_solver."""
    ct = r.headers.get("content-type", "")
    if "text/html" in ct and ("renderData" in r.text[:2000] or "AliyunCaptcha" in r.text[:5000]):
        raise WafCaptchaError(r.text)
