"""Дедупликация сообщений.

Одно и то же объявление часто разлетается по десяткам чатов (иногда с мелкими
правками). Ловим два случая:

1. Точный дубль — sha1 от нормализованного текста.
2. Почти-дубль — simhash + расстояние Хэмминга <= порога.

Для быстрого поиска почти-дублей simhash режется на 4 полосы по 16 бит.
Принцип Дирихле: если расстояние <= 3, хотя бы одна полоса совпадёт точно,
поэтому кандидатов ищем по полосам, а не линейным перебором.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque

from .db import Database

_URL_RE = re.compile(r"https?://\S+|t\.me/\S+|@[\w\d_]{4,}")
_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d+")

BANDS = 4
BAND_BITS = 16
BAND_MASK = (1 << BAND_BITS) - 1

# На коротких текстах simhash шумит: одно вставленное слово даёт расстояние 16+,
# то есть два разных коротких сообщения легко схлопнутся в «дубль».
# Поэтому короткие сравниваем только по точному хешу.
MIN_WORDS_FOR_SIMHASH = 15


def normalize(text: str) -> str:
    """Приводим текст к виду, устойчивому к косметическим правкам."""
    text = (text or "").lower()
    text = _URL_RE.sub(" ", text)      # ссылки и @упоминания меняются от репоста к репосту
    text = _DIGIT_RE.sub("0", text)    # цены/телефоны не должны ломать сравнение
    text = _NON_WORD_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text)
    return text.strip()


def text_hash(norm: str) -> str:
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def simhash(norm: str, bits: int = 64) -> int:
    """Классический simhash по 2-словным шинглам."""
    words = norm.split()
    if not words:
        return 0
    if len(words) == 1:
        shingles = words
    else:
        shingles = [f"{a} {b}" for a, b in zip(words, words[1:])]

    vector = [0] * bits
    for shingle in shingles:
        digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for i in range(bits):
            vector[i] += 1 if (value >> i) & 1 else -1

    result = 0
    for i in range(bits):
        if vector[i] > 0:
            result |= 1 << i
    return result


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def stable_simhash(norm: str) -> int:
    """simhash только для текстов, где он осмыслен. Иначе 0 — «не сравнивать»."""
    if len(norm.split()) < MIN_WORDS_FOR_SIMHASH:
        return 0
    return simhash(norm)


class DedupIndex:
    """Индекс уже виденных сообщений (в памяти + персист в SQLite)."""

    def __init__(self, db: Database, ttl_days: int = 7, max_hamming: int = 4,
                 memory_limit: int = 30000):
        self.db = db
        self.ttl_days = ttl_days
        self.max_hamming = max_hamming
        self._hashes: set[str] = set()
        self._order: deque[str] = deque()
        self._bands: list[dict[int, list[int]]] = [dict() for _ in range(BANDS)]
        self._memory_limit = memory_limit

    async def load(self) -> int:
        """Поднимает индекс из базы при старте."""
        rows = await self.db.seen_load(self.ttl_days)
        for text_h, sim in rows[-self._memory_limit:]:
            self._remember(text_h, sim)
        return len(self._hashes)

    def _remember(self, text_h: str, sim: int) -> None:
        if text_h in self._hashes:
            return
        self._hashes.add(text_h)
        self._order.append(text_h)
        if sim:
            for band in range(BANDS):
                key = (sim >> (band * BAND_BITS)) & BAND_MASK
                self._bands[band].setdefault(key, []).append(sim)
        # Грубая защита от разрастания памяти: полосы чистим целиком при переполнении.
        if len(self._order) > self._memory_limit:
            for _ in range(self._memory_limit // 5):
                if not self._order:
                    break
                self._hashes.discard(self._order.popleft())
            self._bands = [dict() for _ in range(BANDS)]

    def is_duplicate(self, text: str) -> bool:
        """Только проверка, без запоминания."""
        norm = normalize(text)
        if not norm:
            return False
        if text_hash(norm) in self._hashes:
            return True
        return self._near_duplicate(stable_simhash(norm))

    def _near_duplicate(self, sim: int) -> bool:
        if not sim or self.max_hamming <= 0:
            return False
        for band in range(BANDS):
            key = (sim >> (band * BAND_BITS)) & BAND_MASK
            for other in self._bands[band].get(key, ()):
                if hamming(sim, other) <= self.max_hamming:
                    return True
        return False

    async def check_and_add(self, text: str) -> bool:
        """True — сообщение новое (и оно тут же запоминается).

        False — уже видели такое или почти такое.
        """
        norm = normalize(text)
        if not norm:
            return False

        text_h = text_hash(norm)
        if text_h in self._hashes:
            return False

        sim = stable_simhash(norm)
        if self._near_duplicate(sim):
            # Запоминаем и вариацию тоже — иначе цепочка мелких правок пролезет.
            self._remember(text_h, sim)
            await self.db.seen_add(text_h, sim)
            return False

        self._remember(text_h, sim)
        await self.db.seen_add(text_h, sim)
        return True

    async def cleanup(self) -> int:
        return await self.db.seen_cleanup(self.ttl_days)

    @property
    def size(self) -> int:
        return len(self._hashes)
