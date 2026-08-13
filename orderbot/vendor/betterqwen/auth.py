"""Логин на chat.qwen.ai.

Qwen — форк open-webui. Аутентификация: POST /api/v2/auths/signin с телом
{email, password}, где password — **SHA-256 хеш (hex) от плейнтекста**, НЕ сам
пароль (проверено по фронту: `Nc = e => new jsSHA("SHA-256","TEXT").update(e)
.getHash("HEX")`). Плейнтекст сервер отвергает как "email or password incorrect".

Ответ signin содержит JWT `token` — его надо слать как `Authorization: Bearer
<token>` на защищённых POST-эндпоинтах (chats/new, chat/completions); одной
cookie-сессии мало (client.py ставит заголовок из extract_token()).

Анти-бот bx-ua/bx-umidtoken/bx-v на защищённые запросы считает bx_solver.py.
"""

import hashlib

from .exceptions import AuthError, WafCaptchaError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
BASE = "https://chat.qwen.ai/api/v2"
# signup — единственный auth-эндпоинт на ДРУГОЙ версии API. Подтверждено чтением
# main.js: signin идёт на дефолтный baseURL клиента (=v2, как и chats/models/
# completions), а signup явно переопределяет baseURL на "/api/v1"
# (`$A("/auths/signup",{baseURL:"/api/v1",method:"POST",data:n})`). Раньше тут
# ошибочно стоял v2 — сервер такой запрос скорее всего отклонял.
BASE_V1 = "https://chat.qwen.ai/api/v1"

__all__ = ["login", "register", "verify_email", "hash_password", "extract_token", "UA", "BASE", "BASE_V1"]


def hash_password(password):
    """SHA-256(hex) — как фронт Qwen (jsSHA SHA-256/TEXT/HEX) кодирует пароль
    перед отправкой на signin."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _check_waf_captcha(r):
    """Aliyun WAF на signup отдаёт HTML вместо JSON (HTTP 200, content-type text/html).
    Сохраняем HTML в файл при QWEN_DEBUG=1 для анализа структуры.
    """
    import os
    ct = r.headers.get("content-type", "")
    if "text/html" in ct and "aliyun_waf" in r.text[:2000]:
        if os.environ.get("QWEN_DEBUG"):
            path = "waf_captcha_dump.html"
            with open(path, "w", encoding="utf-8") as f:
                f.write(r.text)
            import sys
            sys.stderr.write(f"[qwen-debug] WAF HTML сохранён: {path}\n")
        raise WafCaptchaError(r.text)


def login(session, email, password):
    """Логинится через переданную requests.Session (куки лягут прямо в неё).

    Возвращает распарсенное тело ответа signin (dict) — оттуда client.py берёт
    JWT `token` (см. extract_token) для Authorization-заголовка.
    """
    r = session.post(
        f"{BASE}/auths/signin",
        json={"email": email, "password": hash_password(password)},
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "user-agent": UA,
        },
        timeout=30,
    )
    _check_waf_captcha(r)
    try:
        data = r.json()
    except ValueError:
        raise AuthError(f"signin вернул не-JSON ответ (HTTP {r.status_code}): {r.text[:300]}")

    if r.status_code >= 400:
        raise AuthError(f"signin -> HTTP {r.status_code}: {data if data else r.text[:300]}")

    # Qwen отвечает HTTP 200 даже на неверный логин — ошибка в теле (success:false).
    if isinstance(data, dict) and data.get("success") is False:
        err = data.get("data", {})
        raise AuthError("signin отклонён: " + str(err.get("details") or err.get("code") or data))

    return data


def register(session, name, email, password, headers=None):
    """Регистрация нового аккаунта на chat.qwen.ai.

    Payload {name,email,password} и эндпоинт /auths/signup подтверждены чтением
    main.js вживую: `rd=e=>{const t=hash(e.password);const n={...e,password:t};
    return $A("/auths/signup",{baseURL:"/api/v1",method:"POST",data:n})}`.

    Сервер ТРЕБУЕТ подтверждения email — после signup аккаунт неактивен до
    перехода по ссылке из письма. Токен в ответе signup НЕ выдаётся;
    client.py ждёт письмо на mail.tm, извлекает ссылку и вызывает verify_email().

    headers — доп. заголовки (bx-ua/bx-v/version/x-request-id/timezone).
    Возвращает распарсенное тело ответа (dict).
    """
    h = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "user-agent": UA,
    }
    if headers:
        h.update(headers)

    payload = {"name": name, "email": email, "password": hash_password(password)}
    url = f"{BASE_V1}/auths/signup"

    r = session.post(url, json=payload, headers=h, timeout=30)
    _check_waf_captcha(r)  # кидает WafCaptchaError если HTML вместо JSON

    try:
        data = r.json()
    except ValueError:
        raise AuthError(f"signup вернул не-JSON (HTTP {r.status_code}): {r.text[:300]}")

    if r.status_code >= 400:
        raise AuthError(f"signup -> HTTP {r.status_code}: {data if data else r.text[:300]}")

    if isinstance(data, dict) and data.get("success") is False:
        err = data.get("data", {})
        raise AuthError("signup отклонён: " + str(err.get("details") or err.get("code") or data))

    return data


def extract_token(data):
    """JWT из ответа signin. Qwen — форк open-webui: токен обычно в `token` или
    `data.token`. Нужен как `Authorization: Bearer <token>` на защищённых POST
    (chats/new, chat/completions) — cookie одной мало."""
    if not isinstance(data, dict):
        return ""
    for container in (data, data.get("data") if isinstance(data.get("data"), dict) else {}):
        for key in ("token", "access_token", "id_token", "jwt"):
            if container.get(key):
                return container[key]
    return ""


def verify_email(session, verify_url):
    """GET на ссылку активации из письма Qwen — активирует аккаунт.

    Qwen (open-webui-форк) обычно шлёт ссылку вида
    https://chat.qwen.ai/auth/verify?token=XXX — GET по ней активирует аккаунт
    и редиректит на главную. После этого можно логиниться через login().
    """
    r = session.get(
        verify_url,
        headers={"user-agent": UA},
        timeout=30,
        allow_redirects=True,
    )
    if r.status_code >= 400:
        raise AuthError(f"Активация email: HTTP {r.status_code}: {r.text[:300]}")
    return r
