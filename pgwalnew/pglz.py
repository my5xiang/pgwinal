"""pglz 解压（PostgreSQL PG_LZ 压缩格式，纯 Python 实现）。

对照 src/include/common/pg_lzcompress.h：
  头部：int32 rawsize（含 4 字节头自身）+ 变长 tag
  tag 高 4 位 = 类型；0 = 字面量，1..4 = 回溯引用
"""

from __future__ import annotations

import struct


def pglz_decompress(src: bytes, rawsize: int) -> bytes:
    """解压 pglz 数据（src 不含 rawsize 前缀）。"""
    out = bytearray()
    sp = 0
    n = len(src)
    while sp < n and len(out) < rawsize:
        ctrl = src[sp]
        sp += 1
        for bit in range(8):
            if sp >= n or len(out) >= rawsize:
                break
            if (ctrl >> bit) & 1:
                # 回溯引用
                if sp + 2 > n:
                    break
                length = (src[sp] >> 4) + 3
                offset = ((src[sp] & 0x0F) << 8) | src[sp + 1]
                sp += 2
                if offset == 0 or offset > len(out):
                    raise ValueError(f"pglz 非法回溯 offset={offset} out={len(out)}")
                for _ in range(length):
                    out.append(out[-offset])
            else:
                out.append(src[sp])
                sp += 1
    if len(out) != rawsize:
        raise ValueError(f"pglz 尺寸不符: {len(out)} != {rawsize}")
    return bytes(out)
