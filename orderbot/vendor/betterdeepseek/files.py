"""Нормализация входных данных перед загрузкой файла на DeepSeek.

Принимает то, что реально под рукой у вызывающего кода (путь на диске, bytes,
скачанные из Telegram/HTTP, открытый файловый объект) и приводит к единому
виду (filename, data: bytes, content_type) для client.upload_file().
"""

import mimetypes
import os


def _is_single_file_tuple(x):
    return (isinstance(x, tuple) and 2 <= len(x) <= 3
            and isinstance(x[0], str) and isinstance(x[1], (bytes, bytearray)))


def normalize_input(file, filename=None, content_type=None):
    """file — путь (str/os.PathLike), bytes, (filename, bytes[, content_type])
    или файловый объект с .read(). Возвращает (filename, data, content_type)."""
    if _is_single_file_tuple(file):
        filename = filename or file[0]
        content_type = content_type or (file[2] if len(file) == 3 else None)
        file = file[1]

    if isinstance(file, (str, os.PathLike)):
        path = os.fspath(file)
        with open(path, "rb") as f:
            data = f.read()
        filename = filename or os.path.basename(path)
    elif isinstance(file, (bytes, bytearray)):
        data = bytes(file)
        filename = filename or "file.bin"
    elif hasattr(file, "read"):
        data = file.read()
        name = getattr(file, "name", None)
        filename = filename or (os.path.basename(name) if name else "file.bin")
    else:
        raise TypeError(
            f"Не знаю, как прочитать файл из {type(file)!r} — передай путь, bytes, "
            "(filename, bytes[, content_type]) или файловый объект"
        )

    if content_type is None:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    return filename, data, content_type


def as_list(files):
    """files — один файл или список файлов. Один file-tuple (filename, bytes)
    остаётся одним файлом, а не парой файлов."""
    if files is None:
        return []
    if isinstance(files, list):
        return files
    return [files]
