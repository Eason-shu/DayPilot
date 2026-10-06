#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 作者：EasonShu
"""
TRAE 桌面凭据的解密 —— 纯 Python 实现，零第三方依赖。
"""

import hashlib
import json

HEADER = bytes([116, 99, 5, 16, 0, 0])
LEFT_SECRET = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
    124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
    84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
    8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
RIGHT_SECRET = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
    96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
    160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
    23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])

# ---------------------------------------------------------------- AES-128 ---

_SBOX = bytes([
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16])

_INV_SBOX = bytes(256)
_INV_SBOX = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = bytes(_INV_SBOX)

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36)


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1b) & 0xff
    return a


def _mul(a, b):
    """GF(2^8) 乘法，只用于 InvMixColumns 的固定系数。"""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _expand_key(key):
    """AES-128 密钥扩展：11 组轮密钥，每组 16 字节。"""
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                       # RotWord
            temp = [_SBOX[b] for b in temp]                  # SubWord
            temp[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return [b for word in w for b in word]                   # 44*4 = 176 字节


def _add_round_key(state, rk, off):
    for i in range(16):
        state[i] ^= rk[off + i]


def _inv_sub_bytes(state):
    for i in range(16):
        state[i] = _INV_SBOX[state[i]]


def _inv_shift_rows(s):
    # 行 r 循环右移 r 位；下标按 AES 的列优先排列（s[r + 4*c]）
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] = s[r + 4 * ((c - r) % 4)]
    return out


def _inv_mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c], s[4 * c + 1], s[4 * c + 2], s[4 * c + 3]
        out[4 * c] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
        out[4 * c + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
        out[4 * c + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
        out[4 * c + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)
    return out


def _decrypt_block(block, rk):
    state = list(block)
    _add_round_key(state, rk, 160)
    for rnd in range(9, 0, -1):
        state = _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, rk, rnd * 16)
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, rk, 0)
    return bytes(state)


def _strip_pkcs7(data):
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and len(data) >= n and all(b == n for b in data[-n:]):
        return data[:-n]
    return data


def aes128_cbc_decrypt(key, iv, ciphertext, strip_padding=True):
    """AES-128-CBC 解密。key/iv 各 16 字节，ciphertext 长度必须是 16 的倍数。"""
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    if len(iv) != 16:
        raise ValueError("CBC 需要 16 字节 IV")
    if len(ciphertext) == 0 or len(ciphertext) % 16:
        raise ValueError("密文长度必须是 16 的倍数：%d" % len(ciphertext))
    rk = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i:i + 16]
        plain = _decrypt_block(block, rk)
        out.extend(bytes(a ^ b for a, b in zip(plain, prev)))
        prev = block
    return bytes(_strip_pkcs7(out) if strip_padding else out)


# ------------------------------------------------------------- TRAE 信封 ---

def _secret():
    return bytes(a ^ b for a, b in zip(LEFT_SECRET, RIGHT_SECRET))


def _sha512(data):
    return hashlib.sha512(data).digest()


def decrypt_envelope(encoded):
    """解一个 TRAE 信封（base64 字符串）→ 里面的 JSON 对象。"""
    import base64
    envelope = base64.b64decode(encoded)
    if len(envelope) <= 38 or envelope[:6] != HEADER:
        raise ValueError("不是合法的 TRAE 凭据信封（头部不匹配）")

    random_key = envelope[6:38]
    derived = _sha512(_sha512(random_key) + _secret())
    plaintext = aes128_cbc_decrypt(derived[:16], derived[16:32], envelope[38:])

    digest, payload = plaintext[:64], plaintext[64:]
    if len(digest) != 64 or _sha512(payload) != digest:
        raise ValueError("TRAE 凭据完整性校验失败（解出来对不上）")
    return json.loads(payload.decode("utf-8"))


if __name__ == "__main__":
    # 自检：FIPS-197 标准向量（AES-128）
    key = bytes(range(16))
    pt = bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
                0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff])
    ct = bytes([0x69, 0xc4, 0xe0, 0xd8, 0x6a, 0x7b, 0x04, 0x30,
                0xd8, 0xcd, 0xb7, 0x80, 0x70, 0xb4, 0xc5, 0x5a])
    got = aes128_cbc_decrypt(key, bytes(16), ct, strip_padding=False)
    print("FIPS-197 向量:", "OK" if got == pt else "失败 %s" % got.hex())
