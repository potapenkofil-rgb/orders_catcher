"""NumPy-векторизованная версия pow_hash.solve_pow — та же самая хэш-функция,
просто считает сотни/тысячи кандидатов параллельно вместо одного за раз.

Чистый Python (pow_hash.py) даёт ~800-1000 хэшей/сек — при реальной сложности
challenge (десятки-сотни тысяч) это означает десятки-сотни секунд на одно
сообщение, неприемлемо медленно. Векторизация по батчу однотипных (одной
длины в десятичной записи) кандидатов даёt кратный прирост, потому что Keccak
внутри — это чисто поразрядные операции (xor/and/shift), которые NumPy умеет
применять сразу ко всему батчу состояний одной инструкцией.

Корректность проверена сравнением с pow_hash.py (который сам сверен с эталонной
JS-реализацией) на большом наборе значений — см. verify_pow_hash_fast.py.
"""

import numpy as np

try:
    from .pow_hash import DeepSeekHash, RC, RHO_OFFSETS, RHO_ROTATIONS
except ImportError:  # запущено напрямую, не как часть пакета
    from pow_hash import DeepSeekHash, RC, RHO_OFFSETS, RHO_ROTATIONS

U32 = np.uint32


def _vshl(x, n):
    return (x << U32(n & 31))


def _vushr(x, n):
    return (x >> U32(n & 31))


def _theta_batch(A, C, D, W):
    for t in range(5):
        n, i, o, f, s = 2 * t, (t + 5) * 2, (t + 10) * 2, (t + 15) * 2, (t + 20) * 2
        C[:, n] = A[:, n] ^ A[:, i] ^ A[:, o] ^ A[:, f] ^ A[:, s]
        C[:, n + 1] = A[:, n + 1] ^ A[:, i + 1] ^ A[:, o + 1] ^ A[:, f + 1] ^ A[:, s + 1]
    for t in range(5):
        idx = (t + 1) % 5
        o, f = C[:, 2 * idx].copy(), C[:, 2 * idx + 1].copy()
        w0 = _vshl(o, 1) | _vushr(f, 31)
        w1 = _vshl(f, 1) | _vushr(o, 31)
        prev = (t + 4) % 5
        d0 = C[:, prev * 2] ^ w0
        d1 = C[:, prev * 2 + 1] ^ w1
        D[:, 2 * t], D[:, 2 * t + 1] = d0, d1
        W[:, 0], W[:, 1] = w0, w1
        for r in range(0, 25, 5):
            A[:, (r + t) * 2] ^= d0
            A[:, (r + t) * 2 + 1] ^= d1


def _rho_pi_batch(A, C, W):
    W[:, 0], W[:, 1] = A[:, 2].copy(), A[:, 3].copy()
    for i in range(24):
        t, a = RHO_OFFSETS[i], RHO_ROTATIONS[i]
        C[:, 0], C[:, 1] = A[:, 2 * t].copy(), A[:, 2 * t + 1].copy()
        o, f = W[:, 0].copy(), W[:, 1].copy()
        u = 32 - a
        s = 0 if a < 32 else 1
        val_s = _vshl(o, a) | _vushr(f, u)
        val_s1 = _vshl(f, a) | _vushr(o, u)
        if s == 0:
            W[:, 0], W[:, 1] = val_s, val_s1
        else:
            W[:, 1], W[:, 0] = val_s, val_s1
        A[:, 2 * t], A[:, 2 * t + 1] = W[:, 0].copy(), W[:, 1].copy()
        W[:, 0], W[:, 1] = C[:, 0].copy(), C[:, 1].copy()


def _chi_batch(A, C):
    for t in range(0, 25, 5):
        for n in range(5):
            si, di = 2 * (t + n), 2 * n
            C[:, di], C[:, di + 1] = A[:, si].copy(), A[:, si + 1].copy()
        for n in range(5):
            i = (t + n) * 2
            o = ((n + 1) % 5) * 2
            f = ((n + 2) % 5) * 2
            A[:, i] ^= (~C[:, o]) & C[:, f]
            A[:, i + 1] ^= (~C[:, o + 1]) & C[:, f + 1]


def _iota_batch(A, i):
    n = 2 * i
    A[:, 0] ^= U32(RC[n])
    A[:, 1] ^= U32(RC[n + 1])


def _keccak_f_batch(state, C, D, W):
    for i in range(1, 24):
        _theta_batch(state, C, D, W)
        _rho_pi_batch(state, C, W)
        _chi_batch(state, C)
        _iota_batch(state, i)
    C[:] = 0
    D[:] = 0
    W[:] = 0


def _xor_in_batch(data, state):
    """data: (batch, u) uint8, state: (batch, 50) uint32. u должно делиться на 8.

    Байты собираются в 32-битные слова вручную (как в оригинале), а не через
    data.view(np.uint32) — так результат не зависит от endianness платформы.
    """
    d = data.astype(np.uint32)
    for r in range(0, data.shape[1], 8):
        n = r // 4
        hi = (d[:, r + 7] << 24) | (d[:, r + 6] << 16) | (d[:, r + 5] << 8) | d[:, r + 4]
        lo = (d[:, r + 3] << 24) | (d[:, r + 2] << 16) | (d[:, r + 1] << 8) | d[:, r]
        state[:, n] ^= hi.astype(np.uint32)
        state[:, n + 1] ^= lo.astype(np.uint32)


def _squeeze_batch(state, queue, queue_offset, u, s, C, D, W):
    """Возвращает (batch, s) uint8 — digest каждой строки батча."""
    batch = state.shape[0]
    q = queue.copy()
    q[:, queue_offset:] = 0
    q[:, queue_offset] |= 6
    q[:, u - 1] |= 128
    st = state.copy()
    _xor_in_batch(q, st)
    out = np.zeros((batch, s), dtype=np.uint8)
    for t in range(0, s, u):
        _keccak_f_batch(st, C, D, W)
        length = min(u, s - t)
        for r in range(0, length, 8):
            n = r // 4
            w1, w0 = st[:, n + 1], st[:, n]
            out[:, t + r] = (w1 & 0xFF).astype(np.uint8)
            out[:, t + r + 1] = ((w1 >> 8) & 0xFF).astype(np.uint8)
            out[:, t + r + 2] = ((w1 >> 16) & 0xFF).astype(np.uint8)
            out[:, t + r + 3] = ((w1 >> 24) & 0xFF).astype(np.uint8)
            out[:, t + r + 4] = (w0 & 0xFF).astype(np.uint8)
            out[:, t + r + 5] = ((w0 >> 8) & 0xFF).astype(np.uint8)
            out[:, t + r + 6] = ((w0 >> 16) & 0xFF).astype(np.uint8)
            out[:, t + r + 7] = ((w0 >> 24) & 0xFF).astype(np.uint8)
    return out


def _digest_batch(base_sponge, suffix_bytes_matrix):
    """base_sponge — _Sponge после absorb(prefix) (общий для всех кандидатов).
    suffix_bytes_matrix — (batch, L) uint8, L одинаково для всех строк батча.
    Возвращает (batch, 32) uint8 — по одному digest на строку."""
    batch, L = suffix_bytes_matrix.shape
    u = base_sponge.u
    s = base_sponge.s

    state = np.tile(np.array(base_sponge.state, dtype=np.uint32), (batch, 1))
    queue = np.tile(np.frombuffer(bytes(base_sponge.queue), dtype=np.uint8), (batch, 1)).copy()
    queue_offset = base_sponge.queue_offset

    C = np.zeros((batch, 10), dtype=np.uint32)
    D = np.zeros((batch, 10), dtype=np.uint32)
    W = np.zeros((batch, 2), dtype=np.uint32)

    for col in range(L):
        queue[:, queue_offset] = suffix_bytes_matrix[:, col]
        queue_offset += 1
        if queue_offset >= u:
            _xor_in_batch(queue, state)
            _keccak_f_batch(state, C, D, W)
            queue_offset = 0

    return _squeeze_batch(state, queue, queue_offset, u, s, C, D, W)


def solve_pow_fast(challenge_hex, salt, difficulty, expire_at, batch_size=4096):
    """То же самое, что pow_hash.solve_pow(), но векторизовано по NumPy —
    на порядки быстрее для больших difficulty. Возвращает найденный i или None.

    batch_size=4096 — эмпирически близко к оптимуму (~49k hash/s на тестовой
    машине), см. verify_pow_hash_fast.py. Меньше — накладные расходы NumPy на
    вызов доминируют, больше — не даёт заметного прироста.
    """
    prefix = f"{salt}_{expire_at}_"
    base_sponge = DeepSeekHash().update(prefix)._sponge
    challenge_bytes = bytes.fromhex(challenge_hex)

    i = 0
    while i < difficulty:
        length = len(str(i))
        upper = min(difficulty, 10 ** length)
        while i < upper:
            end = min(upper, i + batch_size)
            candidates = list(range(i, end))
            suffix_matrix = np.array(
                [[b for b in str(c).encode("ascii")] for c in candidates],
                dtype=np.uint8,
            )
            digests = _digest_batch(base_sponge, suffix_matrix)
            for row_idx, c in enumerate(candidates):
                if bytes(digests[row_idx]) == challenge_bytes:
                    return c
            i = end
    return None
