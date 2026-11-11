"""GameSpy EncTypeX stream cipher, used to encrypt server-browser replies.

Algorithm as documented by Luigi Auriemma (enctypex_decoder). Only the
encrypt direction is needed by the server; decrypt is provided for tests.
"""

import os


class _State:
    def __init__(self):
        self.k = bytearray(261)


def _func5(st, cnt, ident, n):
    """Pick a swap index; returns (index, n1, n2)."""
    k = st.k
    n1, n2 = n
    if cnt == 0:
        return 0, n1, n2
    mask = 1
    while mask < cnt:
        mask = (mask << 1) + 1
    i = 0
    while True:
        n1 = k[n1 & 0xFF] + ident[n2]
        n2 += 1
        if n2 >= len(ident):
            n2 = 0
            n1 += len(ident)
        tmp = n1 & mask
        i += 1
        if i > 11:
            tmp %= cnt
        if tmp <= cnt:
            return tmp, n1, n2


def _func4(st, ident):
    k = st.k
    for i in range(256):
        k[i] = i
    n1 = n2 = 0
    for i in range(255, -1, -1):
        t, n1, n2 = _func5(st, i, ident, (n1, n2))
        k[i], k[t] = k[t], k[i]
    k[256], k[257], k[258], k[259] = k[1], k[3], k[5], k[7]
    k[260] = k[n1 & 0xFF]


def _step(st, d, encrypt):
    k = st.k
    a = k[256]
    b = k[257]
    c = k[a]
    k[256] = (a + 1) & 0xFF
    k[257] = (b + c) & 0xFF
    a = k[260]
    b = k[k[257]]
    c = k[a]
    k[a] = b
    k[k[257]] = k[k[259]]
    k[k[259]] = k[k[256]]
    k[k[256]] = c
    b = (k[c] + k[258]) & 0xFF
    k[258] = b
    a = b
    cc = (k[k[257]] + k[k[259]]) & 0xFF
    cc = (k[k[260]] + cc) & 0xFF
    b = k[cc]
    a = (k[a] + k[k[256]]) & 0xFF
    out = k[b] ^ k[a] ^ d
    if encrypt:
        k[260] = out
        k[259] = d
    else:
        k[260] = d
        k[259] = out
    return out


def _init(key: bytes, validate: bytes, header: bytes):
    """Derive the cipher state from game key, client challenge and the
    random header bytes."""
    st = _State()
    val = bytearray(validate[:8].ljust(8, b"\x00"))
    for i, h in enumerate(header):
        val[(key[i % len(key)] * i) & 7] ^= val[i & 7] ^ h
    _func4(st, val)
    return st


def encrypt(key: str, validate: bytes, data: bytes, rand=os.urandom) -> bytes:
    key_b = key.encode("latin-1")
    header_len = 7
    tmp_len = 20
    hdr = bytearray(rand(tmp_len))
    hdr[0] = (header_len - 2) ^ 0xEC
    hdr[1] = 0
    hdr[2] = 0
    hdr[header_len - 1] = (tmp_len - header_len) ^ 0xEA
    st = _init(key_b, validate, hdr[header_len:])
    out = bytes(_step(st, b, True) for b in data)
    return bytes(hdr) + out


def decrypt(key: str, validate: bytes, data: bytes) -> bytes:
    key_b = key.encode("latin-1")
    header_len = (data[0] ^ 0xEC) + 2
    start = data[header_len - 1] ^ 0xEA
    st = _init(key_b, validate, data[header_len:header_len + start])
    body = data[header_len + start:]
    return bytes(_step(st, b, False) for b in body)
