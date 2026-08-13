"""Решает AWS WAF challenge на chat.deepseek.com без Node.js — вместо
Node vm.createContext используется py_mini_racer (V8, ставится через pip).

py_mini_racer умеет только Python -> JS (eval/call), обратного моста нет —
поэтому js/waf_shim.js не делает сетевые запросы сам, а кладёт их в очередь
(window.__pending), а этот файл в цикле забирает их (__drainPending),
выполняет по-настоящему через requests и результат проталкивает обратно в V8
(__resolvePending/__rejectPending), что резолвит Promise и запускает
дальнейшие .then()-цепочки challenge.js. См. комментарий в начале waf_shim.js.

Использование: как модуль (solve_waf_token()) или напрямую —
    python waf_solver.py
выведет в stdout строку вида "aws-waf-token=..." (как и js/waf_token_solver.js).
"""

import base64
import json
import os
import re
import time

import requests

from py_mini_racer import MiniRacer

try:
    from .exceptions import WafSolveError
except ImportError:  # запущено напрямую (python waf_solver.py), не как часть пакета
    from exceptions import WafSolveError

TARGET_HOST = "chat.deepseek.com"
TARGET = f"https://{TARGET_HOST}/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

_JS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")
_SHIM_PATH = os.path.join(_JS_DIR, "waf_shim.js")

DEBUG = bool(os.environ.get("WAF_DEBUG"))


def _dbg(msg):
    if DEBUG:
        import sys
        sys.stderr.write(msg if msg.endswith("\n") else msg + "\n")


def _fetch_challenge_page(session):
    r = session.get(TARGET, headers={"Accept": "text/html"}, timeout=30)
    if r.status_code != 202:
        raise WafSolveError(f"Ожидался 202 (WAF challenge), получено {r.status_code}")
    body = r.text

    props_match = re.search(r"window\.gokuProps\s*=\s*(\{[\s\S]*?\})\s*;", body)
    if not props_match:
        raise WafSolveError("gokuProps не найден в странице challenge")
    goku_props = json.loads(props_match.group(1))

    js_match = re.search(r'src="(https://[^"]+challenge\.js[^"]*)"', body)
    if not js_match:
        raise WafSolveError("challenge.js URL не найден в странице challenge")
    challenge_js_url = js_match.group(1)

    domain_match = re.search(r"window\.awsWafCookieDomainList\s*=\s*(\[[^\]]+\])", body)
    if domain_match:
        cookie_domains = json.loads(domain_match.group(1).replace("'", '"'))
    else:
        cookie_domains = [TARGET_HOST]

    return goku_props, challenge_js_url, cookie_domains


_TOKEN_RE = re.compile(r"aws-waf-token=([^;,\s]+)", re.I)


def _extract_token_from_cookies(resp):
    """Проверяет и обычный CookieJar, и сырые Set-Cookie заголовки (на случай
    нескольких Set-Cookie в одном ответе — requests может их объединить)."""
    val = resp.cookies.get("aws-waf-token")
    if val and len(val) > 10:
        return val
    raw = resp.raw.headers.get_all("Set-Cookie") if resp.raw and hasattr(resp.raw, "headers") else None
    if raw:
        for c in raw:
            m = _TOKEN_RE.search(c)
            if m and len(m.group(1)) > 10:
                return m.group(1)
    return None


class _Pump:
    """Прогоняет очередь fetch/XHR-запросов из V8 через реальный HTTP (requests),
    пока не появится токен, не истечёт таймаут или JS не кинет ошибку."""

    def __init__(self, mr, session):
        self.mr = mr
        self.session = session
        self.captured_token = None

    def _do_request(self, req):
        method = req["method"]
        url = req["url"]
        headers = dict(req.get("headers") or {})
        headers.setdefault("User-Agent", UA)
        headers.setdefault("Origin", TARGET.rstrip("/"))
        headers.setdefault("Referer", TARGET)
        body = None
        if req.get("bodyBase64"):
            body = base64.b64decode(req["bodyBase64"])
        _dbg(f"[pump] {method} {url}")
        _dbg(f"[pump]   req headers: {headers}")
        if body:
            _dbg(f"[pump]   req body: {len(body)} bytes")
        resp = self.session.request(method, url, headers=headers, data=body, timeout=20)
        _dbg(f"[pump]   -> {resp.status_code} len={len(resp.content)} body={resp.text[:300]!r}")

        token = _extract_token_from_cookies(resp)
        if token:
            _dbg(f"[pump] TOKEN via Set-Cookie: {token[:40]}...")
            self.captured_token = token

        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        return {
            "status": resp.status_code,
            "headers": resp_headers,
            "bodyBase64": base64.b64encode(resp.content).decode(),
        }

    def run(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.mr.eval("window.__runTimers()")

            pending = json.loads(self.mr.eval("window.__drainPending()"))
            did_work = bool(pending)
            for req in pending:
                try:
                    result = self._do_request(req)
                    self.mr.call("window.__resolvePending", req["id"], result["status"],
                                 json.dumps(result["headers"]), result["bodyBase64"])
                except Exception as e:  # сетевые ошибки и т.п. — пробрасываем в JS как reject
                    _dbg(f"[pump] request error: {e}")
                    self.mr.call("window.__rejectPending", req["id"], str(e))

            if self.captured_token:
                return self.captured_token

            js_token = self.mr.eval("window.__capturedToken")
            if js_token:
                return js_token

            err = self.mr.eval("window.__bootstrapError")
            if err:
                raise WafSolveError(f"challenge.js сообщил об ошибке: {err}")

            if not did_work:
                time.sleep(0.05)
        return None


def solve_waf_token(timeout=45):
    """Решает AWS WAF challenge, возвращает cookie-строку 'aws-waf-token=...'.

    Чистый Python + py_mini_racer (V8 через pip) — Node.js не требуется.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": TARGET.rstrip("/"),
        "Referer": TARGET,
    })

    _dbg("[waf-solver-native] fetching challenge page...")
    goku_props, challenge_js_url, cookie_domains = _fetch_challenge_page(session)

    _dbg(f"[waf-solver-native] challenge.js: {challenge_js_url}")
    r = session.get(challenge_js_url, headers={"Accept": "*/*"}, timeout=30)
    if r.status_code != 200:
        raise WafSolveError(f"Не удалось скачать challenge.js: HTTP {r.status_code}")
    challenge_code = r.text
    _dbg(f"[waf-solver-native] challenge.js size: {len(challenge_code)}")

    mr = MiniRacer()

    preamble = f"""
    var __target = {json.dumps(TARGET)};
    var __targetHost = {json.dumps(TARGET_HOST)};
    var __ua = {json.dumps(UA)};
    var __gokuProps = {json.dumps(goku_props)};
    var __cookieDomains = {json.dumps(cookie_domains)};
    var __challengeJsUrl = {json.dumps(challenge_js_url)};
    var __bootstrapError = null;
    """
    mr.eval(preamble)

    with open(_SHIM_PATH, encoding="utf-8") as f:
        shim_code = f.read()
    mr.eval(shim_code)

    _dbg("[waf-solver-native] executing challenge.js...")
    try:
        mr.eval(challenge_code)
    except Exception as e:
        raise WafSolveError(f"Ошибка выполнения challenge.js: {e}")

    kickoff = """
    (function () {
        try {
            AwsWafIntegration.saveReferrer();
            AwsWafIntegration.checkForceRefresh().then(function (forceRefresh) {
                if (forceRefresh) {
                    return AwsWafIntegration.forceRefreshToken();
                }
                return AwsWafIntegration.getToken();
            }).then(function (tok) {
                if (tok && typeof tok === 'string' && tok.length > 10 && !window.__capturedToken) {
                    window.__capturedToken = 'aws-waf-token=' + tok;
                }
                if (!window.__capturedToken && AwsWafIntegration.fetch) {
                    return AwsWafIntegration.fetch(window.location.href, { method: 'GET', headers: { 'Accept': 'text/html' } });
                }
            }).catch(function (e) {
                window.__bootstrapError = String((e && e.message) || e);
            });
        } catch (e) {
            window.__bootstrapError = String((e && e.message) || e);
        }
    })();
    """
    _dbg("[waf-solver-native] running saveReferrer -> checkForceRefresh -> getToken sequence...")
    mr.eval(kickoff)

    pump = _Pump(mr, session)
    token = pump.run(timeout=timeout)
    if not token:
        raise WafSolveError("Решение WAF challenge не уложилось в таймаут")
    return token


if __name__ == "__main__":
    import sys
    try:
        cookie = solve_waf_token()
        sys.stdout.write(cookie + "\n")
    except WafSolveError as e:
        sys.stderr.write(f"[waf-solver-native] FATAL: {e}\n")
        sys.exit(1)
