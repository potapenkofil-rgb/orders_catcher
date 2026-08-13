class QwenError(Exception):
    """Базовая ошибка библиотеки."""


class WafCaptchaError(QwenError):
    """Aliyun WAF вернул HTML-капчу вместо JSON. html_body — сырой HTML для анализа/CapSolver."""

    def __init__(self, html_body=""):
        self.html_body = html_body
        super().__init__("Aliyun WAF slider captcha")


class AuthError(QwenError):
    """Не удалось залогиниться / обновить сессию (неверный пароль, протухшие cookie без credentials)."""


class ApiError(QwenError):
    """Qwen API вернул ошибку (не 2xx)."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class FileUploadError(QwenError):
    """Не удалось загрузить файл, либо дождаться, пока Qwen закончит его разбор."""
