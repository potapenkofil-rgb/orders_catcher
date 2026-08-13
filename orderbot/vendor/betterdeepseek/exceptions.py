class DeepSeekError(Exception):
    """Базовая ошибка библиотеки."""


class AuthError(DeepSeekError):
    """Не удалось получить/обновить bearer token (логин, WAF, протухший токен без учётных данных)."""


class WafSolveError(DeepSeekError):
    """Не удалось решить AWS WAF challenge (node недоступен, изменился challenge.js и т.п.)."""


class PowSolveError(DeepSeekError):
    """Не удалось решить proof-of-work challenge перед отправкой сообщения."""


class ApiError(DeepSeekError):
    """DeepSeek API вернул ошибку (не 2xx, либо code != 0 в теле ответа)."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class FileUploadError(DeepSeekError):
    """Не удалось загрузить файл, либо дождаться, пока DeepSeek закончит его разбор."""
