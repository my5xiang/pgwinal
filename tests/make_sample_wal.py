"""
Generate a minimal synthetic PostgreSQL WAL segment containing
a Heap INSERT + Transaction COMMIT so unit tests can run offline.
"""
from __future__ import annotations

import struct
from pathlib import Path

BLCKSZ = 8192
PAGE_HDR = 24
XLOG_RECORD_HDR = 24
SEG_SIZE = 16 * 1024 * 1024

RM_HEAP = 10
RM_XACT = 1
XLOG_HEAP_INSERT = 0x00
XLOG_XACT_COMMIT = 0x00


def build_page(magic=0x8270, info=0, tli=1, pageaddr=0, xrecoff=0, data=b""):
    hdr = struct.pack("<HHIQII", magic, info, tli, pageaddr, 0, xrecoff)
    body = bytearray(BLCKSZ)
    body[0:24] = hdr
    if data:
        body[PAGE_HDR : PAGE_HDR + len(data)] = data
    return bytes(body)


def build_record(total_len, xid, prev, info, rmid, body: bytes) -> bytes:
    tot = XLOG_RECORD_HDR + len(body)
    # pad body so tot_len matches if needed
    header = struct.pack("<IIQBBHI", tot, xid, prev, info, rmid, 0, 0)
    return header + body


def make_main_data_short(payload: bytes) -> bytes:
    """XLR_BLOCK_ID_DATA_SHORT + len + payload (len must be < 256)."""
    assert len(payload) < 256
    return bytes([255, len(payload)]) + payload


def xl_heap_header(infomask2, infomask, hoff) -> bytes:
    return struct.pack("<HHB", infomask2, infomask, hoff)


def block_ref_image():
    """Minimal block reference without image/data (rlocator + blocknum only)."""
    # id, fork_flags=0 (no same_rel), data_length=0, rlocator 12 bytes, block 4 bytes
    return bytes([0, 0, 0, 0]) + struct.pack("<III", 1663, 16384, 16400) + struct.pack("<I", 0)


def build_sample_wal(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # One page with two records: heap insert + commit
    # heap insert main: xl_heap_insert (offnum=1, flags=0) + xl_heap_header + tuple payload
    # tuple: int4 id=1, text name='hello' with null bitmap none
    # user data after header: for text with varlena
    # int4: 4 bytes little endian
    text = b"hello"
    # 1-byte varlena: header 0x80|5 = 0x85, then 'hello'
    name_b = bytes([0x80 | len(text)]) + text
    user = struct.pack("<i", 1) + name_b
    hoff = 23  # standard
    xh = xl_heap_header(2, 0, hoff)  # 2 attrs, no nulls
    insert_payload = xh + user  # goes into block data
    # block with data
    bref = (
        bytes([0, 0x20])  # id=0, HAS_DATA
        + struct.pack("<H", len(insert_payload))
        + struct.pack("<III", 1663, 16384, 16400)
        + struct.pack("<I", 0)
        + insert_payload
    )
    xl_insert_main = struct.pack("<HB", 1, 0)  # offnum, flags
    insert_body = bref + make_main_data_short(xl_insert_main)
    rec_insert = build_record(0, 100, 0, XLOG_HEAP_INSERT, RM_HEAP, insert_body)

    # commit record: xact_time = microseconds since 2000-01-01
    commit_main = struct.pack("<Q", 0)
    commit_body = make_main_data_short(commit_main)
    rec_commit = build_record(0, 100, 0, XLOG_XACT_COMMIT, RM_XACT, commit_body)

    records = rec_insert + rec_commit
    data_end = PAGE_HDR + len(records)
    page = build_page(pageaddr=0, xrecoff=data_end, data=records)

    # pad to a small file (not full 16MB) — scanner works on file size
    path.write_bytes(page)
    return path


if __name__ == "__main__":
    out = Path(__file__).parent / "sample" / "000000010000000000000001"
    out.parent.mkdir(parents=True, exist_ok=True)
    build_sample_wal(out)
    print("wrote", out, "size", out.stat().st_size)
