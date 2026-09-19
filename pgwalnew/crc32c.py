"""CRC32C（Castagnoli）—— 与 PostgreSQL pg_crc32c 完全一致。

优先使用 C 扩展 crc32c（pip install crc32c），缺失时回退纯 Python 表实现。
测试向量：crc32c(b"123456789") == 0xE3069283。

WAL 记录校验规则（对照 REL_12_STABLE xlogreader.c ValidXLogRecord）：
    crc = CRC32C( body || record[0:20] )
即：先记录体（24 字节头之后的部分），再拼接记录头的前 20 字节（不含 xl_crc）。
"""

from __future__ import annotations

_POLY = 0x82F63B78  # Castagnoli 反转多项式

_TABLE: list[int] = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (_POLY if _c & 1 else 0)
    _TABLE.append(_c)
del _i, _c


def _crc32c_pure(data: bytes, crc: int = 0) -> int:
    crc ^= 0xFFFFFFFF
    tbl = _TABLE
    for b in data:
        crc = tbl[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


try:  # C 加速
    from crc32c import crc32c as _crc32c_ext  # type: ignore

    def crc32c(data: bytes) -> int:
        return _crc32c_ext(data)

except ImportError:  # 纯 Python 回退（慢，但保证可运行）
    def crc32c(data: bytes) -> int:
        return _crc32c_pure(data)


def record_crc_valid(record_bytes: bytes) -> bool:
    """校验一条完整 WAL 记录（含 24B 头）的 CRC。

    record_bytes 长度必须等于 xl_tot_len。
    """
    tot_len = int.from_bytes(record_bytes[0:4], "little")
    if tot_len != len(record_bytes) or tot_len < 24:
        return False
    body = record_bytes[24:tot_len]
    hdr20 = record_bytes[0:20]
    return crc32c(body + hdr20) == int.from_bytes(record_bytes[20:24], "little")
