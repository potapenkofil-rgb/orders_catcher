"""Chat — один диалог. ChatManager — создание/поиск/теги чатов аккаунта."""

import time

import requests

from .exceptions import QwenError
from .files import as_list
from .ping import PingResult


class Chat:
    def __init__(self, client, chat_id, model=None, parent_id=None):
        self._client = client
        self.id = chat_id
        self.model = model or client.default_model
        self.parent_id = parent_id  # id последнего ответа — цепочка сообщений

    def stream(self, prompt, model=None, thinking=None, search=None,
               files=None, vision=None, ref_files=None, file_timeout=60, cancel_event=None):
        """Генератор (kind, text): kind = 'think' | 'response'.

        Полный контроль над выводом — сам решаешь, показывать ли 'think'
        (рассуждения модели), писать ли его отдельно в Telegram и т.п.

        thinking — включить рассуждения модели (phase 'thinking_summary').
        search — авто-веб-поиск.

        files — обычные файлы/документы: сервер извлекает из них текст (для фото
        это OCR). vision — картинки, которые модель "видит" (цвета/объекты/
        композиция), нужна vision-модель (qwen3.7-plus и др.). ref_files — уже
        загруженные file-объекты (из qwen.upload_file()) для переиспользования
        без повторной загрузки.

        Каждый из files/vision принимает один файл или список: путь, bytes,
        (filename, bytes[, content_type]) или файловый объект.

        cancel_event — threading.Event: если задан, при его установке
        соединение обрывается немедленно (не дожидаясь следующего SSE-чанка).
        """
        model = model or self.model
        thinking = self._client.default_thinking if thinking is None else thinking
        search = self._client.default_search if search is None else search

        file_objs = list(ref_files) if ref_files else []
        for f in as_list(files):
            file_objs.append(self._client.upload_file(f, vision=False, wait=True,
                                                      parse_timeout=file_timeout))
        for f in as_list(vision):
            file_objs.append(self._client.upload_file(f, vision=True, wait=True,
                                                      parse_timeout=file_timeout))

        holder = {}
        for kind, text in self._client._stream_completion(
                self.id, prompt, self.parent_id, model, thinking, search, holder,
                files=file_objs, cancel_event=cancel_event):
            yield kind, text
        if holder.get("id"):
            self.parent_id = holder["id"]

    def send(self, prompt, model=None, thinking=None, search=None, show_thinking=False,
             files=None, vision=None, ref_files=None, file_timeout=60):
        """Блокирующий вызов — возвращает готовый текст ответа.

        show_thinking=True — печатает рассуждения модели в stdout по мере
        поступления (для CLI/отладки). Для полного контроля используй .stream().
        files/vision/ref_files — см. .stream().
        """
        parts = []
        printed = False
        for kind, text in self.stream(prompt, model=model, thinking=thinking, search=search,
                                      files=files, vision=vision, ref_files=ref_files,
                                      file_timeout=file_timeout):
            if kind == "response":
                parts.append(text)
            elif show_thinking:
                print(text, end="", flush=True)
                printed = True
        if printed:
            print()
        return "".join(parts)

    def tag(self, tag):
        self._client.chats.tag(self.id, tag)

    def untag(self, tag):
        self._client.chats.untag(self.id, tag)

    @property
    def tags(self):
        return self._client.chats.tags_of(self.id)

    def delete(self):
        """Удалить этот чат на сервере."""
        self._client.chats.delete(self.id)

    def ping(self):
        """Проверка этого чата: существует ли он ещё на сервере Qwen.

        Ничего не отправляет. Не кидает исключений — результат всегда
        PingResult(ok, latency_ms, error).
        """
        t0 = time.monotonic()
        try:
            self._client._request("GET", f"/chats/{self.id}")
            return PingResult(ok=True, latency_ms=(time.monotonic() - t0) * 1000)
        except (QwenError, requests.exceptions.RequestException) as e:
            return PingResult(ok=False, latency_ms=(time.monotonic() - t0) * 1000, error=str(e))

    def __repr__(self):
        return f"<Chat {self.id} model={self.model}>"


class ChatManager:
    """Доступен как qwen.chats — создание чатов и локальные теги."""

    def __init__(self, client):
        self._client = client

    def new(self, model=None, tags=None):
        """Создать новый чат. tags — строка или список строк для сразу-пометки."""
        model = model or self._client.default_model
        chat_id = self._client.create_chat(model=model)
        chat = Chat(self._client, chat_id, model=model)
        if tags:
            for t in ([tags] if isinstance(tags, str) else tags):
                self.tag(chat_id, t)
        return chat

    def get(self, chat_id, model=None, parent_id=None):
        """Обернуть существующий chat_id в объект Chat (для продолжения)."""
        return Chat(self._client, chat_id, model=model, parent_id=parent_id)

    def list(self, page=1):
        """Последние чаты аккаунта (список dict'ов из API Qwen)."""
        r = self._client._request("GET", f"/chats/?page={page}&exclude_project=true")
        body = r.json()
        data = body.get("data", body)
        if isinstance(data, dict):
            return data.get("data") or data.get("chats") or []
        return data or []

    def delete(self, chat_id):
        """Удалить чат на сервере."""
        self._client._request("DELETE", f"/chats/{chat_id}")

    # ── локальные теги (в Store клиента, не на сервере Qwen) ──────────────

    def _tags_data(self):
        return self._client.store.get("chat_tags", {})

    def tag(self, chat_id, tag):
        data = self._tags_data()
        entry = data.setdefault(chat_id, {"tags": [], "tagged_at": time.time()})
        if tag not in entry["tags"]:
            entry["tags"].append(tag)
        entry["tagged_at"] = time.time()
        self._client.store.set("chat_tags", data)

    def untag(self, chat_id, tag):
        data = self._tags_data()
        entry = data.get(chat_id)
        if entry and tag in entry["tags"]:
            entry["tags"].remove(tag)
            self._client.store.set("chat_tags", data)

    def tags_of(self, chat_id):
        return list(self._tags_data().get(chat_id, {}).get("tags", []))

    def tagged(self, tag, n=None):
        """chat_id'ы с данным тегом — сначала новые."""
        data = self._tags_data()
        matches = [(cid, e["tagged_at"]) for cid, e in data.items() if tag in e.get("tags", [])]
        matches.sort(key=lambda pair: pair[1], reverse=True)
        ids = [cid for cid, _ in matches]
        return ids[:n] if n else ids
