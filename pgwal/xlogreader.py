"""严格 WAL 帧扫描器：段/页/记录 + CRC32C + prev 链校验。

实现规格 = REL_12_STABLE src/backend/access/transam/xlogreader.c：
  - XLogReadRecord     ：记录帧、跨页/跨段续记录、XLOG_SWITCH、prev 链
  - ValidXLogRecord    ：CRC = CRC32C(body || header[0:20])
  - XLogReaderValidatePageHeader ：magic/info/pageaddr/长头/TLI
  - DecodeXLogRecord   ：块引用链 + main data（decode_record_body）

与 pg_waldump 一致的纪律：校验失败 = 报错停止，绝不启发式重同步。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from . import profiles as P
from .crc32c import crc32c

_REC_HDR = struct.Struct("<IIQBBHI")     # tot_len, xid, prev, info, rmid, pad, crc
_PAGE_HDR = struct.Struct("<HHIQ")       # magic, info, tli, pageaddr
_LONG_EXTRA = struct.Struct("<QII")      # sysid, seg_size, blcksz（offset 24）
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")


def MAXALIGN(x: int) -> int:
    return (x + 7) & ~7


MAX_RECORD_BYTES = 64 * 1024 * 1024  # 防御上限（真实上限 XLogRecordMaxSize≈1GB）


class WalError(Exception):
    """帧级错误（CRC/布局/连续性）。"""


@dataclass
class BlockRef:
    block_id: int
    forknum: int
    flags: int
    has_image: bool
    has_data: bool
    data_len: int
    bimg_len: int = 0
    hole_offset: int = 0
    bimg_info: int = 0
    hole_length: int = 0
    rlocator: tuple = (0, 0, 0)   # (spcNode, dbNode, relNode)
    blkno: int = 0
    image: bytes = b""
    data: bytes = b""


@dataclass
class Record:
    start_lsn: int
    end_lsn: int
    tot_len: int
    xid: int
    prev: int
    info: int
    rmid: int
    crc: int
    blocks: list = field(default_factory=list)
    main_data: bytes = b""
    origin_id: int = 0
    toplevel_xid: int = 0  # PG13+ 的 XLR_BLOCK_ID_TOPLEVEL_XID 块值（子事务→顶层）

    def block(self, block_id: int) -> Optional[BlockRef]:
        for b in self.blocks:
            if b.block_id == block_id:
                return b
        return None

    def is_xlog_switch(self) -> bool:
        return self.rmid == P.RM_XLOG_ID and (self.info & ~P.XLR_INFO_MASK) == P.XLOG_SWITCH

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<Record lsn={self.start_lsn:016X} rmid={self.rmid} "
                f"({P.RMGR_NAMES.get(self.rmid, '?')}) info={self.info:02X} "
                f"xid={self.xid} len={self.tot_len}>")


def decode_record_body(body: bytes, profile: P.VersionProfile) -> tuple:
    """DecodeXLogRecord 的 Python 等价实现。

    返回 (blocks: list[BlockRef], main_data: bytes, origin_id)。
    body = 记录 24B 头之后的全部字节（长度 = tot_len - 24）。
    """
    blocks_by_id: dict[int, BlockRef] = {}
    main_len = 0
    origin_id = 0
    toplevel_xid = 0
    ptr = 0
    remaining = len(body)
    datatotal = 0
    rnode: Optional[tuple] = None

    while remaining > datatotal:
        if remaining < 1:
            raise WalError("record with invalid length (header overrun)")
        block_id = body[ptr]
        ptr += 1
        remaining -= 1

        if block_id == P.XLR_BLOCK_ID_DATA_SHORT:
            if remaining < 1:
                raise WalError("record with invalid length (short data hdr)")
            main_len = body[ptr]
            ptr += 1
            remaining -= 1
            datatotal += main_len
            break  # main data 按约定在最后
        elif block_id == P.XLR_BLOCK_ID_DATA_LONG:
            if remaining < 4:
                raise WalError("record with invalid length (long data hdr)")
            main_len = _U32.unpack_from(body, ptr)[0]
            ptr += 4
            remaining -= 4
            datatotal += main_len
            break
        elif block_id == P.XLR_BLOCK_ID_ORIGIN:
            if remaining < 2:
                raise WalError("record with invalid length (origin)")
            origin_id = _U16.unpack_from(body, ptr)[0]
            ptr += 2
            remaining -= 2
        elif block_id == P.XLR_BLOCK_ID_TOPLEVEL_XID:
            # PG13+；PG12 的 xlogreader 不接受该块（子事务用 XACT_ASSIGNMENT）
            if not profile.has_toplevel_xid_block:
                raise WalError("unexpected XLR_BLOCK_ID_TOPLEVEL_XID in PG12 record")
            if remaining < 4:
                raise WalError("record with invalid length (toplevel xid)")
            toplevel_xid = _U32.unpack_from(body, ptr)[0]
            ptr += 4
            remaining -= 4
        elif block_id <= P.XLR_MAX_BLOCK_ID:
            if block_id in blocks_by_id:
                raise WalError(f"out-of-order block_id {block_id}")
            if remaining < 3:
                raise WalError("record with invalid length (block hdr)")
            fork_flags = body[ptr]
            data_len = _U16.unpack_from(body, ptr + 1)[0]
            ptr += 3
            remaining -= 3
            has_image = bool(fork_flags & P.BKPBLOCK_HAS_IMAGE)
            has_data = bool(fork_flags & P.BKPBLOCK_HAS_DATA)
            if has_data != (data_len > 0):
                raise WalError("BKPBLOCK_HAS_DATA 与 data_length 矛盾")
            datatotal += data_len

            blk = BlockRef(
                block_id=block_id,
                forknum=fork_flags & P.BKPBLOCK_FORK_MASK,
                flags=fork_flags,
                has_image=has_image,
                has_data=has_data,
                data_len=data_len,
            )

            if has_image:
                if remaining < 5:
                    raise WalError("record with invalid length (image hdr)")
                blk.bimg_len = _U16.unpack_from(body, ptr)[0]
                blk.hole_offset = _U16.unpack_from(body, ptr + 2)[0]
                blk.bimg_info = body[ptr + 4]
                ptr += 5
                remaining -= 5
                # 压缩位分版本（PG12–14: IS_COMPRESSED=0x02；PG15+: 方法位）
                compressed = profile.image_is_compressed(blk.bimg_info)
                if compressed:
                    if blk.bimg_info & P.BKPIMAGE_HAS_HOLE:
                        if remaining < 2:
                            raise WalError("record with invalid length (hole len)")
                        blk.hole_length = _U16.unpack_from(body, ptr)[0]
                        ptr += 2
                        remaining -= 2
                    else:
                        blk.hole_length = 0
                else:
                    # 未压缩：洞长可推导（xlogreader.c 同款）
                    blk.hole_length = P.BLCKSZ - blk.bimg_len
                datatotal += blk.bimg_len

            if not (fork_flags & P.BKPBLOCK_SAME_REL):
                if remaining < 12:
                    raise WalError("record with invalid length (rlocator)")
                spc, db, rel = struct.unpack_from("<III", body, ptr)
                rnode = (spc, db, rel)
                ptr += 12
                remaining -= 12
                blk.rlocator = rnode
            else:
                if rnode is None:
                    raise WalError("BKPBLOCK_SAME_REL 但无前驱关系")
                blk.rlocator = rnode

            if remaining < 4:
                raise WalError("record with invalid length (blkno)")
            blk.blkno = _U32.unpack_from(body, ptr)[0]
            ptr += 4
            remaining -= 4
            blocks_by_id[block_id] = blk
        else:
            raise WalError(f"invalid block_id {block_id}")

    if remaining != datatotal:
        raise WalError(
            f"record with invalid length: remaining={remaining} datatotal={datatotal}")

    # 载荷顺序：按 block_id 升序，先 image 后 data；main data 最后
    for bid in sorted(blocks_by_id):
        blk = blocks_by_id[bid]
        if blk.has_image:
            blk.image = bytes(body[ptr:ptr + blk.bimg_len])
            if len(blk.image) != blk.bimg_len:
                raise WalError("record with invalid length (image payload)")
            ptr += blk.bimg_len
        if blk.has_data:
            blk.data = bytes(body[ptr:ptr + blk.data_len])
            if len(blk.data) != blk.data_len:
                raise WalError("record with invalid length (block payload)")
            ptr += blk.data_len
    main_data = bytes(body[ptr:ptr + main_len])
    if len(main_data) != main_len:
        raise WalError("record with invalid length (main payload)")

    return list(blocks_by_id.values()), main_data, origin_id, toplevel_xid


def restore_block_image(blk: BlockRef) -> bytes:
    """RestoreBlockImage 等价：重建 8K 页（含洞回填）。压缩镜像需调用方先解压。"""
    if blk.hole_length == 0:
        page = bytes(blk.image)
        if len(page) != P.BLCKSZ:
            raise WalError(f"image 长度异常: {len(page)}")
        return page
    hole_off, hole_len = blk.hole_offset, blk.hole_length
    src = blk.image
    if len(src) != P.BLCKSZ - hole_len:
        raise WalError(f"image 长度与洞不符: {len(src)} vs {P.BLCKSZ - hole_len}")
    page = bytearray(P.BLCKSZ)
    page[:hole_off] = src[:hole_off]
    page[hole_off + hole_len:] = src[hole_off:]
    return bytes(page)


class _Pending:
    __slots__ = ("buf", "tot_len", "start_lsn")

    def __init__(self, start_lsn: int, first_chunk: bytes, tot_len: Optional[int]):
        self.buf = bytearray(first_chunk)
        self.start_lsn = start_lsn
        self.tot_len = tot_len


class WalStream:
    """跨多个连续段文件的顺序记录流（严格校验）。"""

    def __init__(
        self,
        paths,
        profile: Optional[P.VersionProfile] = None,
        system_id: Optional[int] = None,
        seg_size: int = P.DEFAULT_WAL_SEG_SIZE,
        on_note=None,
    ):
        self.paths = [Path(p) for p in paths]
        if not self.paths:
            raise WalError("未提供 WAL 文件")
        self.profile = profile or P.get_profile(12)
        self.system_id = system_id
        self.seg_size = seg_size
        self.on_note = on_note or (lambda msg: None)
        self.stats = {
            "files": 0, "pages": 0, "records": 0, "crc_ok": 0,
            "prev_checked": 0, "continuations": 0, "orphan_continuation_bytes": 0,
            "zero_pages": 0, "xlog_switch": 0, "max_reclen": 0,
            "stopped_at": None,
        }

    def _note(self, msg: str):
        self.on_note(msg)

    @staticmethod
    def _parse_seg_name(name: str) -> Optional[tuple[int, int]]:
        stem = Path(name).name.split(".")[0]
        if len(stem) == 24 and all(c in "0123456789abcdefABCDEF" for c in stem):
            return int(stem[8:16], 16), int(stem[16:24], 16)  # (log, seg)
        return None

    # ------------------------------------------------------------------

    def iter_records(self) -> Iterator[Record]:
        prev_start: Optional[int] = None
        pending: Optional[_Pending] = None
        files = sorted(self.paths, key=lambda p: p.name.lower())
        self.stats["files"] = len(files)

        for path in files:
            logseg = self._parse_seg_name(path.name)
            if logseg is None:
                raise WalError(f"无法解析段文件名: {path.name}")
            log_id, seg_no = logseg
            seg_start_lsn = (log_id << 32) + seg_no * self.seg_size
            stop_all = False

            with open(path, "rb") as f:
                page_idx = 0
                while True:
                    page = f.read(P.XLOG_BLCKSZ)
                    if len(page) < P.XLOG_BLCKSZ:
                        if len(page) > 0:
                            self._note(f"{path.name}: 文件非整页对齐（部分归档），页 {page_idx} 停止")
                        break
                    self.stats["pages"] += 1

                    magic, info, tli, pageaddr = _PAGE_HDR.unpack_from(page, 0)
                    if magic == 0:
                        self.stats["zero_pages"] += 1
                        self._note(f"{path.name}: 零页 @page {page_idx}（已写 WAL 的终点）")
                        stop_all = True
                        break
                    if magic != self.profile.page_magic:
                        raise WalError(
                            f"{path.name} page {page_idx}: 非法 magic {magic:04X} @lsn {pageaddr:016X}")
                    if info & ~P.XLP_ALL_FLAGS:
                        raise WalError(f"{path.name} page {page_idx}: 非法 info {info:04X}")

                    is_long = bool(info & P.XLP_LONG_HEADER)
                    if is_long:
                        if page_idx != 0:
                            raise WalError(f"{path.name}: 非首页出现长页头")
                        sysid, segsz, blcksz = _LONG_EXTRA.unpack_from(page, 24)
                        if sysid and self.system_id and sysid != self.system_id:
                            raise WalError(
                                f"{path.name}: system_id 不匹配（WAL={sysid} 期望={self.system_id}）"
                                "—— 字典与 WAL 来自不同数据库")
                        if segsz != self.seg_size:
                            self._note(f"{path.name}: 段大小 {segsz} 与配置 {self.seg_size} 不一致，采用页头值")
                            self.seg_size = segsz
                        if blcksz != P.BLCKSZ:
                            raise WalError(f"{path.name}: XLOG_BLCKSZ={blcksz} 非 8192，暂不支持")
                        hdr_size = P.SIZEOF_XLOG_LONG_PHD
                    else:
                        if page_idx == 0:
                            raise WalError(f"{path.name}: 首页缺少长页头")
                        hdr_size = P.SIZEOF_XLOG_SHORT_PHD

                    expected_addr = seg_start_lsn + page_idx * P.XLOG_BLCKSZ
                    if pageaddr != expected_addr:
                        raise WalError(
                            f"{path.name} page {page_idx}: pageaddr {pageaddr:016X} "
                            f"与预期 {expected_addr:016X} 不符（文件名与内容不匹配？）")

                    rem_len = _U32.unpack_from(page, 16)[0]
                    off = hdr_size
                    is_cont = bool(info & P.XLP_FIRST_IS_CONTRECORD)

                    # ---- 续记录 ----
                    if is_cont:
                        if pending is None:
                            # 孤儿续记录：流从中途开始
                            skip = min(rem_len, P.XLOG_BLCKSZ - off)
                            self.stats["orphan_continuation_bytes"] += skip
                            self._note(
                                f"{path.name} page {page_idx}: 跳过孤儿续记录 {skip} 字节"
                                "（WAL 流从中途开始，正常）")
                            off = MAXALIGN(hdr_size + skip)
                            if off >= P.XLOG_BLCKSZ:
                                page_idx += 1
                                continue
                        else:
                            self.stats["continuations"] += 1
                            avail = P.XLOG_BLCKSZ - off
                            take = min(rem_len, avail)
                            gotlen = len(pending.buf)
                            pending.buf += page[off:off + take]
                            # 两阶段：先补齐 24B 头
                            if pending.tot_len is None and len(pending.buf) >= P.SIZEOF_XLOG_RECORD:
                                pending.tot_len = _U32.unpack_from(pending.buf, 0)[0]
                                if pending.tot_len < P.SIZEOF_XLOG_RECORD:
                                    raise WalError(
                                        f"续记录头非法 @lsn {pending.start_lsn:016X}")
                            if pending.tot_len is not None:
                                # 交叉校验（XLogReadRecord 同款）
                                if pending.tot_len != rem_len + gotlen:
                                    raise WalError(
                                        f"续记录长度矛盾 @lsn {pending.start_lsn:016X}: "
                                        f"tot_len={pending.tot_len} rem_len+got={rem_len + gotlen}")
                                if len(pending.buf) >= pending.tot_len:
                                    rec = self._finish(pending, prev_start)
                                    prev_start = rec.start_lsn
                                    pending = None
                                    yield rec
                                    off = MAXALIGN(hdr_size + rem_len)
                                else:
                                    # 整页均为续数据
                                    page_idx += 1
                                    continue
                            else:
                                # 头仍未齐（整页都是头的一部分，极罕见）
                                page_idx += 1
                                continue

                    elif pending is not None:
                        raise WalError(
                            f"{path.name} page {page_idx}: 上一条记录未完成但本页无续标志"
                            "（WAL 缺段或损坏）")

                    # ---- 常规记录 ----
                    while off < P.XLOG_BLCKSZ:
                        avail = P.XLOG_BLCKSZ - off
                        if avail < P.SIZEOF_XLOG_RECORD:
                            # 剩余空间不足完整记录头：
                            # - 若开头 4 字节是合法 tot_len(>=24) → 记录头跨页分裂，
                            #   作为 pending 进入续记录流程（XLogReadRecord 同款路径）
                            # - 否则视为页尾填充（零=正常，非零=报错）
                            if avail >= 4:
                                tl = _U32.unpack_from(page, off)[0]
                                if tl >= P.SIZEOF_XLOG_RECORD:
                                    pending = _Pending(pageaddr + off, page[off:], None)
                                    break
                            if any(page[off:]):
                                raise WalError(
                                    f"{path.name} page {page_idx} off {off}: "
                                    "页尾残留非法字节（非记录头亦非零填充）")
                            break

                        tot_len, xid, prev, rinfo, rmid, _pad, crc = _REC_HDR.unpack_from(page, off)
                        if tot_len < P.SIZEOF_XLOG_RECORD:
                            if any(page[off:]):
                                raise WalError(
                                    f"{path.name} page {page_idx} off {off}: "
                                    f"非法记录长度 {tot_len} @lsn {pageaddr + off:016X}")
                            self._note(f"{path.name} page {page_idx}: 页尾零填充（WAL 末尾）")
                            break
                        if tot_len > MAX_RECORD_BYTES:
                            raise WalError(
                                f"{path.name} page {page_idx} off {off}: 记录超长 {tot_len}")
                        if rmid > P.RM_MAX_ID:
                            raise WalError(
                                f"{path.name}: 非法 rmid {rmid} @lsn {pageaddr + off:016X}")

                        start_lsn = pageaddr + off
                        if prev_start is not None and prev != prev_start:
                            raise WalError(
                                f"prev 链断裂 @lsn {start_lsn:016X}: "
                                f"prev={prev:016X} 期望={prev_start:016X}")

                        if off + tot_len <= P.XLOG_BLCKSZ:
                            rec_bytes = page[off:off + tot_len]
                            rec = self._build(rec_bytes, start_lsn, prev_start)
                            prev_start = start_lsn
                            yield rec
                            off += MAXALIGN(tot_len)
                            if rec.is_xlog_switch():
                                self.stats["xlog_switch"] += 1
                                self._note(f"{path.name}: XLOG_SWITCH，跳至下一段")
                                break  # 本段剩余为零
                        else:
                            pending = _Pending(start_lsn, page[off:], tot_len)
                            break

                    page_idx += 1

            # 注意：pending 允许跨段携带（记录起始于本段末尾、续体在下一段首页）
            if stop_all:
                self.stats["stopped_at"] = str(path)
                break

        if pending is not None:
            # 流在记录中间被截断（段集合不完整）——与开头的孤儿续记录对称，属正常
            self._note(
                f"流末尾记录未完成（起始于 0x{pending.start_lsn:016X}，"
                "段集合截断所致，如需该记录请补充后续段文件）")
            self.stats["truncated_tail"] = True

    # ------------------------------------------------------------------

    def _build(self, rec_bytes: bytes, start_lsn: int, prev_start) -> Record:
        tot_len, xid, prev, rinfo, rmid, _pad, crc = _REC_HDR.unpack_from(rec_bytes, 0)
        # CRC：body || header[0:20]（ValidXLogRecord 同款）
        calc = crc32c(rec_bytes[24:tot_len] + rec_bytes[0:20])
        if calc != crc:
            raise WalError(
                f"CRC 校验失败 @lsn {start_lsn:016X}: 计算={calc:08X} 存储={crc:08X}")
        self.stats["crc_ok"] += 1
        if prev_start is not None:
            self.stats["prev_checked"] += 1
        self.stats["records"] += 1
        self.stats["max_reclen"] = max(self.stats["max_reclen"], tot_len)

        body = rec_bytes[P.SIZEOF_XLOG_RECORD:tot_len]
        try:
            blocks, main_data, origin_id, toplevel_xid = decode_record_body(body, self.profile)
        except WalError as e:
            raise WalError(f"{e} @lsn {start_lsn:016X}") from None

        return Record(
            start_lsn=start_lsn,
            end_lsn=start_lsn + tot_len,
            tot_len=tot_len,
            xid=xid,
            prev=prev,
            info=rinfo,
            rmid=rmid,
            crc=crc,
            blocks=blocks,
            main_data=main_data,
            origin_id=origin_id,
            toplevel_xid=toplevel_xid,
        )

    def _finish(self, pending: _Pending, prev_start) -> Record:
        buf = bytes(pending.buf[:pending.tot_len])
        if len(buf) != pending.tot_len:
            raise WalError(f"续记录拼装失败 @lsn {pending.start_lsn:016X}")
        return self._build(buf, pending.start_lsn, prev_start)


def collect_wal_files(path) -> list:
    """目录或文件 → 排序后的段文件列表。"""
    path = Path(path)

    def is_wal_name(name: str) -> bool:
        stem = name.split(".")[0]
        return len(stem) == 24 and all(c in "0123456789abcdefABCDEF" for c in stem)

    if path.is_file():
        return [path] if is_wal_name(path.name) or path.suffix in (".wal", ".partial") else []
    if not path.is_dir():
        return []
    files = [p for p in path.iterdir()
             if p.is_file() and (is_wal_name(p.name) or p.suffix in (".wal", ".partial"))]
    return sorted(files, key=lambda p: p.name.lower())
