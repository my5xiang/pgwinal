from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from ..constants import (
    HEAP_COMBOCID,
    HEAP_HASNULL,
    HEAP_HASEXTERNAL,
    HEAP_ONLY_TUPLE,
    HEAP_XMAX_INVALID,
    VARATT_1B,
)
from ..dictstore.schema import AttributeDef, RelationDef

# type oids we decode nicely
OID_BOOL = 16
OID_BYTEA = 17
OID_CHAR = 18
OID_NAME = 19
OID_INT8 = 20
OID_INT2 = 21
OID_INT4 = 23
OID_TEXT = 25
OID_OID = 26
OID_JSON = 114
OID_XML = 142
OID_FLOAT4 = 700
OID_FLOAT8 = 701
OID_UNKNOWN = 705
OID_NUMERIC = 1700
OID_VARCHAR = 1043
OID_DATE = 1082
OID_TIME = 1083
OID_TIMESTAMP = 1114
OID_TIMESTAMPTZ = 1184
OID_INTERVAL = 1186
OID_TIMETZ = 1266
OID_UUID = 2950
OID_JSONB = 3802
OID_INT4RANGE = 3904
OID_NUMRANGE = 3906
OID_TSRANGE = 3908
OID_TSTZRANGE = 3910
OID_DATERANGE = 3912
OID_INT8RANGE = 3926
OID_INT4ARRAY = 1007
OID_INT8ARRAY = 1016
OID_TEXARRAY = 1009
OID_VARCHARARRAY = 1015
OID_TEXTARRAY = 1009
OID_NUMERICARRAY = 1231
OID_BYTEAARRAY = 1001
OID_OIDARRAY = 1028
OID_FLOAT8ARRAY = 1022
OID_FLOAT4ARRAY = 1021
OID_BOOLARRAY = 1000
OID_TIMESTAMPARRAY = 1115
OID_TIMESTAMPTZARRAY = 1185
OID_DATEARRAY = 1182
OID_INT2ARRAY = 1005

PG_EPOCH = datetime(2000, 1, 1)


@dataclass
class TupleData:
    t_xmin: int
    t_xmax: int
    t_cid: int
    t_ctid_block: int
    t_ctid_offset: int
    t_infomask2: int
    t_infomask: int
    t_hoff: int
    nulls: list[bool] = field(default_factory=list)
    user_data: bytes = b""
    attnum: int = 0  # number of attributes from infomask2


def decode_heap_header_fixed(data: bytes) -> TupleData:
    """Reconstruct fixed HeapTupleHeader fields from WAL xl_heap_header + payload."""
    if len(data) < 5:
        raise ValueError("heap header too short")
    t_infomask2, t_infomask, t_hoff = struct.unpack_from("<HHB", data, 0)
    # payload after 5-byte xl_heap_header is the portion at t_hoff
    # We reconstruct synthetic xmin/xmax/cid/ctid from WAL main data separately;
    # here store payload only.
    return TupleData(
        t_xmin=0,
        t_xmax=0,
        t_cid=0,
        t_ctid_block=0,
        t_ctid_offset=0,
        t_infomask2=t_infomask2,
        t_infomask=t_infomask,
        t_hoff=t_hoff,
        nulls=[],
        user_data=data[5:],
        attnum=t_infomask2 & 0x07FF,
    )


def attach_from_main(td: TupleData, main: bytes) -> None:
    """Fill xmin/xmax/cid/ctid from xl_heap_* main header when possible."""
    pass


def decode_tuple_values(
    td: TupleData,
    rel: Optional[RelationDef],
    heap_tuple_payload: Optional[bytes] = None,
) -> dict[str, Any]:
    """
    Decode column values.

    WAL stores: xl_heap_header + (null bitmap + user data) starting at t_hoff.
    Full on-disk tuple layout after fixed 23-byte header:
      - padding to MAXALIGN(hoff)
      - null bitmap
      - user data

    For WAL-parsed records, user_data is the blob after xl_heap_header which
    corresponds to (null_bitmap + data) starting at t_hoff.
    """
    blob = heap_tuple_payload if heap_tuple_payload is not None else td.user_data
    if rel is None:
        return {"_raw_hex": blob.hex(), "_note": "no_data_dictionary"}

    attrs = [a for a in rel.attributes if not a.is_dropped]
    natts = len(attrs)
    hasnull = bool(td.t_infomask & HEAP_HASNULL)

    pos = 0
    nullmap: list[Optional[int]] = [None] * natts  # None = not null, 0 = null

    if hasnull and natts > 0:
        # null bitmap is at start of payload (t_hoff region)
        nbitmap_bytes = (natts + 7) // 8
        if len(blob) < nbitmap_bytes:
            return {"_raw_hex": blob.hex(), "_note": "short_null_bitmap"}
        bitmap = blob[:nbitmap_bytes]
        pos = nbitmap_bytes
        for i in range(natts):
            byte = bitmap[i // 8]
            bit = 1 << (i % 8)
            nullmap[i] = 0 if (byte & bit) else None
        nulls = [m == 0 for m in nullmap]
    else:
        nulls = [False] * natts

    values: dict[str, Any] = {}
    data = blob[pos:]
    dpos = 0

    for i, attr in enumerate(attrs):
        if nulls[i]:
            values[attr.attname] = None
            continue
        val, dpos, status = _decode_attribute(attr, data, dpos, td.t_infomask)
        values[attr.attname] = val
        if status == "external":
            values[attr.attname] = {"type": "TOAST/EXTERNAL", "status": status}
            break
        if status == "short":
            values[attr.attname] = {
                "type": attr.type_name or f"oid:{attr.type_oid}",
                "status": "truncated_payload",
                "raw_hex": data[dpos:].hex() if dpos < len(data) else "",
            }
            break

    return values


def _decode_attribute(
    attr: AttributeDef, data: bytes, pos: int, infomask: int
) -> tuple[Any, int, str]:
    """Return (value, new_pos, status)."""
    if pos >= len(data):
        return None, pos, "short"
    oid = attr.type_oid
    try:
        if oid in (OID_INT2,):
            if pos + 2 > len(data):
                return None, pos, "short"
            return struct.unpack_from("<h", data, pos)[0], pos + 2, "ok"
        if oid in (OID_INT4, OID_OID, 26, 29):  # int4/oid/xid/cid
            if pos + 4 > len(data):
                return None, pos, "short"
            return struct.unpack_from("<I", data, pos)[0], pos + 4, "ok"
        if oid in (OID_INT8,):
            if pos + 8 > len(data):
                return None, pos, "short"
            return struct.unpack_from("<q", data, pos)[0], pos + 8, "ok"
        if oid == OID_FLOAT4:
            if pos + 4 > len(data):
                return None, pos, "short"
            return struct.unpack_from("<f", data, pos)[0], pos + 4, "ok"
        if oid == OID_FLOAT8:
            if pos + 8 > len(data):
                return None, pos, "short"
            return struct.unpack_from("<d", data, pos)[0], pos + 8, "ok"
        if oid == OID_BOOL:
            return (data[pos] != 0), pos + 1, "ok"
        if oid in (OID_TEXT, OID_VARCHAR, OID_NAME, OID_CHAR, OID_UNKNOWN, OID_JSON, OID_XML):
            s, npos = _decode_cstring_like(data, pos)
            return s, npos, "ok"
        if oid == OID_BYTEA:
            s, npos = _decode_varlena_bytes(data, pos)
            return s, npos, "ok"
        if oid == OID_NUMERIC:
            return _decode_numeric(data, pos)
        if oid == OID_DATE:
            if pos + 4 > len(data):
                return None, pos, "short"
            days = struct.unpack_from("<i", data, pos)[0]
            if days == 0x7FFFFFFF:
                return "infinity", pos + 4, "ok"
            if days == -0x80000000:
                return "-infinity", pos + 4, "ok"
            return (PG_EPOCH.date() + __import__("datetime").timedelta(days=days)).isoformat(), pos + 4, "ok"
        if oid == OID_TIME:
            return _decode_time(data, pos, False)
        if oid == OID_TIMETZ:
            return _decode_timetz(data, pos)
        if oid in (OID_TIMESTAMP, OID_TIMESTAMPTZ):
            return _decode_timestamp(data, pos)
        if oid == OID_UUID:
            if pos + 16 > len(data):
                return None, pos, "short"
            return str(UUID(bytes_le=data[pos : pos + 16] if False else bytes(reversed(data[pos:pos+16])))), pos + 16, "ok"
        if oid == OID_INTERVAL:
            return _decode_interval(data, pos)
        if oid == OID_JSONB:
            s, npos = _decode_varlena_bytes(data, pos)
            # strip version byte 1
            if isinstance(s, (bytes, bytearray)) and len(s) > 0 and s[0] == 1:
                try:
                    return s[1:].decode("utf-8", "replace"), npos, "ok"
                except Exception:
                    return s.hex(), npos, "ok"
            if isinstance(s, (bytes, bytearray)):
                return s.hex(), npos, "ok"
            return s, npos, "ok"
        # arrays
        if oid in (
            OID_INT2ARRAY,
            OID_INT4ARRAY,
            OID_INT8ARRAY,
            OID_FLOAT4ARRAY,
            OID_FLOAT8ARRAY,
            OID_BOOLARRAY,
            OID_TEXARRAY,
            OID_VARCHARARRAY,
            OID_TEXTARRAY,
            OID_NUMERICARRAY,
            OID_BYTEAARRAY,
            OID_OIDARRAY,
            OID_TIMESTAMPARRAY,
            OID_TIMESTAMPTZARRAY,
            OID_DATEARRAY,
        ):
            raw, npos = _decode_varlena_bytes(data, pos)
            if isinstance(raw, (bytes, bytearray)):
                return _decode_array(raw, attr.type_name or ""), npos, "ok"
            return raw, npos, "ok"
        # ranges / fallback text
        s, npos = _decode_cstring_like(data, pos)
        return s, npos, "ok"
    except Exception as exc:
        return f"<decode_error:{exc}>", pos, "ok"


def _decode_cstring_like(data: bytes, pos: int) -> tuple[Any, int]:
    if pos >= len(data):
        return None, pos
    # varlena may prefix text
    if data[pos] & VARATT_1B:
        length = data[pos] & 0x7F
        start = pos + 1
        end = start + length
        if end > len(data):
            return data[start:].hex(), len(data)
        chunk = data[start:end]
        return _try_text(chunk), end
    # 4-byte varlena little-endian: lower 2 bits length
    if pos + 4 <= len(data):
        header = struct.unpack_from("<I", data, pos)[0]
        # varattrib_4b: va_header, length in lower bits
        length = header >> 2 & 0x3FFFFFFF
        # sometimes raw cstring without varlena
        # Heuristic: if first byte is printable and no valid 4B length, treat as cstring
        if length == 0 or length > len(data) - pos:
            # fallback cstring
            end = data.find(b"\x00", pos)
            if end == -1:
                return _try_text(data[pos:]), len(data)
            return _try_text(data[pos:end]), end + 1
        start = pos + 4
        end = start + length
        if end > len(data):
            return _try_text(data[start:]), len(data)
        return _try_text(data[start:end]), end
    end = data.find(b"\x00", pos)
    if end == -1:
        return _try_text(data[pos:]), len(data)
    return _try_text(data[pos:end]), end + 1


def _decode_varlena_bytes(data: bytes, pos: int) -> tuple[Any, int]:
    if pos >= len(data):
        return None, pos
    if data[pos] & VARATT_1B:
        length = data[pos] & 0x7F
        start = pos + 1
        end = start + length
        if end > len(data):
            return data[start:], len(data)
        chunk = data[start:end]
        # external toast pointer is 1-byte header + toast pointer struct
        if length >= 18 and (data[pos] & 0x40):
            return {"toast_external": chunk.hex()}, end
        return chunk, end
    if pos + 4 <= len(data):
        header = struct.unpack_from("<I", data, pos)[0]
        length = header >> 2 & 0x3FFFFFFF
        if 0 < length <= len(data) - pos - 4:
            start = pos + 4
            end = start + length
            return data[start:end], end
    # no valid varlena — take rest
    return data[pos:], len(data)


def _try_text(chunk: bytes) -> Any:
    try:
        return chunk.decode("utf-8")
    except Exception:
        try:
            return chunk.decode("gbk")
        except Exception:
            return chunk.hex()


def _decode_numeric(data: bytes, pos: int) -> tuple[Any, int, str]:
    raw, npos = _decode_varlena_bytes(data, pos)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 4:
        return raw, npos, "ok"
    # NumericShort / NumericLong simplified
    try:
        # Use numeric binary decode (best-effort)
        weight = struct.unpack_from("<H", raw, 2)[0]
        sign_ds = struct.unpack_from("<H", raw, 4)[0]
        # fall back: represent as hex if complex
        # Simple NumericShort: ndigits small
        ndigits = sign_ds & 0x3FFF
        sign = (sign_ds >> 14) & 0x3
        digits = []
        for i in range(ndigits):
            off = 6 + i * 2
            if off + 2 > len(raw):
                break
            digits.append(struct.unpack_from("<H", raw, off)[0])
        if not digits:
            return 0, npos, "ok"
        # approximate decimal string
        scale = 4  # NUMERIC_SHORT DisplayScale default handling simplified
        # Build integer from base-10000 digits with weight
        int_part_digits = weight + 1
        s = ""
        for i in range(-weight, ndigits - weight):
            idx = i + weight
            d = digits[idx] if 0 <= idx < len(digits) else 0
            s += f"{d:04d}"
        # trim / insert point
        if len(s) < int_part_digits * 4:
            s = s.zfill(int_part_digits * 4)
        int_s = s[: int_part_digits * 4].lstrip("0") or "0"
        frac = s[int_part_digits * 4 :].rstrip("0")
        val = int_s if not frac else f"{int_s}.{frac}"
        if sign == 1:
            val = "-" + val
        return val, npos, "ok"
    except Exception:
        return raw.hex(), npos, "ok"


def _decode_time(data: bytes, pos: int, with_tz: bool) -> tuple[Any, int, str]:
    if pos + 8 > len(data):
        return None, pos, "short"
    micros = struct.unpack_from("<q", data, pos)[0]
    if micros == 0x7FFFFFFF or micros == 0x7FFFFFFFFFFFFFFF:
        return "infinity", pos + 8, "ok"
    seconds = micros // 1_000_000
    frac = micros % 1_000_000
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:06d}", pos + 8, "ok"


def _decode_timetz(data: bytes, pos: int) -> tuple[Any, int, str]:
    if pos + 12 > len(data):
        return None, pos, "short"
    micros = struct.unpack_from("<q", data, pos)[0]
    tz = struct.unpack_from("<i", data, pos + 8)[0]
    seconds = micros // 1_000_000
    frac = micros % 1_000_000
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:06d}{tz}", pos + 12, "ok"


def _decode_timestamp(data: bytes, pos: int) -> tuple[Any, int, str]:
    if pos + 8 > len(data):
        return None, pos, "short"
    micros = struct.unpack_from("<q", data, pos)[0]
    if micros == 0x7FFFFFFFFFFFFFFF:
        return "infinity", pos + 8, "ok"
    if micros == -0x8000000000000000:
        return "-infinity", pos + 8, "ok"
    try:
        dt = PG_EPOCH + __import__("datetime").timedelta(microseconds=micros)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f"), pos + 8, "ok"
    except Exception:
        return micros, pos + 8, "ok"


def _decode_interval(data: bytes, pos: int) -> tuple[Any, int, str]:
    if pos + 16 > len(data):
        return None, pos, "short"
    usec, days, months = struct.unpack_from("<qIi", data, pos)
    return f"months={months},days={days},usec={usec}", pos + 16, "ok"


def _decode_array(raw: bytes, type_name: str) -> Any:
    """Best-effort PostgreSQL array binary decode."""
    if len(raw) < 12:
        return raw.hex()
    ndim, flags, _elemoid = struct.unpack_from("<III", raw, 0)
    if ndim > 6 or ndim * 8 + 12 > len(raw):
        return raw.hex()
    dims = []
    pos = 12
    for _ in range(ndim):
        if pos + 8 > len(raw):
            return raw.hex()
        dim, _lbound = struct.unpack_from("<II", raw, pos)
        dims.append(dim)
        pos += 8
    total = 1
    for d in dims:
        total *= d
    # element decode by type name
    elements = []
    for _ in range(min(total, 1000)):
        if pos >= len(data_end(raw)):
            break
        # null bitmap after dims
        break
    # null bitmap size
    # For simplicity return hex for non-trivial arrays beyond dim info
    return {"dims": dims, "flags": flags, "raw_len": len(raw)}


def data_end(raw: bytes) -> bytes:
    return raw
