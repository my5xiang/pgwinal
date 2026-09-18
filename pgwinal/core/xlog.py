from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

from ..constants import (
    BLCKSZ,
    DEFAULT_WAL_SEG_SIZE,
    RM_HEAP_ID,
    RM_HEAP2_ID,
    RM_XACT_ID,
    XLOG_PAGE_HDR_SIZE,
    XLP_FIRST_CONTINUE,
    XLP_LONG_HEADER,
)
from .models import BlockRef, RelFileLocator, WalRecord, XLogRecord

# XLogRecord fixed header (PG12+):
# uint32 tot_len | uint32 xid | uint64 prev | uint8 info | uint8 rmid | uint16 pad | uint32 crc
_XLOG_RECORD_FMT = "<IIQBBHI"
_XLOG_RECORD_SIZE = struct.calcsize(_XLOG_RECORD_FMT)  # 24? 4+4+8+1+1+2+4=24

# Actually SizeOfXLogRecord in PG is 24:
# offsetof(xl_crc)+4 = 4+4+8+1+1+2 + 4 = 24
assert _XLOG_RECORD_SIZE == 24

_XLOG_PAGE_FMT = "<HHIQII"
_XLOG_PAGE_SIZE = struct.calcsize(_XLOG_PAGE_FMT)  # 2+2+4+8+4+4=24
assert _XLOG_PAGE_SIZE == XLOG_PAGE_HDR_SIZE

BKPBLOCK_FORK_MASK = 0x0F
BKPBLOCK_FLAG_MASK = 0xF0
BKPBLOCK_HAS_IMAGE = 0x10
BKPBLOCK_HAS_DATA = 0x20
BKPBLOCK_WILL_INIT = 0x40
BKPBLOCK_SAME_REL = 0x80

XLR_BLOCK_ID_DATA_SHORT = 255
XLR_BLOCK_ID_DATA_LONG = 254
XLR_BLOCK_ID_ORIGIN = 253
XLR_BLOCK_ID_TOPLEVEL_XID = 252


class XLogError(Exception):
    pass


@dataclass
class PageHeader:
    magic: int
    info: int
    tli: int
    pageaddr: int
    xlog_blckseg: int
    xrecoff: int


def parse_page_header(buf: bytes, off: int = 0) -> PageHeader:
    magic, info, tli, pageaddr, seg, xrecoff = struct.unpack_from(_XLOG_PAGE_FMT, buf, off)
    return PageHeader(magic, info, tli, pageaddr, seg, xrecoff)


class WalSegment:
    """One WAL segment file (typically 16MB)."""

    def __init__(self, path: Path, seg_no: int = 0, timeline: int = 1):
        self.path = Path(path)
        self.seg_no = seg_no
        self.timeline = timeline
        self.size = self.path.stat().st_size

    @classmethod
    def from_filename(cls, path: Path) -> "WalSegment":
        """Parse TLI + 8-digit segno from name like 000000010000000000000001."""
        name = path.stem
        timeline = 1
        seg_no = 0
        if len(name) >= 24 and name[:16].isdigit() and name[16:].isdigit():
            timeline = int(name[:8])
            seg_no = int(name[8:])
        elif len(name) >= 8 and name.isdigit():
            # partial / custom names
            timeline = int(name[:8]) if len(name) >= 8 else 1
        return cls(path, seg_no=seg_no, timeline=timeline)


class WalScanner:
    """Scan one or more WAL files and yield raw WalRecord objects."""

    def __init__(
        self,
        paths: list[Path],
        seg_size: int = DEFAULT_WAL_SEG_SIZE,
        strict_crc: bool = False,
    ):
        self.paths = [Path(p) for p in paths]
        self.seg_size = seg_size
        self.strict_crc = strict_crc

    def scan(self, progress_cb=None) -> Iterator[WalRecord]:
        for idx, path in enumerate(sorted(self.paths, key=lambda p: p.name.lower())):
            if progress_cb:
                progress_cb(f"扫描文件 ({idx+1}/{len(self.paths)}): {path.name}")
            # warn when copied filename does not match page header segment
            try:
                with open(path, "rb") as fh:
                    hdr = parse_page_header(fh.read(XLOG_PAGE_HDR_SIZE))
                if hdr.pageaddr and len(path.name) >= 24:
                    logid = (hdr.pageaddr >> 32) & 0xFFFFFFFF
                    segno = (hdr.pageaddr >> 24) & 0xFF
                    correct = f"{hdr.tli:08X}{logid:08X}{segno:08X}"
                    if path.name[:24].upper() != correct:
                        if progress_cb:
                            progress_cb(
                                f"警告: 文件名 {path.name} 与页头段号不符，建议改名为 {correct}"
                            )
            except Exception:
                pass
            yield from self.scan_file(path)
        if progress_cb:
            progress_cb("扫描完成")

    def scan_file(self, path: Path) -> Iterator[WalRecord]:
        with open(path, "rb") as f:
            yield from self._scan_stream(f, path.name)

    def _scan_stream(self, f: BinaryIO, name: str) -> Iterator[WalRecord]:
        file_size = f.seek(0, 2)
        f.seek(0)
        pos = 0
        pending: Optional[bytes] = None
        pending_len = 0
        pending_end_lsn = 0
        page_index = 0

        while pos + XLOG_PAGE_HDR_SIZE <= file_size:
            f.seek(pos)
            page = f.read(BLCKSZ)
            if len(page) < XLOG_PAGE_HDR_SIZE:
                break
            try:
                hdr = parse_page_header(page)
            except struct.error:
                pos += BLCKSZ
                page_index += 1
                continue

            # Heuristic: invalid page (zeroed / torn)
            if hdr.magic == 0 and hdr.xrecoff == 0 and hdr.pageaddr == 0:
                pos += BLCKSZ
                page_index += 1
                continue

            page_addr = hdr.pageaddr
            # valid data end in this page
            end = hdr.xrecoff
            if end <= XLOG_PAGE_HDR_SIZE or end > BLCKSZ:
                # might still have data if xrecoff field variant; fallback
                end = BLCKSZ
                # try long header?
                if hdr.info & XLP_LONG_HEADER:
                    end = BLCKSZ

            off = XLOG_PAGE_HDR_SIZE
            first_continue = bool(hdr.info & XLP_FIRST_CONTINUE)

            while off < end:
                if pending is not None:
                    # continuation of previous record
                    need = pending_len - len(pending)
                    if need <= 0:
                        pending = None
                        continue
                    chunk = page[off : off + need]
                    pending += chunk
                    off += len(chunk)
                    if len(pending) == pending_len:
                        rec = self._decode_record(pending, pending_end_lsn - pending_len, name)
                        if rec:
                            yield rec
                        pending = None
                    continue

                if off + _XLOG_RECORD_SIZE > end:
                    break

                (
                    tot_len,
                    xid,
                    prev,
                    info,
                    rmid,
                    _pad,
                    crc,
                ) = struct.unpack_from(_XLOG_RECORD_FMT, page, off)

                if tot_len < _XLOG_RECORD_SIZE or tot_len > 16 * 1024 * 1024:
                    # resync: jump to next page
                    break

                available = end - off
                if available >= tot_len:
                    record_bytes = page[off : off + tot_len]
                    # absolute LSN of record start
                    start_lsn = page_addr + off
                    rec = self._decode_record(record_bytes, start_lsn, name)
                    if rec:
                        yield rec
                    off += tot_len
                else:
                    pending = page[off:end]
                    pending_len = tot_len
                    start_lsn = page_addr + off
                    pending_end_lsn = start_lsn + tot_len
                    off = end

            pos += BLCKSZ
            page_index += 1

    def _decode_record(self, record_bytes: bytes, start_lsn: int, name: str) -> Optional[WalRecord]:
        if len(record_bytes) < _XLOG_RECORD_SIZE:
            return None
        tot_len, xid, prev, info, rmid, _pad, crc = struct.unpack_from(
            _XLOG_RECORD_FMT, record_bytes, 0
        )
        if tot_len < _XLOG_RECORD_SIZE or len(record_bytes) < tot_len:
            return None
        body = record_bytes[_XLOG_RECORD_SIZE:tot_len]
        rec = XLogRecord(
            total_len=tot_len,
            xid=xid,
            prev=prev,
            info=info,
            rmid=rmid,
            crc=crc,
            start_lsn=start_lsn,
            end_lsn=start_lsn + tot_len,
            data=body,
        )
        try:
            blocks, main = self._split_body(body)
        except Exception:
            blocks, main = [], body
        return WalRecord(rec=rec, blocks=blocks, main_data=main)

    def _split_body(self, body: bytes) -> tuple[list[BlockRef], bytes]:
        blocks: list[BlockRef] = []
        pos = 0
        last_rlocator: Optional[RelFileLocator] = None
        n = len(body)

        while pos < n:
            block_id = body[pos]
            if block_id in (
                XLR_BLOCK_ID_DATA_SHORT,
                XLR_BLOCK_ID_DATA_LONG,
                XLR_BLOCK_ID_ORIGIN,
                XLR_BLOCK_ID_TOPLEVEL_XID,
            ):
                break
            if pos + 4 > n:
                break
            fork_flags = body[pos + 1]
            data_length = struct.unpack_from("<H", body, pos + 2)[0]
            pos += 4
            has_image = bool(fork_flags & BKPBLOCK_HAS_IMAGE)
            has_data = bool(fork_flags & BKPBLOCK_HAS_DATA)
            same_rel = bool(fork_flags & BKPBLOCK_SAME_REL)

            image_length = 0
            hole_offset = 0
            hole_length = 0
            bimg_info = 0
            if has_image:
                if pos + 5 > n:
                    break
                image_length, hole_offset, bimg_info = struct.unpack_from("<HHB", body, pos)
                pos += 5
                if (bimg_info & 0x01) and (bimg_info & (0x04 | 0x08 | 0x10)):
                    if pos + 2 > n:
                        break
                    hole_length = struct.unpack_from("<H", body, pos)[0]
                    pos += 2

            rlocator = last_rlocator
            if not same_rel:
                if pos + 12 > n:
                    break
                spc, db, rel = struct.unpack_from("<III", body, pos)
                rlocator = RelFileLocator(spc, db, rel)
                last_rlocator = rlocator
                pos += 12

            if pos + 4 > n:
                break
            block_num = struct.unpack_from("<I", body, pos)[0]
            pos += 4

            payload = b""
            if has_data:
                if pos + data_length > n:
                    break
                payload = body[pos : pos + data_length]
                pos += data_length

            blocks.append(
                BlockRef(
                    block_id=block_id,
                    fork_flags=fork_flags,
                    data_length=data_length,
                    has_image=has_image,
                    has_data=has_data,
                    same_rel=same_rel,
                    rlocator=rlocator,
                    block_num=block_num,
                    image_length=image_length,
                    hole_offset=hole_offset,
                    hole_length=hole_length,
                    bimg_info=bimg_info,
                    payload=payload,
                )
            )

        main = b""
        if pos < n:
            bid = body[pos]
            if bid == XLR_BLOCK_ID_DATA_SHORT and pos + 2 <= n:
                ln = body[pos + 1]
                main = body[pos + 2 : pos + 2 + ln]
            elif bid == XLR_BLOCK_ID_DATA_LONG and pos + 5 <= n:
                ln = struct.unpack_from("<I", body, pos + 1)[0]
                main = body[pos + 5 : pos + 5 + ln]
            else:
                # leftover is treated as main data (lenient)
                main = body[pos:]
        return blocks, main


def collect_wal_files(path: Path) -> list[Path]:
    """Accept a directory or a file; return sorted segment files."""
    path = Path(path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []

    def _is_hex_token(s: str) -> bool:
        return bool(s) and all(c in "0123456789abcdefABCDEF" for c in s)

    files: list[Path] = []
    for p in path.iterdir():
        if not p.is_file():
            continue
        name = p.name
        # classic WAL segment: 24 hex digits e.g. 000000010000000000000001 / ...0000006F
        if len(name) >= 24 and _is_hex_token(name[:16]) and _is_hex_token(name[16:24]):
            files.append(p)
            continue
        if len(name) == 24 and _is_hex_token(name):
            files.append(p)
            continue
        if p.suffix.lower() in (".wal", ".log", ".partial"):
            files.append(p)
    return sorted(files, key=lambda p: p.name.lower())

