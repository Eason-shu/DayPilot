#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EasonShu
"""ECDSA P-256 签名 —— 纯 Python 实现，只为 TRAE 凭据续期服务。

续期接口要求用设备私钥对请求载荷签名，私钥以 PEM 形式存放。标准库没有椭圆
曲线签名能力，故在此实现最小可用子集：

  load_private_scalar(pem)   解析 PEM 私钥并校验曲线
  curve_matches(pem)         只判断曲线是否为 P-256，不暴露私钥内容
  sign(payload, pem, k=None) 返回 DER 编码的签名
  sign_b64(payload, pem)     返回 base64 签名，便于直接放进请求体

只实现签名（不做验签），随机数 k 用 os.urandom 拒绝采样生成。
"""

import hashlib
import os

# ---- NIST P-256（prime256v1 / secp256r1）曲线参数 ---------------------------
P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = 0xffffffff00000001000000000000000000000000fffffffffffffffffffffffc  # -3 mod p
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
GX = 0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296
GY = 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551

P256_OID = bytes([0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07])


# ---- 曲线运算 ---------------------------------------------------------------
def _inv_mod(x, m):
    """模逆（扩展欧几里得）。"""
    if x == 0:
        raise ZeroDivisionError("0 没有模逆")
    lm, hm = 1, 0
    low, high = x % m, m
    while low > 1:
        r = high // low
        nm, new = hm - lm * r, high - low * r
        lm, low, hm, high = nm, new, lm, low
    return lm % m


def _is_on_curve(pt):
    if pt is None:
        return False
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def _point_double(pt):
    """仿射坐标倍点。a = -3，所以斜率用 3(x^2-1)/(2y)。"""
    x, y = pt
    if y == 0:
        return None
    lam = (3 * (x * x - 1) * _inv_mod(2 * y, P)) % P
    x3 = (lam * lam - 2 * x) % P
    return (x3, (lam * (x - x3) - y) % P)


def _point_add(pt1, pt2):
    """仿射坐标点加。"""
    if pt1 is None:
        return pt2
    if pt2 is None:
        return pt1
    x1, y1 = pt1
    x2, y2 = pt2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None                       # 互为逆元 -> 无穷远点
        return _point_double(pt1)
    lam = ((y2 - y1) * _inv_mod(x2 - x1, P)) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def scalar_mult(k, pt=(GX, GY)):
    """k * pt，双倍-相加。续期一次只调一次，不做常数时间优化（本地脚本）。"""
    if not _is_on_curve(pt):
        raise ValueError("点不在 P-256 曲线上")
    if k % N == 0 or k < 0:
        raise ValueError("标量超出范围")
    result = None
    addend = pt
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_double(addend)
        k >>= 1
    return result


# ---- DER 工具 ---------------------------------------------------------------
def _der_read_len(data, i):
    first = data[i]
    i += 1
    if first < 0x80:
        return first, i
    count = first & 0x7f
    if count == 0 or count > 4:
        raise ValueError("不支持的 DER 长度格式")
    length = int.from_bytes(data[i:i + count], "big")
    return length, i + count


def _der_walk(data, start=0, end=None):
    """极简 DER 遍历，产出 (tag, 值bytes)。够解析 PKCS#8 / ECPrivateKey 用。"""
    end = len(data) if end is None else end
    i = start
    out = []
    while i < end:
        tag = data[i]
        length, j = _der_read_len(data, i + 1)
        value = data[j:j + length]
        out.append((tag, value))
        i = j + length
    return out


def _der_integer(value):
    b = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    if b[0] & 0x80:
        b = b"\x00" + b          # 正数需要补前导 0
    return b"\x02" + _der_len(len(b)) + b


def _der_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _der_signature(r, s):
    """SEQUENCE { INTEGER r, INTEGER s } —— ECDSA-Sig-Value。"""
    body = _der_integer(r) + _der_integer(s)
    return b"\x30" + _der_len(len(body)) + body


# ---- PEM 解析 ---------------------------------------------------------------
def _der_seq_body(data):
    """给一串以 SEQUENCE 开头的 DER，返回它的内容（去掉 tag+长度头）。"""
    if not data or data[0] != 0x30:
        raise ValueError("不是 DER SEQUENCE")
    length, i = _der_read_len(data, 1)
    body = data[i:i + length]
    if len(body) != length:
        raise ValueError("DER 长度越界")
    return body


def load_private_scalar(pem):
    """从 PKCS#8 的 EC 私钥 PEM 里取出私钥标量 d。

    PKCS#8 是三层套娃：
        SEQUENCE
          INTEGER 版本(0)
          SEQUENCE 算法标识（含 P-256 的 OID）
          OCTET STRING   <- 这里面又是 ECPrivateKey 的 SEQUENCE
              SEQUENCE
                INTEGER 1
                OCTET STRING 32字节私钥  <- 要的就是它
                [0] 曲线参数（可选）
                [1] 公钥（可选）
    所以拿到外层 OCTET STRING 之后必须再剥一层 SEQUENCE。
    """
    import base64
    text = "".join(line.strip() for line in str(pem).splitlines()
                   if line.strip() and not line.strip().startswith("-----"))
    der = base64.b64decode(text)

    top = _der_walk(der)
    if not top or top[0][0] != 0x30:
        raise ValueError("不是 PKCS#8 私钥（顶层不是 SEQUENCE）")

    # 第一层：找 privateKey 那个 OCTET STRING
    inner = None
    for tag, value in _der_walk(top[0][1]):
        if tag == 0x04:
            inner = value
            break
    if inner is None:
        raise ValueError("PKCS#8 里没找到 privateKey（OCTET STRING）")

    # 第二层：OCTET STRING 里是 ECPrivateKey 的 SEQUENCE，得再剥一次
    fields = _der_walk(_der_seq_body(inner) if inner[:1] == b"\x30" else inner)
    for tag, value in fields:
        if tag == 0x04 and len(value) == 32:
            return int.from_bytes(value, "big")
    raise ValueError("没找到 32 字节的 EC 私钥（只支持 P-256）")


def curve_matches(pem):
    """确认这把钥匙是 P-256 —— 不是的话签名算法对不上。"""
    import base64
    text = "".join(l.strip() for l in str(pem).splitlines()
                   if l.strip() and not l.strip().startswith("-----"))
    return P256_OID in base64.b64decode(text)


# ---- ECDSA ------------------------------------------------------------------
def sign(payload, private_key_pem, k=None):
    """对 payload（bytes）做 ECDSA-SHA256 签名，返回 DER 签名的 base64 字符串。

    与 Node 的 crypto.sign('sha256', buf, pem).toString('base64') 等价。
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    d = load_private_scalar(private_key_pem)

    e = int.from_bytes(hashlib.sha256(payload).digest(), "big")
    z = e >> max(0, e.bit_length() - N.bit_length())   # 截断到曲线阶的位长

    while True:
        if k is None:
            k = _random_k()
        try:
            point = scalar_mult(k)
        except ZeroDivisionError:
            k = None
            continue
        r = point[0] % N
        if r == 0:
            k = None
            continue
        s = (_inv_mod(k, N) * (z + r * d)) % N
        if s != 0:
            break
        k = None
    return _der_signature(r, s).hex()


def _random_k():
    """拒绝采样出一个 [1, N-1] 的随机数，用 os.urandom。"""
    while True:
        candidate = int.from_bytes(os.urandom(32), "big")
        if 1 <= candidate < N:
            return candidate


def sign_b64(payload, private_key_pem, k=None):
    import base64
    der = bytes.fromhex(sign(payload, private_key_pem, k))
    return base64.b64encode(der).decode("ascii")
