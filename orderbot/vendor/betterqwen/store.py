"""Хранилища состояния (cookie-сессия, локальные теги чатов) — единый key-value интерфейс.

Любой объект с методами get(key, default) / set(key, value) подходит как store.
Пиши свой (Redis, SQLite, БД) — Qwen() ничего не знает про формат хранения.

В отличие от betterDeepSeek (bearer token), у Qwen аутентификация — обычная
httpOnly cookie-сессия (см. auth.py) — поэтому здесь хранится не токен, а
словарь cookies (session.cookies.get_dict()).
"""

import json
import os
import tempfile
import threading


class MemoryStore:
    """Ничего не сохраняет на диск — живёт только в рамках процесса."""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value


class FileStore:
    """JSON-файл на диске. Один файл — один аккаунт/пользователь.

    Запись атомарна (пишет во временный файл, потом переименовывает),
    так что параллельные процессы не увидят битый JSON.
    """

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._write({})

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data):
        directory = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def get(self, key, default=None):
        with self._lock:
            return self._read().get(key, default)

    def set(self, key, value):
        with self._lock:
            data = self._read()
            data[key] = value
            self._write(data)

    def all(self):
        with self._lock:
            return self._read()
