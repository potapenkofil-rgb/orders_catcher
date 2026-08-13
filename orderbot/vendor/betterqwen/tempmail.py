"""Одноразовая почта через mail.tm — публичный REST API, без ключей.

Тот же сервис, что использует Telegram-бот пользователя
(Ящик проектов/tempmail/bot.py), но здесь — прямой синхронный клиент на
`requests` (без aiohttp/Telegram), в стиле остальной библиотеки. Нужен, чтобы
регистрировать тестовые аккаунты Qwen полностью автономно, без участия
пользователя (см. register_with_tempmail() в __init__.py).
"""

import re
import secrets
import string
import time

import requests

_URL_RE = re.compile(r'https?://[^\s"\'<>\)]+')


def extract_links(message, domain_filter=None):
    """URL-ы из тела письма (HTML или plain text).

    domain_filter — если задан, оставляет только ссылки, содержащие эту подстроку.
    html в mail.tm-ответе может быть строкой или списком строк.
    """
    parts = []
    html = message.get("html", [])
    if isinstance(html, list):
        parts.extend(str(h) for h in html)
    elif html:
        parts.append(str(html))
    text = message.get("text") or ""
    if text:
        parts.append(text)
    body = " ".join(parts)
    links = _URL_RE.findall(body)
    if domain_filter:
        links = [l for l in links if domain_filter in l]
    return links

from .exceptions import QwenError

BASE = "https://api.mail.tm"


class TempMailError(QwenError):
    pass


def _rand_local(n=12):
    first = secrets.choice(string.ascii_lowercase)
    rest = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(n - 1))
    return first + rest


def random_password(n=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _request(method, path, token=None, json=None, timeout=20):
    headers = {"accept": "application/ld+json", "content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    r = requests.request(method, f"{BASE}{path}", headers=headers, json=json, timeout=timeout)
    if r.status_code == 204:
        return None
    if r.status_code >= 400:
        raise TempMailError(f"mail.tm {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
    if not r.text:
        return None
    return r.json()


def get_domains():
    """Список доменов, доступных прямо сейчас (mail.tm их периодически меняет)."""
    data = _request("GET", "/domains?page=1")
    members = (data or {}).get("hydra:member", [])
    return [d["domain"] for d in members if d.get("isActive", True)]


class Mailbox:
    """Один одноразовый ящик: адрес + токен для чтения входящих."""

    def __init__(self, account_id, address, password, token):
        self.account_id = account_id
        self.address = address
        self.password = password
        self.token = token

    def refresh_token(self):
        data = _request("POST", "/token", json={"address": self.address, "password": self.password})
        self.token = data["token"]
        return self.token

    def list_messages(self):
        data = _request("GET", "/messages?page=1", token=self.token)
        return (data or {}).get("hydra:member", [])

    def get_message(self, message_id):
        return _request("GET", f"/messages/{message_id}", token=self.token)

    def wait_for_message(self, timeout=60, poll_interval=2, predicate=None):
        """Ждёт первое (подходящее predicate(header)) письмо. None, если не дождались.

        predicate — необязательный фильтр по заголовку письма (dict из
        list_messages: subject/from/intro и т.п.), например
        lambda m: "qwen" в m.get("from",{}).get("address","").lower().
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                messages = self.list_messages()
            except TempMailError:
                messages = []
            for m in messages:
                if predicate is None or predicate(m):
                    return self.get_message(m["id"])
            time.sleep(poll_interval)
        return None

    def delete(self):
        try:
            _request("DELETE", f"/accounts/{self.account_id}", token=self.token)
        except TempMailError:
            pass


def create_mailbox(address=None):
    """Создать новый одноразовый ящик на mail.tm. Возвращает Mailbox."""
    domains = get_domains()
    if not domains:
        raise TempMailError("mail.tm не отдал ни одного активного домена")
    if address is None:
        address = f"{_rand_local()}@{domains[0]}"
    password = random_password()
    created = _request("POST", "/accounts", json={"address": address, "password": password})
    token_data = _request("POST", "/token", json={"address": address, "password": password})
    return Mailbox(account_id=created["id"], address=address, password=password,
                   token=token_data["token"])
