"""元组 deform：WAL 元组数据 → 列值字典。

依据（全部对照官方源码核实）：
  - heapam.c log_heap_insert：WAL 元组 = xl_heap_header(5B) + [t_bits 起的元组数据]
    （"write bitmap [+ padding] [+ oid] + data"，从 offsetof(t_bits)=23 起）
  - postgres.h（小端 varlena 标签）：
      首字节 & 0x03 == 0x00 → 4B 未压缩（size = u32 >> 2）
      首字节 & 0x03 == 0x02 → 4B 压缩（size = u32 >> 2，随后 u32 rawsize + pglz 数据）
      首字节 & 0x01 == 0x01 → 1B（size = byte >> 1）
      首字节 == 0x01        → 1B 外部（TOAST 指针）
  - tupmacs.h att_align_pointer：varlena 列先窥视当前字节，非零则不对方（1B 紧凑存储），
    为零则按 typalign 对齐；定长列按 typalign 对齐
  - htup_details.h：null bitmap 在 t_bits（元组偏移 23），bit 置位 = 非空
"""

from __future__ import annotations

import struct

from . import profiles as P
from .typereg import ExternalValue, RawValue, type_info

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")

HEAP_HASNULL = 0x0001
HEAP_HASOID_OLD = 0x0008
HEAP_NATTS_MASK = 0x07FF


class TupleData:
    """WAL/FPI 元组的统一表示。"""

    __slots__ = ("infomask2", "infomask", "hoff", "data", "natts")

    def __init__(self, infomask2: int, infomask: int, hoff: int, data: bytes):
        self.infomask2 = infomask2
        self.infomask = infomask
        self.hoff = hoff
        self.data = data          # 元组偏移 23 起的字节（含 null bitmap）
        self.natts = infomask2 & HEAP_NATTS_MASK

    @classmethod
    def from_wal_payload(cls, payload: bytes) -> "TupleData":
        """WAL 记录中的元组：xl_heap_header(5B) + data。"""
        if len(payload) < 5:
            raise ValueError("元组载荷过短")
        infomask2, infomask, hoff = struct.unpack_from("<HHB", payload, 0)
        return cls(infomask2, infomask, hoff, payload[5:])

    @classmethod
    def from_page_tuple(cls, tup: bytes) -> "TupleData":
        """页内完整元组：23B HeapTupleHeader + bitmap + data。

        头布局：xmin(0:4) xmax(4:8) cid(8:12) ctid(12:18)
                infomask2(18:20) infomask(20:22) hoff(22:23)
        """
        if len(tup) < 23:
            raise ValueError("页元组过短")
        infomask2, infomask, hoff = struct.unpack_from("<HHB", tup, 18)
        return cls(infomask2, infomask, hoff, tup[23:])


def read_varlena(data: bytes, pos: int):
    """读取 pos 处的 varlena，返回 (kind, payload_or_marker, new_pos)。

    kind: '1b' | '4b' | '4bc' | 'ext'
    """
    if pos >= len(data):
        return "err", RawValue(b"", "varlena 越界", "decode_error"), pos
    b0 = data[pos]
    if b0 == 0x01:  # 1B 外部（TOAST 指针）
        # varattrib_1b_e: header(0x01) + tag(1B) + 数据
        if pos + 2 > len(data):
            return "err", RawValue(data[pos:], "外部指针截断", "decode_error"), len(data)
        tag = data[pos + 1]
        if tag == 18:  # VARTAG_ONDISK
            if pos + 2 + 18 > len(data):
                return "err", RawValue(data[pos:], "TOAST 指针截断", "decode_error"), len(data)
            rawsize, extsize, valueid, toastrelid = struct.unpack_from(
                "<iiII", data, pos + 2)
            return "ext", ExternalValue(rawsize, extsize, valueid, toastrelid), pos + 2 + 18
        return "err", RawValue(data[pos:pos + 2], f"未知外部标签 {tag}", "external"), pos + 2
    if b0 & 0x01:  # 1B 未压缩
        size = b0 >> 1
        end = pos + size
        if end > len(data) or size < 1:
            return "err", RawValue(data[pos:], "1B varlena 长度非法", "decode_error"), len(data)
        return "1b", data[pos + 1:end], end
    # 4B
    if pos + 4 > len(data):
        return "err", RawValue(data[pos:], "4B varlena 截断", "decode_error"), len(data)
    hdr = _U32.unpack_from(data, pos)[0]
    size = (hdr >> 2) & 0x3FFFFFFF
    end = pos + size
    if end > len(data) or size < 4:
        return "err", RawValue(data[pos:], "4B varlena 长度非法", "decode_error"), len(data)
    if b0 & 0x02:  # 压缩
        return "4bc", data[pos + 4:end], end  # 载荷 = rawsize(4B) + pglz 数据
    return "4b", data[pos + 4:end], end


def _align(cur: int, a: int) -> int:
    if a <= 1:
        return cur
    return (cur + a - 1) & ~(a - 1)


def deform(td: TupleData, attrs: list, pglz_decompress=None):
    """元组 → ({attname: value}, issues)。

    attrs: [AttrDef]（字典列，按 attnum 升序）
    返回值中的 RawValue/ExternalValue 表示该列不可执行。
    """
    values: dict = {}
    issues: list[str] = []
    natts = td.natts
    data = td.data

    if natts < len(attrs):
        # 元组早于 ADD COLUMN：缺失列按 NULL（dict 较新）
        pass
    elif natts > len(attrs):
        # 元组列数多于字典：字典早于该元组（DDL 后）——截断并标记
        issues.append(f"元组列数 {natts} > 字典列数 {len(attrs)}（字典时点早于该 WAL）")

    bitmap = b""
    if td.infomask & HEAP_HASNULL:
        bitmap = data[: (natts + 7) // 8]
    user_start = td.hoff - 23
    if user_start < 0 or user_start > len(data):
        issues.append(f"非法 t_hoff={td.hoff}")
        return values, issues
    # 关键：对齐相对【用户数据起点（tuple 偏移 hoff，已 MAXALIGN）】计算，
    # 而非 payload 偏移（payload 基址含 23B 头，直接对齐会错位）。
    cur = 0  # 相对 user data 起点

    def _at(rel: int) -> int:
        return user_start + rel

    for i, att in enumerate(attrs):
        if i >= natts:
            values[att.name] = None   # ADD COLUMN 后的旧元组
            continue
        # null 判定
        if bitmap and not (bitmap[i >> 3] >> (i & 7)) & 1:
            values[att.name] = None
            continue

        info = type_info(att.type_oid, att.type_name)
        if info is None:
            # 未知类型：按 varlena 尽力推进位置
            pos = _at(cur)
            if pos < len(data) and data[pos] != 0:
                pass  # 1B 紧凑，不对方
            else:
                cur = _align(cur, 4)
            kind, payload, cur = read_varlena(data, _at(cur))
            cur = cur - user_start if cur > user_start else 0
            values[att.name] = RawValue(
                payload if isinstance(payload, bytes) else b"",
                f"类型 {att.type_oid}({att.type_name}) 未注册", "unknown_type")
            continue

        typlen, align, decoder, is_varlena = info

        if typlen == -1:  # varlena
            pos = _at(cur)
            if pos >= len(data):
                values[att.name] = None
                issues.append(f"{att.name}: varlena 越界")
                continue
            if data[pos] == 0:  # 填充字节 → 对齐（相对 user 起点）
                cur = _align(cur, 4)
            kind, payload, newpos = read_varlena(data, _at(cur))
            cur = newpos - user_start
            if kind == "err":
                values[att.name] = payload
                issues.append(f"{att.name}: {payload.reason}")
                continue
            if kind == "ext":
                values[att.name] = payload  # ExternalValue
                continue
            if kind == "4bc":
                # 压缩内联：rawsize(4B) + pglz 数据
                if pglz_decompress and len(payload) >= 4:
                    rawsize = struct.unpack_from("<i", payload, 0)[0]
                    try:
                        raw = pglz_decompress(payload[4:], rawsize)
                        values[att.name] = decoder(raw)
                    except Exception as e:
                        values[att.name] = RawValue(payload, f"pglz 解压失败: {e}", "compressed")
                        issues.append(f"{att.name}: pglz 解压失败")
                else:
                    values[att.name] = RawValue(payload, "内联压缩值（未解压）", "compressed")
                continue
            try:
                values[att.name] = decoder(payload)
            except Exception as e:
                values[att.name] = RawValue(payload, f"解码异常: {e}", "decode_error")
                issues.append(f"{att.name}: 解码异常 {type(e).__name__}")
        else:  # 定长
            cur = _align(cur, {"c": 1, "s": 2, "i": 4, "d": 8}[align])
            start = _at(cur)
            end = start + typlen
            if end > len(data):
                values[att.name] = RawValue(data[start:], f"{att.name}: 定长字段截断", "decode_error")
                issues.append(f"{att.name}: 定长字段截断")
                continue
            try:
                values[att.name] = decoder(data[start:end])
            except Exception as e:
                values[att.name] = RawValue(data[start:end], f"解码异常: {e}", "decode_error")
                issues.append(f"{att.name}: 解码异常 {type(e).__name__}")
            cur += typlen

    return values, issues


def is_clean(values: dict) -> bool:
    """所有值均为可执行类型（无 RawValue/ExternalValue）。"""
    for v in values.values():
        if isinstance(v, (RawValue, ExternalValue)):
            return False
    return True
