"""DeepSeek — главный клиент библиотеки."""

import json
import os
import sys
import threading
import time

import requests

_FILE_DEBUG = bool(os.environ.get("DS_DEBUG"))


def _fdbg(label, obj):
    if _FILE_DEBUG:
        sys.stderr.write(f"[ds-debug] {label}: {json.dumps(obj, ensure_ascii=False)[:1500]}\n")

from . import auth
from .chat import ChatManager
from .exceptions import ApiError, AuthError, DeepSeekError, FileUploadError
from .files import normalize_input
from .ping import PingResult
from .store import MemoryStore

BASE = "https://chat.deepseek.com/api/v0"
# Подтверждено захватом реального трафика с chat.deepseek.com: model_type
# "vision" — валидное третье значение наравне с "default"/"expert". Файл для
# него должен быть загружен с заголовком x-model-type: vision на upload_file
# (см. upload_file(..., vision=True)) — никакого отдельного fork-эндпоинта
# в реальном трафике нет.
MODELS = ("default", "expert", "vision")


class DeepSeek:
    def __init__(self, email=None, password=None, token=None, cookie=None,
                 store=None, device_id=None,
                 default_model="default", default_think=False, default_search=False):
        if default_model not in MODELS:
            raise ValueError(f"model должен быть одним из {MODELS}, получено: {default_model!r}")

        self.email = email
        self.password = password
        self.device_id = device_id
        self.default_model = default_model
        self.default_think = default_think
        self.default_search = default_search

        self.store = store or MemoryStore()
        self._auth_lock = threading.Lock()

        self.session = requests.Session()
        self.session.headers.update({
            "accept": "*/*",
            "accept-encoding": "identity",
            "content-type": "application/json",
            "origin": "https://chat.deepseek.com",
            "referer": "https://chat.deepseek.com/",
            "user-agent": auth.UA,
            "x-app-version": "2.0.0",
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-locale": "en_US",
            "x-client-platform": "web",
            "x-client-timezone-offset": "10800",
            "x-client-version": "2.0.0",
        })

        if token:
            self._set_credentials(token, cookie or "", persist=store is not None)
        else:
            saved_token = self.store.get("token")
            if saved_token:
                self._set_credentials(saved_token, self.store.get("cookie", ""), persist=False)
            elif email and password:
                self._login()
            else:
                raise AuthError(
                    "Нужно передать либо token=..., либо email=...+password=..., "
                    "либо store с уже сохранёнными credentials."
                )

        self.chats = ChatManager(self)

    # ── авторизация ──────────────────────────────────────────────────────

    def _set_credentials(self, token, cookie, persist=True):
        self.token = token
        self.cookie = cookie
        self.session.headers["authorization"] = f"Bearer {token}"
        self.session.headers["cookie"] = cookie
        if persist:
            self.store.set("token", token)
            self.store.set("cookie", cookie)

    def _login(self):
        if not (self.email and self.password):
            raise AuthError("Bearer token недействителен, а email/password не заданы — обновить некому.")
        with self._auth_lock:
            token = auth.login(self.email, self.password, device_id=self.device_id)
            self._set_credentials(token, "", persist=True)

    def relogin(self):
        """Принудительно получить свежий bearer token (например, если решил, что старый мог протухнуть)."""
        self._login()

    @classmethod
    def register(cls, email, password, gmail_app_password=None, headless=True,
                 code_wait_timeout=180, proxy=None, store=None, **kwargs):
        """Полностью автоматическая регистрация нового DeepSeek аккаунта.

        Требует: Google Chrome установлен + Gmail App Password для чтения кода.

        email: адрес с plus-alias'ом, например 'potapenkofil+claudereg@gmail.com'.
               Gmail игнорирует +xxx (письмо придёт на potapenkofil@gmail.com),
               а DeepSeek видит каждый alias как отдельный аккаунт.
        password: пароль нового аккаунта.
        gmail_app_password: 16-символьный App Password с
               https://myaccount.google.com/apppasswords
               (либо env GMAIL_APP_PASSWORD).
        headless: True — Chrome скрыт.
        code_wait_timeout: сколько ждать письма (сек, default 3 мин).

        Flow:
          Chrome headless → форма /sign_up → сайт сам решает hCaptcha invisible
          и PoW → приходит письмо на Gmail → IMAP забирает 6-значный код →
          заполняем пароли+код → Register → готовый DeepSeek клиент.

        Ограничения:
          - hCaptcha invisible пропускает "доверенные" браузеры молча. При
            частых регистрациях с одного IP может начать требовать визуальный
            challenge — тогда регистрация упадёт с таймаутом. Для 1-2 акков
            в час обычно ок.
          - IP-rate-limit DeepSeek: после ~30-50 регистраций/час бан на час.
        """
        token = auth.register(email, password,
                              gmail_app_password=gmail_app_password,
                              headless=headless,
                              code_wait_timeout=code_wait_timeout,
                              proxy=proxy)
        return cls(email=email, password=password, token=token, store=store, **kwargs)

    # ── низкоуровневые запросы с авто-релогином на 401 ──────────────────

    def _request(self, method, path, retry_on_401=True, **kwargs):
        timeout = kwargs.pop("timeout", 30)
        r = self.session.request(method, f"{BASE}{path}", timeout=timeout, **kwargs)
        if r.status_code == 401 and retry_on_401:
            self._login()
            return self._request(method, path, retry_on_401=False, timeout=timeout, **kwargs)
        if r.status_code >= 400:
            raise ApiError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}",
                            status_code=r.status_code)
        return r

    # ── chat sessions / PoW ──────────────────────────────────────────────

    def create_chat_session(self):
        r = self._request("POST", "/chat_session/create", json={})
        return r.json()["data"]["biz_data"]["chat_session"]["id"]

    def _solve_pow(self, target_path="/api/v0/chat/completion"):
        r = self._request("POST", "/chat/create_pow_challenge",
                           json={"target_path": target_path})
        challenge = r.json()["data"]["biz_data"]["challenge"]
        return auth.solve_pow(challenge)

    def _stream_completion(self, chat_id, prompt, parent_id, model, think, search, result_holder,
                            ref_file_ids=None):
        if model not in MODELS:
            raise ValueError(f"model должен быть одним из {MODELS}, получено: {model!r}")

        ref_file_ids = ref_file_ids or []
        if ref_file_ids and model == "expert":
            model = "default"  # DeepSeek: вложения не поддерживаются в expert-режиме

        pow_token = self._solve_pow()
        payload = {
            "chat_session_id": chat_id,
            "parent_message_id": parent_id,
            "model_type": model,
            "prompt": prompt,
            "ref_file_ids": ref_file_ids,
            "thinking_enabled": think,
            "search_enabled": search,
            "action": None,
            "preempt": False,
        }
        _fdbg("chat/completion payload", payload)

        def do_request():
            return self.session.post(f"{BASE}/chat/completion",
                                      headers={"x-ds-pow-response": pow_token},
                                      json=payload, stream=True, timeout=(15, 300))

        r = do_request()
        if r.status_code == 401:
            self._login()
            r = do_request()
        if r.status_code >= 400:
            raise ApiError(f"chat/completion -> HTTP {r.status_code}: {r.text[:300]}",
                            status_code=r.status_code)

        current_type = "RESPONSE"
        try:
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if _FILE_DEBUG:
                    sys.stderr.write(f"[ds-debug] sse: {json.dumps(data, ensure_ascii=False)[:500]}\n")

                if "response_message_id" in data:
                    result_holder["id"] = data["response_message_id"]
                    continue

                if "v" in data and isinstance(data.get("v"), dict) and "response" in data["v"]:
                    for frag in data["v"]["response"].get("fragments", []):
                        current_type = frag.get("type", "RESPONSE")
                        yield ("think" if current_type == "THINK" else "response", frag.get("content", ""))
                    continue

                p = data.get("p", "")
                if p == "response/fragments" and data.get("o") == "APPEND":
                    for frag in data.get("v", []):
                        current_type = frag.get("type", "RESPONSE")
                        yield ("think" if current_type == "THINK" else "response", frag.get("content", ""))
                    continue

                if "content" in p and isinstance(data.get("v"), str):
                    yield ("think" if current_type == "THINK" else "response", data["v"])
                    continue

                if not p and "o" not in data and isinstance(data.get("v"), str):
                    yield ("think" if current_type == "THINK" else "response", data["v"])
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            return  # то, что уже было отдано через yield — не теряется

    def new_chat(self, tags=None, model=None):
        """Шорткат для ds.chats.new(...)."""
        return self.chats.new(tags=tags, model=model)

    # ── файлы: загрузка (обычная/vision), ожидание разбора ───────────────

    _FILE_READY_STATUSES = ("SUCCESS", "COMPLETED", "CONTENT_EMPTY")
    _FILE_TERMINAL_STATUSES = _FILE_READY_STATUSES + ("FAILED", "ERROR", "PARSE_FAILED")

    def _upload_file_raw(self, data, filename, content_type, model_type="default", think=False):
        # Подтверждено вживую (DevTools на chat.deepseek.com): режим файла
        # (обычный/OCR или vision) передаётся заголовками ПРЯМО на upload_file —
        # x-model-type/x-file-size/x-thinking-enabled. Отдельного эндпоинта
        # форка в реальном трафике нет, а id, который тот эндпоинт возвращал,
        # не был валиден для ref_file_ids (отсюда пустые ответы модели).
        def do_request():
            pow_token = self._solve_pow(target_path="/api/v0/file/upload_file")
            return self.session.post(
                f"{BASE}/file/upload_file",
                headers={
                    "x-ds-pow-response": pow_token,
                    "content-type": None,
                    "x-model-type": model_type,
                    "x-file-size": str(len(data)),
                    "x-thinking-enabled": "1" if think else "0",
                },
                files={"file": (filename, data, content_type)},
                timeout=60,
            )

        r = do_request()
        if r.status_code == 401:
            self._login()
            r = do_request()
        if r.status_code >= 400:
            raise FileUploadError(f"upload_file -> HTTP {r.status_code}: {r.text[:300]}")

        body = r.json()
        _fdbg("upload_file response", body)
        biz = body.get("data", {}).get("biz_data", {})
        file_id = biz.get("id") or body.get("data", {}).get("id")
        if not file_id:
            raise FileUploadError(f"upload_file: сервер не вернул id файла: {r.text[:300]}")
        return file_id

    def _fetch_file_statuses(self, file_ids):
        r = self._request("GET", "/file/fetch_files", params={"file_ids": file_ids})
        body = r.json()
        _fdbg("fetch_files response", body)
        biz = body.get("data", {}).get("biz_data", {})
        entries = biz.get("files") or biz.get("file_list") or biz.get("items") or []
        return {e.get("id") or e.get("file_id"): e for e in entries}

    def _wait_for_files(self, file_ids, timeout=30):
        if not file_ids:
            return []
        start = time.monotonic()
        pending = list(file_ids)
        ready = []
        while pending and time.monotonic() - start < timeout:
            statuses = self._fetch_file_statuses(pending)
            still_pending = []
            for fid in pending:
                status = str(statuses.get(fid, {}).get("status", "")).upper()
                if status in self._FILE_READY_STATUSES:
                    ready.append(fid)
                elif status not in self._FILE_TERMINAL_STATUSES:
                    still_pending.append(fid)
                # иначе — разобран, но неуспешно: не подставляем в ref_file_ids
            pending = still_pending
            if pending:
                time.sleep(1)
        return ready

    def upload_file(self, file, filename=None, content_type=None, vision=False, wait=True, timeout=30,
                     think=False):
        """Загружает файл на DeepSeek, возвращает готовый file_id для ref_file_ids.

        file — путь, bytes, (filename, bytes[, content_type]) или файловый объект.

        vision=False (по умолчанию) — обычная загрузка, как в обычном чате: DeepSeek
        сам извлекает из файла текст (для фото это OCR — видит текст на картинке,
        не саму картинку).
        vision=True — загружает файл с заголовком x-model-type: vision: модель
        реально "смотрит" на изображение (для фото/скриншотов), а не читает
        извлечённый из него текст. Тот же file_id идёт в ref_file_ids — при этом
        чат тоже должен использовать model_type="vision" (см. chat.send(vision=...)).

        think — должен совпадать с thinking_enabled сообщения, к которому пойдёт
        этот файл (в реальном трафике upload_file шлёт x-thinking-enabled с тем
        же значением).

        wait=True — дожидается, пока DeepSeek закончит разбор файла (обязательно
        перед тем, как использовать file_id в chat/completion — иначе модель
        может не успеть его увидеть).
        """
        fname, data, ctype = normalize_input(file, filename=filename, content_type=content_type)
        file_id = self._upload_file_raw(data, fname, ctype, model_type="vision" if vision else "default",
                                         think=think)
        if wait:
            ready = self._wait_for_files([file_id], timeout=timeout)
            if not ready:
                raise FileUploadError(f"Файл {fname!r} не был разобран за {timeout}с (file_id={file_id})")
            file_id = ready[0]
        return file_id

    # ── пинг-проверки ─────────────────────────────────────────────────────

    def ping(self):
        """Проверка аккаунта: жив ли токен и отвечает ли DeepSeek API.

        Ничего не создаёт (ни чат, ни сообщение) — только читает список последних
        чатов. Не кидает исключений — результат всегда PingResult(ok, latency_ms, error).
        """
        t0 = time.monotonic()
        try:
            self._request("GET", "/chat_session/fetch_page?count=1")
            return PingResult(ok=True, latency_ms=(time.monotonic() - t0) * 1000)
        except (DeepSeekError, requests.exceptions.RequestException) as e:
            return PingResult(ok=False, latency_ms=(time.monotonic() - t0) * 1000, error=str(e))
