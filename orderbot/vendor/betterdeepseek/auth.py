"""Всё, что связано с получением/обновлением bearer token: WAF challenge, PoW, логин.

Ничего здесь не привязано к диску — токены возвращаются наверх (в client.py),
где решается, сохранять их или нет (через Store).

Чистый Python — Node.js не требуется (WAF решается через waf_solver.py на
py_mini_racer/V8, PoW — через pow_hash_fast.py, NumPy-векторизованную версию
побитового порта исходного JS-алгоритма, см. pow_hash.py/verify_pow_hash*.py).
"""

import base64
import json
import secrets

import requests

from .exceptions import AuthError, PowSolveError
from .pow_hash_fast import solve_pow_fast as _solve_pow_hash
from .waf_solver import solve_waf_token

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
BASE = "https://chat.deepseek.com/api/v0"

__all__ = ["solve_waf_token", "solve_pow", "generate_device_id", "login", "register", "UA", "BASE"]


def solve_pow(challenge, timeout=30):
    """challenge — dict из ответа /chat/create_pow_challenge.
    Возвращает готовое значение для заголовка x-ds-pow-response.

    timeout принят для обратной совместимости сигнатуры, не используется —
    решение чистым перебором (pow_hash.py) занимает миллисекунды.
    """
    answer = _solve_pow_hash(
        challenge["challenge"], challenge["salt"], challenge["difficulty"], challenge["expire_at"],
    )
    if answer is None:
        raise PowSolveError(f"Не удалось подобрать ответ на PoW challenge (difficulty={challenge.get('difficulty')})")

    result = {
        "algorithm": challenge["algorithm"],
        "challenge": challenge["challenge"],
        "salt": challenge["salt"],
        "answer": answer,
        "signature": challenge["signature"],
        "target_path": challenge["target_path"],
    }
    return base64.b64encode(json.dumps(result, separators=(",", ":")).encode()).decode()


def generate_device_id():
    """Генерирует правдоподобный base64 device_id для /users/login.

    ВНИМАНИЕ: это НЕ воспроизведение реального алгоритма fp-1.min.js (он не был
    захвачен) — рабочий вариант для логина, но не гарантированный железно.
    Если логин начнёт падать именно на этом поле — поймай реальный device_id
    через DevTools на chat.deepseek.com (Network → users/login → payload)
    и передай его в DeepSeek(device_id=...).
    """
    blob = {
        "version": "1.0.0",
        "fingerprint": secrets.token_hex(16),
        "screen": "1920x1080",
        "timezone": -180,
        "platform": "Win32",
    }
    return base64.b64encode(json.dumps(blob, separators=(",", ":")).encode()).decode()


def login(email, password, device_id=None):
    """Полный цикл логина: решает WAF challenge, шлёт POST /users/login,
    возвращает bearer token (строка)."""
    waf_cookie = solve_waf_token()
    device_id = device_id or generate_device_id()

    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/sign_in",
        "user-agent": UA,
        "x-app-version": "2.0.0",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-locale": "en_US",
        "x-client-platform": "web",
        "x-client-timezone-offset": "10800",
        "x-client-version": "2.0.0",
        "cookie": waf_cookie,
    }
    payload = {
        "email": email,
        "mobile": "",
        "password": password,
        "area_code": "",
        "device_id": device_id,
        "os": "web",
    }

    r = requests.post(f"{BASE}/users/login", headers=headers, json=payload, timeout=30)
    try:
        data = r.json()
    except ValueError:
        raise AuthError(f"Логин вернул не-JSON ответ (HTTP {r.status_code}): {r.text[:300]}")

    if data.get("code") != 0:
        raise AuthError(f"Логин не удался: {data.get('msg') or data}")

    biz = data["data"]["biz_data"]
    if biz.get("biz_code") not in (0, None):
        raise AuthError(f"Логин не удался: {biz.get('msg', biz)}")

    return biz["user"]["token"]


def register(email, password, get_code_fn=None, gmail_app_password=None,
             headless=True, code_wait_timeout=180, proxy=None):
    """Полностью автоматическая регистрация DeepSeek.

    Flow:
      1. Спавним headless Chrome (browser_captcha.py)
      2. Открываем chat.deepseek.com/sign_up
      3. Заполняем email → сайт САМ вызывает hCaptcha invisible
         + create_guest_challenge (PoW) + create_email_verification_code
      4. Ждём код на Gmail (IMAP) — по alias'у в To
      5. Заполняем password + confirm + code → клик Register
      6. Извлекаем Bearer token из ответа

    Все шаги в браузере — сайт сам решает и hCaptcha, и PoW, и подписи. Мы
    только заполняем поля и извлекаем результат — без реверса чего-либо.

    email: полный адрес, например 'potapenkofil+claudereg@gmail.com'.
           Gmail plus-alias — DeepSeek видит как отдельный аккаунт,
           письмо приходит на 'potapenkofil@gmail.com'.
    password: пароль нового аккаунта.
    get_code_fn: callable(email, timeout=...) -> str. Если None — используется
                 Gmail IMAP автоматически (нужен gmail_app_password).
    gmail_app_password: 16-символьный Gmail App Password. Если None, берётся
                       из env GMAIL_APP_PASSWORD.
                       См. https://myaccount.google.com/apppasswords
    headless: True — окно скрыто.
    code_wait_timeout: сколько секунд ждать письмо (default 3 мин).

    Возвращает Bearer token (str). Кидает AuthError при ошибке.
    """
    from .browser_captcha import do_signup

    if get_code_fn is None:
        from .gmail_reader import make_code_getter
        get_code_fn = make_code_getter(app_password=gmail_app_password)

    result = do_signup(
        email=email, password=password, get_code_fn=get_code_fn,
        headless=headless, code_wait_timeout=code_wait_timeout,
        proxy=proxy,
    )
    return result["token"]
