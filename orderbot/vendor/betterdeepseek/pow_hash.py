"""Чистый Python порт кастомного Keccak-варианта ("DeepSeekHashV1") из
js/pow_solver.js — 1:1 трансляция, без использования JS-движка.

Это НЕ стандартный SHA3-256 (проверено эмпирически — выход не совпадает с
hashlib.sha3_256 при тех же входных данных), несмотря на то, что параметры
(capacity=256, rate=136 байт, digest=32 байта, паддинг 0x06) совпадают с
SHA3-256. Похоже на намеренно урезанный вариант: цикл раундов Keccak-f в
оригинале начинается с i=1, а не i=0 — то есть 23 раунда вместо стандартных 24.

Корректность порта проверена сравнением с эталонной JS-реализацией на наборе
тестовых векторов, см. verify_pow_hash.py.
"""

MASK32 = 0xFFFFFFFF


def _shl(x, n):
    """JS `x << n` — величина сдвига берётся по модулю 32."""
    return (x << (n & 31)) & MASK32


def _ushr(x, n):
    """JS `x >>> n` — беззнаковый сдвиг, величина по модулю 32."""
    return (x & MASK32) >> (n & 31)


RC = [
    0, 1, 0, 32898, 0x80000000, 32906, 0x80000000, 0x80008000,
    0, 32907, 0, 0x80000001, 0x80000000, 0x80008081, 0x80000000, 32777,
    0, 138, 0, 136, 0, 0x80008009, 0, 0x8000000a,
    0, 0x8000808b, 0x80000000, 139, 0x80000000, 32905, 0x80000000, 32771,
    0x80000000, 32770, 0x80000000, 128, 0, 32778, 0x80000000, 0x8000000a,
    0x80000000, 0x80008081, 0x80000000, 32896, 0, 0x80000001, 0x80000000, 0x80008008,
]

RHO_OFFSETS = [10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4, 15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1]
RHO_ROTATIONS = [1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14, 27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44]


def _theta(A, C, D, W):
    for t in range(5):
        n, i, o, f, s = 2 * t, (t + 5) * 2, (t + 10) * 2, (t + 15) * 2, (t + 20) * 2
        C[n] = A[n] ^ A[i] ^ A[o] ^ A[f] ^ A[s]
        C[n + 1] = A[n + 1] ^ A[i + 1] ^ A[o + 1] ^ A[f + 1] ^ A[s + 1]
    for t in range(5):
        idx = (t + 1) % 5
        W[0], W[1] = C[2 * idx], C[2 * idx + 1]
        o, f = W[0], W[1]
        W[0] = _shl(o, 1) | _ushr(f, 31)
        W[1] = _shl(f, 1) | _ushr(o, 31)
        prev = (t + 4) % 5
        D[2 * t] = C[prev * 2] ^ W[0]
        D[2 * t + 1] = C[prev * 2 + 1] ^ W[1]
        for r in range(0, 25, 5):
            A[(r + t) * 2] ^= D[2 * t]
            A[(r + t) * 2 + 1] ^= D[2 * t + 1]


def _rho_pi(A, C, W):
    W[0], W[1] = A[2], A[3]
    for i in range(24):
        t, a = RHO_OFFSETS[i], RHO_ROTATIONS[i]
        C[0], C[1] = A[2 * t], A[2 * t + 1]
        o, f = W[0], W[1]
        u = 32 - a
        s = 0 if a < 32 else 1
        val_s = _shl(o, a) | _ushr(f, u)
        val_s1 = _shl(f, a) | _ushr(o, u)
        W[s] = val_s
        W[(s + 1) % 2] = val_s1
        A[2 * t], A[2 * t + 1] = W[0], W[1]
        W[0], W[1] = C[0], C[1]


def _chi(A, C):
    for t in range(0, 25, 5):
        for n in range(5):
            si, di = 2 * (t + n), 2 * n
            C[di], C[di + 1] = A[si], A[si + 1]
        for n in range(5):
            i = (t + n) * 2
            o = ((n + 1) % 5) * 2
            f = ((n + 2) % 5) * 2
            A[i] = (A[i] ^ ((~C[o] & MASK32) & C[f])) & MASK32
            A[i + 1] = (A[i + 1] ^ ((~C[o + 1] & MASK32) & C[f + 1])) & MASK32


def _iota(A, i):
    n = 2 * i
    A[0] ^= RC[n]
    A[1] ^= RC[n + 1]


def _keccak_f(state, eC, eD, eW):
    # Оригинал начинает цикл с i=1, а не i=0 — 23 раунда, не 24. Сохраняем как есть.
    for i in range(1, 24):
        _theta(state, eC, eD, eW)
        _rho_pi(state, eC, eW)
        _chi(state, eC)
        _iota(state, i)
    for i in range(len(eC)):
        eC[i] = 0
    for i in range(len(eD)):
        eD[i] = 0
    eW[0] = eW[1] = 0


def _xor_in(data, e):
    for r in range(0, len(data), 8):
        n = r // 4
        e[n] ^= (data[r + 7] << 24 | data[r + 6] << 16 | data[r + 5] << 8 | data[r + 4])
        e[n + 1] ^= (data[r + 3] << 24 | data[r + 2] << 16 | data[r + 1] << 8 | data[r])
        e[n] &= MASK32
        e[n + 1] &= MASK32


def _copy_out(state, buf, offset, length):
    for r in range(0, length, 8):
        n = r // 4
        w1, w0 = state[n + 1], state[n]
        buf[offset + r] = w1 & 0xFF
        buf[offset + r + 1] = (w1 >> 8) & 0xFF
        buf[offset + r + 2] = (w1 >> 16) & 0xFF
        buf[offset + r + 3] = (w1 >> 24) & 0xFF
        buf[offset + r + 4] = w0 & 0xFF
        buf[offset + r + 5] = (w0 >> 8) & 0xFF
        buf[offset + r + 6] = (w0 >> 16) & 0xFF
        buf[offset + r + 7] = (w0 >> 24) & 0xFF


class _Sponge:
    def __init__(self, capacity):
        self.capacity = capacity
        self.s = capacity // 8
        self.u = 200 - capacity // 4
        self.eC = [0] * 10
        self.eD = [0] * 10
        self.eW = [0] * 2
        self.state = [0] * 50
        self.queue = bytearray(self.u)
        self.queue_offset = 0

    def absorb(self, data: bytes):
        for byte in data:
            self.queue[self.queue_offset] = byte
            self.queue_offset += 1
            if self.queue_offset >= self.u:
                _xor_in(bytes(self.queue), self.state)
                _keccak_f(self.state, self.eC, self.eD, self.eW)
                self.queue_offset = 0
        return self

    def squeeze(self, padding):
        buf = bytearray(self.s)
        q = bytearray(self.queue)
        for i in range(self.queue_offset, len(q)):
            q[i] = 0
        q[self.queue_offset] |= padding
        q[self.u - 1] |= 128
        st = list(self.state)
        _xor_in(bytes(q), st)
        for t in range(0, len(buf), self.u):
            _keccak_f(st, self.eC, self.eD, self.eW)
            _copy_out(st, buf, t, min(self.u, len(buf) - t))
        return bytes(buf)

    def copy(self):
        c = _Sponge(self.capacity)
        c.queue = bytearray(self.queue)
        c.state = list(self.state)
        c.queue_offset = self.queue_offset
        return c


class DeepSeekHash:
    """Порт js/pow_solver.js DeepSeekHash — не путать со стандартным SHA3."""

    def __init__(self):
        self._sponge = _Sponge(256)

    def update(self, s: str):
        self._sponge.absorb(s.encode("utf-8"))
        return self

    def digest_hex(self):
        return self._sponge.squeeze(6).hex()

    def copy(self):
        h = DeepSeekHash()
        h._sponge = self._sponge.copy()
        return h


def solve_pow(challenge_hex, salt, difficulty, expire_at):
    """Перебор i в [0, difficulty) — то же самое, что js/pow_solver.js solvePow()."""
    prefix = f"{salt}_{expire_at}_"
    base = DeepSeekHash().update(prefix)
    for i in range(difficulty):
        if base.copy().update(str(i)).digest_hex() == challenge_hex:
            return i
    return None
