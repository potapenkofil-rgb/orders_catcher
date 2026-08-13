"""Chat — один диалог. ChatManager — список/поиск/теги чатов аккаунта."""

import time

import requests

from .exceptions import DeepSeekError
from .files import as_list
from .ping import PingResult


class Chat:
    def __init__(self, client, chat_id, parent_message_id=None):
        self._client = client
        self.id = chat_id
        self.parent_message_id = parent_message_id

    def stream(self, prompt, model=None, think=None, search=None,
               files=None, vision=None, ref_file_ids=None, file_timeout=30):
        """Генератор (kind, text): kind = 'think' | 'response'.

        Полный контроль над выводом — сам решаешь, показывать ли 'think',
        писать в Telegram отдельным сообщением и т.п.

        files — фото/документы для обычной загрузки (DeepSeek извлекает текст;
        для фото это OCR — видит текст на картинке, не саму картинку). Работает
        поверх обычного model_type ("default"/"expert"), просто добавляет вложение.

        vision — фото для vision-режима: модель реально "смотрит" на изображение
        (цвета, объекты, композицию), а не читает с него OCR-текст. Подтверждено
        захватом реального трафика: файл должен быть загружен с заголовком
        x-model-type: vision (upload_file(..., vision=True), без отдельного
        fork-эндпоинта), а сам чат — использовать model_type="vision". Поэтому
        при наличии vision-файлов model принудительно становится "vision", что
        бы ни было передано в model=.

        ВАЖНО: vision работает только ПЕРВЫМ сообщением в чате (свежий
        ds.new_chat()). DeepSeek фиксирует model_type за сессией по первому
        сообщению — если чат уже начался с "default"/"expert", попытка
        переключить его на "vision" вторым сообщением молча откатывается
        сервером обратно на "default", и ответ приходит пустым. Библиотека
        это ловит заранее и кидает ValueError вместо тихого пустого ответа.

        Оба (files/vision) принимают один файл или список: путь, bytes,
        (filename, bytes[, content_type]) или файловый объект. ref_file_ids — id
        уже загруженных файлов (см. ds.upload_file()), для повторного использования
        без повторной загрузки.

        files (без vision) работают только с моделью "default" — если запрошен
        "expert", он будет автоматически заменён (DeepSeek не поддерживает файлы
        в expert-режиме).
        """
        model = model or self._client.default_model
        think = self._client.default_think if think is None else think
        search = self._client.default_search if search is None else search

        ids = list(ref_file_ids) if ref_file_ids else []
        for f in as_list(files):
            ids.append(self._client.upload_file(f, wait=True, timeout=file_timeout, think=think))

        vision_files = as_list(vision)
        if vision_files:
            if self.parent_message_id is not None:
                raise ValueError(
                    "vision= работает только первым сообщением в чате — DeepSeek "
                    "фиксирует model_type за сессией по первому сообщению и молча "
                    "откатывает 'vision' на 'default' в уже начатом чате (ответ "
                    "приходит пустым). Используй свежий ds.new_chat() для vision-сообщений."
                )
            for f in vision_files:
                ids.append(self._client.upload_file(f, vision=True, wait=True, timeout=file_timeout, think=think))
            model = "vision"

        holder = {}
        for kind, text in self._client._stream_completion(
                self.id, prompt, self.parent_message_id, model, think, search, holder,
                ref_file_ids=ids):
            yield kind, text
        if "id" in holder:
            self.parent_message_id = holder["id"]

    def send(self, prompt, model=None, think=None, search=None, show_thinking=False,
              files=None, vision=None, ref_file_ids=None, file_timeout=30):
        """Удобный блокирующий вызов — возвращает готовый текст ответа.

        show_thinking=True — печатает рассуждения модели в stdout по мере
        поступления (для CLI/отладки). Для полного контроля используй .stream().
        files/vision/ref_file_ids — см. .stream().
        """
        parts = []
        printed_think = False
        for kind, text in self.stream(prompt, model=model, think=think, search=search,
                                       files=files, vision=vision, ref_file_ids=ref_file_ids,
                                       file_timeout=file_timeout):
            if kind == "response":
                parts.append(text)
            elif show_thinking:
                print(text, end="", flush=True)
                printed_think = True
        if printed_think:
            print()
        return "".join(parts)

    def tag(self, tag):
        self._client.chats.tag(self.id, tag)

    def untag(self, tag):
        self._client.chats.untag(self.id, tag)

    @property
    def tags(self):
        return self._client.chats.tags_of(self.id)

    def ping(self):
        """Проверка этого конкретного чата: существует ли он ещё на сервере DeepSeek.

        Ничего не отправляет. Если чат найден — заодно синхронизирует
        parent_message_id (как ds.chats.get()). Не кидает исключений — результат
        всегда PingResult(ok, latency_ms, error).
        """
        t0 = time.monotonic()
        try:
            r = self._client._request("GET", f"/chat/history_messages?chat_session_id={self.id}")
            session = r.json()["data"]["biz_data"].get("chat_session")
            if not session:
                return PingResult(ok=False, latency_ms=(time.monotonic() - t0) * 1000,
                                   error="чат не найден на сервере")
            self.parent_message_id = session.get("current_message_id")
            return PingResult(ok=True, latency_ms=(time.monotonic() - t0) * 1000)
        except (DeepSeekError, requests.exceptions.RequestException) as e:
            return PingResult(ok=False, latency_ms=(time.monotonic() - t0) * 1000, error=str(e))

    def __repr__(self):
        return f"<Chat {self.id}>"


class ChatManager:
    """Доступен как ds.chats — управление чатами и локальными тегами."""

    def __init__(self, client):
        self._client = client

    def new(self, tags=None, model=None):
        """Создать новый чат. tags — строка или список строк для сразу-пометки."""
        chat_id = self._client.create_chat_session()
        chat = Chat(self._client, chat_id)
        if tags:
            for t in ([tags] if isinstance(tags, str) else tags):
                self.tag(chat_id, t)
        return chat

    def get(self, chat_id):
        """Продолжить существующий чат — подтягивает id последнего сообщения с сервера."""
        r = self._client._request("GET", f"/chat/history_messages?chat_session_id={chat_id}")
        session = r.json()["data"]["biz_data"]["chat_session"]
        return Chat(self._client, chat_id, parent_message_id=session.get("current_message_id"))

    def list(self, n=20):
        """Последние n чатов аккаунта (из API DeepSeek, список dict'ов с id/title/model_type)."""
        r = self._client._request("GET", f"/chat_session/fetch_page?count={n}")
        return r.json()["data"]["biz_data"]["chat_sessions"]

    def delete(self, chat_id):
        """Удалить чат на сервере.

        ВНИМАНИЕ: этот эндпоинт не был захвачен/проверен вживую в трафике —
        если DeepSeek использует другой путь, вызов кинет ApiError.
        """
        self._client._request("POST", "/chat_session/delete", json={"chat_session_id": chat_id})

    # ── локальные теги (хранятся в Store клиента, не на сервере DeepSeek) ──

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
        """Все (или последние n) chat_id, помеченные данным тегом — сначала новые."""
        data = self._tags_data()
        matches = [(cid, e["tagged_at"]) for cid, e in data.items() if tag in e.get("tags", [])]
        matches.sort(key=lambda pair: pair[1], reverse=True)
        ids = [cid for cid, _ in matches]
        return ids[:n] if n else ids
