"""Загрузка файла в Alibaba OSS по STS-токену — OSS V4 signature вручную.

Qwen грузит вложения не к себе на бэкенд, а прямо в Alibaba OSS (Object Storage):
  1. POST /api/v2/files/getstsToken  -> временные STS-креды + bucket/endpoint/path
  2. PUT файла в OSS с V4-подписью (этот модуль)
  3. POST /api/v2/files/parse        -> сервер разбирает файл (OCR/извлечение)

Фронт использует ali-oss JS SDK с authorizationV4:true. Здесь тот же V4-алгоритм
подписи (аналог AWS SigV4: derive signing key -> canonical request -> sign),
реализованный на stdlib (hmac/hashlib) — без зависимости oss2, в духе проекта
(«только pip install»). Однопакетный PUT; для очень больших файлов OSS-фронт
переходит на multipartUpload, здесь пока только простой put (достаточно для
типовых картинок/документов).

НЕ ПРОВЕРЕНО вживую end-to-end (нужен тестовый аккаунт + реальный файл) — если
OSS вернёт ошибку подписи, гоняй с QWEN_DEBUG=1 и присылай ответ OSS: чаще всего
дело в формате region (для V4 нужен голый "cn-hangzhou", без "oss-"/домена) или
в списке подписываемых заголовков.
"""

import datetime
import hashlib
import hmac
import sys

import requests

from .exceptions import FileUploadError

_UNSIGNED = "UNSIGNED-PAYLOAD"


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _region_of(endpoint, region):
    """Голый регион для V4 scope: "cn-hangzhou". STS может вернуть region уже
    голым, либо как "oss-cn-hangzhou" / полный домен — нормализуем."""
    r = (region or "").strip()
    if r.startswith("oss-"):
        r = r[4:]
    if not r and endpoint:
        # вытащить из endpoint вида oss-cn-hangzhou.aliyuncs.com
        host = endpoint.replace("https://", "").replace("http://", "")
        part = host.split(".")[0]
        r = part[4:] if part.startswith("oss-") else part
    return r


def put_object(sts, file_path, data, content_type, debug=False):
    """PUT данные в OSS-бакет по пути file_path с V4-подписью.

    sts — dict из getstsToken: accessKeyId, accessKeySecret, stsToken (security
    token), bucket, region, endpoint. Возвращает финальный URL объекта в OSS.
    """
    access_key_id = sts["accessKeyId"]
    access_key_secret = sts["accessKeySecret"]
    security_token = sts["stsToken"]
    bucket = sts["bucket"]
    endpoint = sts["endpoint"].replace("https://", "").replace("http://", "").rstrip("/")
    region = _region_of(endpoint, sts.get("region", ""))

    host = f"{bucket}.{endpoint}"
    url = f"https://{host}/{file_path}"

    now = datetime.datetime.now(datetime.timezone.utc)
    iso_datetime = now.strftime("%Y%m%dT%H%M%SZ")
    iso_date = now.strftime("%Y%m%d")

    payload_hash = _UNSIGNED
    canonical_uri = "/" + bucket + "/" + file_path

    # Заголовки для подписи (OSS V4): в CanonicalHeaders идут все x-oss-*,
    # content-type, а также любые доп. заголовки из AdditionalHeaders. host мы
    # подписываем дополнительно -> он и в canonical, и в additional list.
    signed = {
        "content-type": content_type,
        "host": host,
        "x-oss-content-sha256": payload_hash,
        "x-oss-date": iso_datetime,
        "x-oss-security-token": security_token,
    }
    canonical_headers = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    additional_headers = "host"  # доп. подписываемые заголовки (кроме x-oss-*/content-*)

    canonical_request = "\n".join([
        "PUT",
        canonical_uri,
        "",                       # canonical query (пусто)
        canonical_headers,        # уже с завершающим \n у каждого
        additional_headers,
        payload_hash,
    ])

    scope = f"{iso_date}/{region}/oss/aliyun_v4_request"
    string_to_sign = "\n".join([
        "OSS4-HMAC-SHA256",
        iso_datetime,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    # derive signing key
    k_date = _hmac(("aliyun_v4" + access_key_secret).encode("utf-8"), iso_date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, "oss")
    k_signing = _hmac(k_service, "aliyun_v4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        "OSS4-HMAC-SHA256 "
        f"Credential={access_key_id}/{scope},"
        f"AdditionalHeaders={additional_headers},"
        f"Signature={signature}"
    )

    headers = {
        "Authorization": authorization,
        "content-type": content_type,
        "x-oss-content-sha256": payload_hash,
        "x-oss-date": iso_datetime,
        "x-oss-security-token": security_token,
    }

    if debug:
        sys.stderr.write(f"[qwen-debug] OSS PUT {url}\n")
        sys.stderr.write(f"[qwen-debug] OSS canonical_request:\n{canonical_request}\n")
        sys.stderr.write(f"[qwen-debug] OSS authorization: {authorization[:120]}...\n")

    r = requests.put(url, data=data, headers=headers, timeout=120)
    if r.status_code >= 300:
        raise FileUploadError(f"OSS PUT -> HTTP {r.status_code}: {r.text[:400]}")
    return url
