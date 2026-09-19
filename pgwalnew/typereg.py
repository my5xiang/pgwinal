"""类型注册表：OID → (typlen, typalign, 解码器)。

typlen/typalign 对照 pg_type 系统目录（12–18 稳定）：
  typlen = -1 → varlena；-2 → cstring；>0 → 定长
  typalign: 'c'=1, 's'=2, 'i'=4, 'd'=8
"""

from __future__ import annotations

import struct
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

PG_EPOCH = datetime(2000, 1, 1)
PG_EPOCH_DATE = date(2000, 1, 1)

# ---- 特殊标记值（不可执行，需人工复核） ----


class RawValue:
    """未能按类型解码的原始值。"""

    __slots__ = ("data", "reason", "kind")

    def __init__(self, data: bytes, reason: str, kind: str = "raw"):
        self.data = data
        self.reason = reason
        self.kind = kind  # raw | external | compressed | unknown_type | decode_error

    def __repr__(self):
        return f"<RawValue {self.kind}:{len(self.data)}B {self.reason}>"


class ExternalValue:
    """TOAST 外部指针（varatt_external）。"""

    __slots__ = ("rawsize", "extsize", "valueid", "toastrelid")

    def __init__(self, rawsize, extsize, valueid, toastrelid):
        self.rawsize = rawsize
        self.extsize = extsize
        self.valueid = valueid
        self.toastrelid = toastrelid


# ---------------------------------------------------------------- 解码器


def _dec_bool(b: bytes) -> bool:
    return b[0] != 0


def _dec_int2(b: bytes) -> int:
    return struct.unpack("<h", b)[0]


def _dec_int4(b: bytes) -> int:
    return struct.unpack("<i", b)[0]


def _dec_int8(b: bytes) -> int:
    return struct.unpack("<q", b)[0]


def _dec_uint4(b: bytes) -> int:
    return struct.unpack("<I", b)[0]


def _dec_float4(b: bytes) -> float:
    return struct.unpack("<f", b)[0]


def _dec_float8(b: bytes) -> float:
    return struct.unpack("<d", b)[0]


def _dec_date(b: bytes) -> object:
    v = struct.unpack("<i", b)[0]
    if v == 0x7FFFFFFF:
        return "infinity"
    if v == -0x80000000:
        return "-infinity"
    return PG_EPOCH_DATE + timedelta(days=v)


def _dec_time(b: bytes) -> str:
    v = struct.unpack("<q", b)[0]
    secs, us = divmod(v, 1_000_000)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{us:06d}"


def _dec_timetz(b: bytes) -> str:
    v = struct.unpack("<qi", b)
    secs, us = divmod(v[0], 1_000_000)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    tz = -v[1]
    return f"{h:02d}:{m:02d}:{s:02d}.{us:06d}{tz // 3600:+03d}"


def _dec_timestamp(b: bytes) -> object:
    v = struct.unpack("<q", b)[0]
    if v == 0x7FFFFFFFFFFFFFFF:
        return "infinity"
    if v == -0x8000000000000000:
        return "-infinity"
    return PG_EPOCH + timedelta(microseconds=v)


def _dec_interval(b: bytes) -> str:
    us, days, months = struct.unpack("<qii", b)
    parts = []
    if months:
        y, m = divmod(months, 12)
        parts.append(f"{y} years {m} mons".replace(" 0 years", "").replace(" 0 mons", ""))
    if days:
        parts.append(f"{days} days")
    if us or not parts:
        secs, rem = divmod(us, 1_000_000)
        h, r = divmod(secs, 3600)
        mi, s = divmod(r, 60)
        parts.append(f"{h:02d}:{mi:02d}:{s:02d}.{abs(rem):06d}")
    return " ".join(parts)


def _dec_uuid(b: bytes) -> str:
    return str(uuid.UUID(bytes=b))


def _dec_pg_lsn(b: bytes) -> str:
    v = struct.unpack("<Q", b)[0]
    return f"{v >> 32:X}/{v & 0xFFFFFFFF:X}"


def _dec_numeric(data: bytes):
    """PG numeric（varlena 去头后的载荷）→ Decimal。

    格式对照 REL_12_STABLE numeric.c：
      uint16 header：
        (h & 0xC000)==0x0000/0x4000 → LONG：dscale=h&0x3FFF, int16 weight, digits
        (h & 0xC000)==0x8000 → SHORT：sign=0x2000, dscale=(h&0x1F80)>>7,
            weight=(h&0x0040)? (h&0x3F)-64 : (h&0x3F), digits 紧随
        (h & 0xC000)==0xC000 → NaN
      digits：int16[]，基 10000
    """
    if len(data) < 2:
        return Decimal(0)
    h = struct.unpack_from("<H", data, 0)[0]
    flag = h & 0xC000
    if flag == 0xC000:
        return Decimal("NaN")
    if flag == 0x8000:
        sign = -1 if (h & 0x2000) else 1
        dscale = (h & 0x1F80) >> 7
        w = h & 0x3F
        weight = w - 64 if (h & 0x0040) else w
        pos = 2
    else:
        sign = -1 if flag == 0x4000 else 1
        dscale = h & 0x3FFF
        weight = struct.unpack_from("<h", data, 2)[0]
        pos = 4
    ndigits = (len(data) - pos) // 2
    digits = struct.unpack_from(f"<{ndigits}H", data, pos) if ndigits else ()

    if not digits:
        return Decimal(0)
    # 值 = Σ digits[i] * 10000^(weight-i)
    txt = ""
    for i, d in enumerate(digits):
        txt += f"{d:04d}"
    # 小数点位置：weight+1 个 digit 在整数侧
    int_digits = (weight + 1) * 4
    if int_digits >= len(txt):
        intpart = txt + "0" * (int_digits - len(txt))
        frac = ""
    elif int_digits <= 0:
        intpart = "0"
        frac = "0" * (-int_digits) + txt
    else:
        intpart = txt[:int_digits]
        frac = txt[int_digits:]
    # 去前导零
    intpart = intpart.lstrip("0") or "0"
    # 补足/截断到 dscale
    if dscale > len(frac):
        frac = frac + "0" * (dscale - len(frac))
    s = intpart + ("." + frac if frac else "")
    if sign < 0 and (intpart != "0" or frac.strip("0")):
        s = "-" + s
    try:
        return Decimal(s)
    except Exception:
        return RawValue(data, "numeric 解析失败", "decode_error")


def _dec_text(data: bytes):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gbk")
        except UnicodeDecodeError:
            return RawValue(data, "非 UTF-8/GBK 文本", "decode_error")


def _dec_name(b: bytes) -> str:
    return b.rstrip(b"\x00").decode("utf-8", "replace")


def _dec_char(b: bytes) -> str:
    return chr(b[0])


def _dec_bit(data: bytes):
    # varlena: u8 bitlen + 位图字节
    if not data:
        return ""
    bitlen = data[0]
    bits = bin(int.from_bytes(data[1:], "big"))[2:].zfill(8 * (len(data) - 1))
    return bits[:bitlen]


def _dec_bytea(data: bytes) -> bytes:
    return data


def _dec_json(data: bytes):
    return _dec_text(data)


def _dec_array(data: bytes):
    """一维定长元素数组（常见类型）。"""
    if len(data) < 12:
        return RawValue(data, "数组过短", "decode_error")
    ndim, _hasnull, elem_oid = struct.unpack_from("<iiI", data, 0)
    if ndim != 1:
        return RawValue(data, f"{ndim} 维数组暂不支持", "raw")
    dim, lbound = struct.unpack_from("<ii", data, 12)
    pos = 20
    out = []
    elem = TYPE_REGISTRY.get(elem_oid)
    for _ in range(dim):
        if pos + 4 > len(data):
            return RawValue(data, "数组截断", "decode_error")
        ln = struct.unpack_from("<i", data, pos)[0]
        pos += 4
        if ln < 0:
            out.append(None)
            continue
        if ln & 1:  # 1B 头数组元素
            ln = ln >> 1
            item = data[pos:pos + ln - 1]
            pos += ln
        else:
            ln = ln >> 2
            item = data[pos:pos + ln - 4]
            pos += ln
        if elem is None:
            out.append(RawValue(item, f"数组元素类型 {elem_oid} 未注册", "unknown_type"))
        else:
            out.append(elem(item))
    return out


# OID → (typlen, typalign, decoder, 需要varlena去头)
# varlena 解码器接收【去掉 varlena 头后的数据】；定长解码器接收原始字节
TYPE_REGISTRY: dict[int, tuple] = {
    16: (1, "c", _dec_bool, False),
    17: (-1, "i", _dec_bytea, True),
    18: (1, "c", _dec_char, False),
    19: (64, "c", _dec_name, False),
    20: (8, "d", _dec_int8, False),
    21: (2, "s", _dec_int2, False),
    23: (4, "i", _dec_int4, False),
    25: (-1, "i", _dec_text, True),
    26: (4, "i", _dec_uint4, False),
    114: (-1, "i", _dec_json, True),
    142: (-1, "i", _dec_text, True),
    700: (4, "i", _dec_float4, False),
    701: (8, "d", _dec_float8, False),
    705: (-1, "i", _dec_text, True),      # unknown
    829: (6, "s", lambda b: b.hex(":"), False),  # macaddr
    869: (-1, "i", _dec_text, True),     # inet
    650: (-1, "i", _dec_text, True),      # cidr
    1042: (-1, "i", _dec_text, True),     # bpchar
    1043: (-1, "i", _dec_text, True),     # varchar
    1082: (4, "i", _dec_date, False),
    1083: (8, "d", _dec_time, False),
    1114: (8, "d", _dec_timestamp, False),
    1184: (8, "d", _dec_timestamp, False),  # timestamptz（按 UTC 呈现）
    1186: (16, "d", _dec_interval, False),
    1266: (12, "d", _dec_timetz, False),
    1560: (-1, "i", _dec_bit, True),
    1562: (-1, "i", _dec_bit, True),
    1700: (-1, "i", _dec_numeric, True),
    2950: (16, "c", _dec_uuid, False),
    3220: (8, "d", _dec_pg_lsn, False),
    3614: (-1, "i", _dec_text, True),     # tsvector
    3802: (-1, "i", _dec_json, True),     # jsonb（近似文本呈现）
    # 常见一维数组
    1000: (-1, "i", _dec_array, True), 1005: (-1, "i", _dec_array, True),
    1007: (-1, "i", _dec_array, True), 1009: (-1, "i", _dec_array, True),
    1015: (-1, "i", _dec_array, True), 1016: (-1, "i", _dec_array, True),
    1021: (-1, "i", _dec_array, True), 1022: (-1, "i", _dec_array, True),
    1231: (-1, "i", _dec_array, True),
}

# 数组元素 OID → 数组 OID（用于识别）
ARRAY_ELEM_TO_ARRAY = {
    16: 1000, 21: 1005, 23: 1007, 25: 1009, 1043: 1015, 20: 1016,
    700: 1021, 701: 1022, 1700: 1231,
}


def type_info(type_oid: int, type_name: str = ""):
    """返回 (typlen, align, decoder, is_varlena)；未知类型返回 None。"""
    info = TYPE_REGISTRY.get(type_oid)
    if info:
        return info
    # 按类型名兜底
    by_name = {
        "varchar": (1043), "text": 25, "numeric": 1700, "int4": 23, "int8": 20,
        "int2": 21, "timestamp": 1114, "timestamptz": 1184, "date": 1082,
        "bool": 16, "float8": 701, "float4": 700, "bpchar": 1042, "bytea": 17,
        "name": 19, "json": 114, "jsonb": 3802, "uuid": 2950, "time": 1083,
        "timetz": 1266, "interval": 1186, "bit": 1560, "varbit": 1562,
        "pg_lsn": 3220, "inet": 869, "cidr": 650, "char": 18,
    }
    oid = by_name.get((type_name or "").lower())
    if oid:
        return TYPE_REGISTRY[oid]
    return None
