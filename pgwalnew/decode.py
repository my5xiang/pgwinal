"""Heap DML 解码 + 页像重放：WAL 记录 → Change（含新旧元组与解码值）。

布局依据（全部对照官方头文件 + walminer 参考实现核实）：
  xl_heap_insert   : offnum u16 + flags u8；元组在 block0 data（xl_heap_header 5B + data）
  xl_heap_delete   : xmax u32 + offnum u16 + infobits u8 + flags u8；
                     旧元组（CONTAINS_OLD 时）紧随 main 偏移 8（无对齐）
  xl_heap_update   : old_xmax u32 + old_offnum u16 + old_infobits u8 + flags u8
                     + new_xmax u32 + new_offnum u16（14B）；
                     旧元组（CONTAINS_OLD 时）紧随 main 偏移 14；
                     新元组在 block0 data：[prefix u16][suffix u16] xl_heap_header + data
  xl_heap_multi_insert: flags u8 + ntuples u16 + offsets u16[]（INIT_PAGE 时省略）；
                     block data 内逐元组：SHORTALIGN + 7B 头 + datalen 数据
  xl_heap_clean    : main = latestRemovedXid u32 + nredirected u16 + ndead u16；
                     block data = redirected 对 + nowdead + nowunused

页像重放（对齐 walminer imagemanage）：
  FPI = 变更后页状态；带 FPI 的记录直接从镜像读元组；
  无 FPI 的 DML 向页库 redo 新元组；CLEAN 应用行指针变更 + 碎片整理。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from . import profiles as P
from .dictstore import DataDictionary, RelationDef
from .heaptuple import TupleData, deform, is_clean, read_varlena
from .pageredo import (PageStore, page_add_item, page_apply_prune,
                       read_tuple_from_page, restore_image)
from .pglz import pglz_decompress
from .typereg import ExternalValue, RawValue

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
PG_EPOCH = datetime(2000, 1, 1)


def SHORTALIGN(x: int) -> int:
    return (x + 1) & ~1


@dataclass
class Change:
    lsn: int
    xid: int
    op: str                      # INSERT / UPDATE / DELETE / MULTI_INSERT
    rel: Optional[RelationDef]
    relfilenode: int
    db_oid: int
    block: int
    offset: int
    new_values: dict = field(default_factory=dict)
    old_values: dict = field(default_factory=dict)
    undo_source: str = "none"    # record / history / fpi / incomplete / none
    notes: list = field(default_factory=list)
    commit_ts: Optional[datetime] = None
    topxid: int = 0

    @property
    def executable(self) -> bool:
        return bool(self.rel) and is_clean(self.new_values) and is_clean(self.old_values)


def commit_ts_from_record(rec) -> Optional[datetime]:
    """xl_xact_commit 首字段 xact_time：2000 纪元微秒。"""
    if len(rec.main_data) < 8:
        return None
    us = struct.unpack_from("<q", rec.main_data, 0)[0]
    return PG_EPOCH + timedelta(microseconds=us)


def parse_xact_assignment(rec) -> list:
    """XLOG_XACT_ASSIGNMENT：xid u32 + nsubxacts u32 + subxacts[] → [(sub, top), ...]"""
    md = rec.main_data
    if len(md) < 8:
        return []
    xid, nsub = struct.unpack_from("<II", md, 0)
    out = []
    for i in range(nsub):
        if 8 + 4 * (i + 1) > len(md):
            break
        out.append((_U32.unpack_from(md, 8 + 4 * i)[0], xid))
    return out


def full_tuple_bytes(payload: bytes, xmin: int, xmax: int) -> bytes:
    """WAL 元组（5B 头）→ 完整 23B 头元组（供页库 redo）。"""
    im2, im, hoff = struct.unpack_from("<HHB", payload, 0)
    header = bytearray(23)
    struct.pack_into("<II", header, 0, xmin & 0xFFFFFFFF, xmax & 0xFFFFFFFF)
    struct.pack_into("<HHB", header, 18, im2, im, hoff)
    return bytes(header) + payload[5:]


class HeapDecoder:
    def __init__(self, dictionary: DataDictionary, page_store: Optional[PageStore] = None,
                 profile=None, max_pages: int = 60000):
        self.dict = dictionary
        self.profile = profile or P.get_profile(dictionary.major or 12)
        self.pages = page_store or PageStore(max_pages=max_pages)
        self.history: dict = {}   # (db, relfilenode, blk, offnum) → TupleData（流内历史）
        # TOAST chunk 库：(db, toast_relfilenode, chunk_id) → {chunk_seq: bytes}
        self.toast_chunks: dict = {}
        self.stats = {
            "insert": 0, "update": 0, "hot_update": 0, "delete": 0, "multi_insert": 0,
            "unresolved_rel": 0, "fpi_tuple_read": 0, "old_from_history": 0,
            "old_from_page": 0, "prefix_suffix": 0, "decode_errors": 0,
            "redo_add": 0, "prune_applied": 0, "prune_skipped": 0,
            "toast_chunks": 0, "toast_resolved": 0, "toast_unresolved": 0,
        }

    # ------------------------------------------------------------------
    # 页像重放入口（每条 heap/heap2/xlog-FPI 记录调用）

    def _seed_images(self, rec) -> set:
        """FPI 播种：返回本记录播种了页像的 block_id 集合。"""
        seeded = set()
        for blk in rec.blocks:
            if not blk.has_image or blk.forknum != 0:
                continue
            spc, db, relno = blk.rlocator
            if self.dict.find_by_relfilenode(db, relno) is None:
                continue
            page = restore_image(blk, self.profile)
            if page is not None:
                self.pages.put((db, relno, blk.blkno), page)
                seeded.add(blk.block_id)
        return seeded

    def on_record(self, rec) -> set:
        """记录级页像处理：FPI 播种 + CLEAN 应用。返回播种集合。"""
        seeded = self._seed_images(rec)
        if rec.rmid == P.RM_HEAP2_ID:
            op = rec.info & P.XLOG_HEAP_OPMASK
            if op == self.profile.heap2.get("CLEAN", -1):
                blk = rec.block(0)
                if blk is None or 0 in seeded:
                    return seeded
                key = (blk.rlocator[1], blk.rlocator[2], blk.blkno)
                page = self.pages.get(key)
                if page is None:
                    self.stats["prune_skipped"] += 1
                    return seeded
                md = rec.main_data
                if len(md) < 8:
                    return seeded
                nredirected, ndead = struct.unpack_from("<HH", md, 4)
                data = blk.data or b""
                # redirected：每项 2 个 OffsetNumber（from,to）= 4 字节
                pos = nredirected * 4
                redirected = list(struct.unpack_from(
                    f"<{nredirected * 2}H", data, 0)) if pos <= len(data) else []
                nowdead = list(struct.unpack_from(
                    f"<{ndead}H", data, pos)) if ndead and pos + ndead * 2 <= len(data) else []
                pos += ndead * 2
                nunused = max(0, (len(data) - pos) // 2)
                nowunused = list(struct.unpack_from(
                    f"<{nunused}H", data, pos)) if nunused else []
                page_apply_prune(page, redirected, nowdead, nowunused)
                self.stats["prune_applied"] += 1
        return seeded

    def _redo_tuple(self, rec, blk, offnum: int, tuple_bytes: bytes):
        """把新元组 redo 进页库（页已在库时）。"""
        spc, db, relno = blk.rlocator
        page = self.pages.get((db, relno, blk.blkno))
        if page is None:
            return
        try:
            page_add_item(page, tuple_bytes, offnum)
            self.stats["redo_add"] += 1
        except Exception:
            pass  # 页不在库或空间异常：跳过 redo，不影响解码

    # ------------------------------------------------------------------

    def _resolve(self, rec, block_id=0):
        blk = rec.block(block_id)
        if blk is None:
            return None, None
        spc, db, rel = blk.rlocator
        reldef = self.dict.find_by_relfilenode(db, rel)
        return blk, reldef

    def _remember(self, db, relfilenode, blkno, offnum, td: TupleData):
        self.history[(db, relfilenode, blkno, offnum)] = td

    def _lookup_history(self, db, relfilenode, blkno, offnum) -> Optional[TupleData]:
        return self.history.get((db, relfilenode, blkno, offnum))

    def _tuple_from_page(self, db, relno, blkno, offnum) -> Optional[TupleData]:
        page = self.pages.get((db, relno, blkno))
        if page is None:
            return None
        tup = read_tuple_from_page(bytes(page), offnum)
        if tup is None:
            return None
        try:
            return TupleData.from_page_tuple(tup)
        except ValueError:
            return None

    def redo_physical(self, rec, seeded: set):
        """仅物理重放（用于未提交/已回滚事务的记录）。

        页是物理的：未提交事务的 FPI 与新元组同样改变页状态，
        跳过会导致页像漂移、后续旧值取错（对齐 walminer record_store_image
        对每条记录执行的语义）。不收集变更、不写流内历史。
        """
        try:
            blk = rec.block(0)
            if blk is None or not (blk.has_data and blk.data) or 0 in seeded:
                return
            spc, db, relno = blk.rlocator
            if self.dict.find_by_relfilenode(db, relno) is None:
                return
            md = rec.main_data
            if rec.rmid == P.RM_HEAP_ID:
                op = rec.info & P.XLOG_HEAP_OPMASK
                if op == P.XLOG_HEAP_INSERT and len(md) >= 3:
                    offnum = _U16.unpack_from(md, 0)[0]
                    self._redo_tuple(rec, blk, offnum,
                                     full_tuple_bytes(blk.data, rec.xid, 0))
                elif op in (P.XLOG_HEAP_UPDATE, P.XLOG_HEAP_HOT_UPDATE) and len(md) >= 14:
                    flags = md[7]
                    new_offnum = struct.unpack_from("<H", md, 12)[0]
                    if not (flags & 0x60):  # prefix/suffix 差量需旧元组，此处跳过
                        self._redo_tuple(rec, blk, new_offnum,
                                         full_tuple_bytes(blk.data, rec.xid, 0))
            elif rec.rmid == P.RM_HEAP2_ID:
                if (rec.info & P.XLOG_HEAP_OPMASK) == self.profile.heap2_multi_insert \
                        and len(md) >= 3:
                    ntuples = _U16.unpack_from(md, 1)[0]
                    is_init = bool(rec.info & P.XLOG_HEAP_INIT_PAGE)
                    offsets = (list(range(1, ntuples + 1)) if is_init else
                               [_U16.unpack_from(md, 3 + 2 * i)[0]
                                for i in range(ntuples)
                                if 3 + 2 * (i + 1) <= len(md)])
                    data = blk.data
                    pos = 0
                    for i in range(min(ntuples, len(offsets))):
                        pos = SHORTALIGN(pos)
                        if pos + 7 > len(data):
                            break
                        datalen, infomask2, infomask, hoff = struct.unpack_from(
                            "<HHHB", data, pos)
                        pos += 7
                        chunk = data[pos:pos + datalen]
                        pos += datalen
                        header = bytearray(23)
                        struct.pack_into("<II", header, 0, rec.xid & 0xFFFFFFFF, 0)
                        struct.pack_into("<HHB", header, 18, infomask2, infomask, hoff)
                        self._redo_tuple(rec, blk, offsets[i], bytes(header) + chunk)
        except Exception:
            pass

    # ------------------------------------------------------------------

    def decode(self, rec, seeded: Optional[set] = None) -> list:
        """解码一条 Heap/Heap2 记录 → [Change]。"""
        seeded = seeded if seeded is not None else set()
        try:
            # toast 表记录：收集 chunk 而非产出 DML
            blk0 = rec.block(0)
            if blk0 is not None:
                spc, db0, relno0 = blk0.rlocator
                rel0 = self.dict.find_by_relfilenode(db0, relno0)
                if rel0 is not None and rel0.relkind == "t":
                    self._collect_toast_chunk(rec, rel0, seeded)
                    return []

            if rec.rmid == P.RM_HEAP_ID:
                op = rec.info & P.XLOG_HEAP_OPMASK
                if op == P.XLOG_HEAP_INSERT:
                    ch = self._insert(rec, seeded)
                    return [ch] if ch else []
                elif op in (P.XLOG_HEAP_UPDATE, P.XLOG_HEAP_HOT_UPDATE):
                    ch = self._update(rec, hot=(op == P.XLOG_HEAP_HOT_UPDATE), seeded=seeded)
                    return [ch] if ch else []
                elif op == P.XLOG_HEAP_DELETE:
                    ch = self._delete(rec, seeded)
                    return [ch] if ch else []
            elif rec.rmid == P.RM_HEAP2_ID:
                op = rec.info & P.XLOG_HEAP_OPMASK
                if op == self.profile.heap2_multi_insert:
                    return self._multi_insert(rec, seeded)
        except Exception as e:
            self.stats["decode_errors"] += 1
            return [Change(
                lsn=rec.start_lsn, xid=rec.xid, op="ERROR", rel=None,
                relfilenode=0, db_oid=0, block=0, offset=0,
                notes=[f"解码异常: {type(e).__name__}: {e}"])]
        return []

    # ------------------------------------------------------------------
    # TOAST 重组

    def _collect_toast_chunk(self, rec, reldef, seeded) -> bool:
        """toast 表（relkind='t'）的 heap 记录 → 收集 chunk。"""
        blk = rec.block(0)
        if blk is None or not (blk.has_data and blk.data):
            return False
        spc, db, relno = blk.rlocator
        md = rec.main_data
        if len(md) < 8:
            return False
        # toast 元组固定三列：chunk_id(oid,4B) chunk_seq(int4) chunk_data(bytea)
        payload = blk.data
        if len(payload) < 5:
            return False
        td = TupleData.from_wal_payload(payload)
        data = td.data
        user = td.hoff - 23
        if user < 0 or user + 8 > len(data):
            return False
        chunk_id = _U32.unpack_from(data, user)[0]
        chunk_seq = struct.unpack_from("<i", data, user + 4)[0]
        # chunk_data：varlena（对齐窥视）
        pos = user + 8
        if pos < len(data) and data[pos] == 0:
            pos = (pos + 3) & ~3
        kind, payload_data, _ = read_varlena(data, pos)
        if kind in ("1b", "4b") and isinstance(payload_data, bytes):
            self.toast_chunks.setdefault((db, relno, chunk_id), {})[chunk_seq] = payload_data
            self.stats["toast_chunks"] += 1
        return True

    def _resolve_external(self, db: int, att, ext) -> object:
        """ExternalValue → 解码值（chunk 重组 + pglz）。"""
        rel = self.dict.find_by_oid(ext.toastrelid)
        if rel is None:
            self.stats["toast_unresolved"] += 1
            return RawValue(b"", f"TOAST 关系 {ext.toastrelid} 不在字典", "external")
        chunks = self.toast_chunks.get((db, rel.relfilenode, ext.valueid))
        if not chunks:
            self.stats["toast_unresolved"] += 1
            return RawValue(b"", f"TOAST chunk 缺失（valueid={ext.valueid}）", "external")
        raw = b"".join(chunks[k] for k in sorted(chunks))
        if ext.extsize < ext.rawsize - 4:
            try:
                raw = pglz_decompress(raw, ext.rawsize - 4)
            except Exception as e:
                self.stats["toast_unresolved"] += 1
                return RawValue(raw, f"TOAST pglz 解压失败: {e}", "compressed")
        self.stats["toast_resolved"] += 1
        from .typereg import type_info
        info = type_info(att.type_oid, att.type_name)
        if info and info[3]:  # varlena 解码器
            try:
                return info[2](raw)
            except Exception:
                pass
        return RawValue(raw, "TOAST 重组后类型未注册", "raw")

    def _resolve_toast_values(self, db: int, values: dict, attrs):
        """把 values 中的 ExternalValue 替换为重组值。"""
        for att in attrs:
            v = values.get(att.name)
            if isinstance(v, ExternalValue):
                values[att.name] = self._resolve_external(db, att, v)

    # ------------------------------------------------------------------

    def _insert(self, rec, seeded: set) -> Optional[Change]:
        blk, reldef = self._resolve(rec, 0)
        if blk is None:
            return None
        spc, db, relno = blk.rlocator
        if reldef is None:
            self.stats["unresolved_rel"] += 1
            return None

        md = rec.main_data
        if len(md) < 3:
            return None
        offnum = _U16.unpack_from(md, 0)[0]
        flags = md[2]

        notes: list = []
        if blk.has_data and blk.data:
            td = TupleData.from_wal_payload(blk.data)
            if 0 not in seeded:
                self._redo_tuple(rec, blk, offnum, full_tuple_bytes(blk.data, rec.xid, 0))
        elif 0 in seeded:
            # 元组在 FPI 页像中（FPI = 变更后状态）
            page = self.pages.get((db, relno, blk.blkno))
            tup = read_tuple_from_page(bytes(page), offnum) if page else None
            if tup is None:
                return None
            td = TupleData.from_page_tuple(tup)
            self.stats["fpi_tuple_read"] += 1
            notes.append("元组取自 FPI 页像")
        else:
            self.stats["fpi_only"] = 0  # 不应发生：有 data 或有 image 二者必居其一
            return None

        values, issues = deform(td, reldef.attrs, pglz_decompress)
        self._resolve_toast_values(db, values, reldef.attrs)
        self._remember(db, relno, blk.blkno, offnum, td)
        self.stats["insert"] += 1
        return Change(
            lsn=rec.start_lsn, xid=rec.xid, op="INSERT", rel=reldef,
            relfilenode=relno, db_oid=db, block=blk.blkno, offset=offnum,
            new_values=values, undo_source="record", notes=notes + issues)

    # ------------------------------------------------------------------

    def _delete(self, rec, seeded: set) -> Optional[Change]:
        blk, reldef = self._resolve(rec, 0)
        if blk is None:
            return None
        spc, db, relno = blk.rlocator
        if reldef is None:
            self.stats["unresolved_rel"] += 1
            return None

        md = rec.main_data
        if len(md) < 8:
            return None
        xmax, offnum, infobits, flags = struct.unpack_from("<IHBB", md, 0)

        old_values: dict = {}
        undo_source = "none"
        notes: list = []

        if flags & P.XLH_DELETE_CONTAINS_OLD:
            if len(md) > 8 + 5:
                td_old = TupleData.from_wal_payload(md[8:])
                old_values, issues = deform(td_old, reldef.attrs, pglz_decompress)
                undo_source = "record"
                notes.extend(issues)
        else:
            td_old = self._lookup_history(db, relno, blk.blkno, offnum)
            src = "history"
            if td_old is None:
                td_old = self._tuple_from_page(db, relno, blk.blkno, offnum)
                src = "fpi"
            if td_old is not None:
                old_values, issues = deform(td_old, reldef.attrs, pglz_decompress)
                undo_source = src
                notes.append(f"UNDO 旧值来自{'流内历史' if src == 'history' else '页像重放'}元组")
                self.stats["old_from_history" if src == "history" else "old_from_page"] += 1
            else:
                undo_source = "incomplete"
                notes.append("DELETE 无旧元组且页像/历史均无（UNDO 不完整）")

        self.stats["delete"] += 1
        return Change(
            lsn=rec.start_lsn, xid=rec.xid, op="DELETE", rel=reldef,
            relfilenode=relno, db_oid=db, block=blk.blkno, offset=offnum,
            old_values=old_values, undo_source=undo_source, notes=notes)

    # ------------------------------------------------------------------

    def _update(self, rec, hot: bool, seeded: set) -> Optional[Change]:
        blk, reldef = self._resolve(rec, 0)
        if blk is None:
            return None
        spc, db, relno = blk.rlocator
        if reldef is None:
            self.stats["unresolved_rel"] += 1
            return None

        md = rec.main_data
        if len(md) < 14:
            return None
        (old_xmax, old_offnum, old_infobits, flags,
         new_xmax, new_offnum) = struct.unpack_from("<IHBBIH", md, 0)

        notes: list = []
        if hot:
            notes.append("HOT_UPDATE")

        # ---- 旧元组 ----
        old_values: dict = {}
        old_td = None
        old_blkno = blk.blkno
        blk1 = rec.block(1)
        if blk1 is not None:
            old_blkno = blk1.blkno

        if flags & P.XLH_UPDATE_CONTAINS_OLD:
            if len(md) > 14 + 5:
                old_td = TupleData.from_wal_payload(md[14:])
                old_values, issues = deform(old_td, reldef.attrs, pglz_decompress)
                notes.extend(issues)
        else:
            old_td = self._lookup_history(db, relno, old_blkno, old_offnum)
            if old_td is not None:
                notes.append("UNDO 旧值来自流内历史元组")
                self.stats["old_from_history"] += 1
            else:
                old_td = self._tuple_from_page(db, relno, old_blkno, old_offnum)
                if old_td is not None:
                    notes.append("UNDO 旧值来自页像重放")
                    self.stats["old_from_page"] += 1
            if old_td is not None:
                old_values, issues = deform(old_td, reldef.attrs, pglz_decompress)
                notes.extend(issues)

        # ---- 新元组（block 0 data 或 FPI）----
        new_values: dict = {}
        new_td = None
        if blk.has_data and blk.data:
            data = blk.data
            pos = 0
            prefixlen = suffixlen = 0
            if flags & P.XLH_UPDATE_PREFIX_FROM_OLD:
                prefixlen = _U16.unpack_from(data, 0)[0]
                pos += 2
            if flags & P.XLH_UPDATE_SUFFIX_FROM_OLD:
                suffixlen = _U16.unpack_from(data, pos)[0]
                pos += 2
            if len(data) < pos + 5:
                return None
            td_new = TupleData.from_wal_payload(data[pos:])

            if prefixlen or suffixlen:
                self.stats["prefix_suffix"] += 1
                src_old = old_td
                if src_old is None:
                    src_old = self._tuple_from_page(db, relno, old_blkno, old_offnum)
                if src_old is None:
                    notes.append("prefix/suffix 差量但旧元组不可得（DO 不完整）")
                else:
                    if old_td is None:
                        old_td = src_old
                        old_values, _ = deform(old_td, reldef.attrs, pglz_decompress)
                    td_new = self._apply_prefix_suffix(td_new, src_old, prefixlen, suffixlen)
                    notes.append(f"prefix={prefixlen}B suffix={suffixlen}B 差量重组")
            new_td = td_new
            new_values, issues = deform(td_new, reldef.attrs, pglz_decompress)
            self._resolve_toast_values(db, new_values, reldef.attrs)
            notes.extend(issues)
            if 0 not in seeded:
                self._redo_tuple(rec, blk, new_offnum,
                                 full_tuple_bytes(blk.data, rec.xid, new_xmax))
        elif 0 in seeded:
            page = self.pages.get((db, relno, blk.blkno))
            tup = read_tuple_from_page(bytes(page), new_offnum) if page else None
            if tup is not None:
                new_td = TupleData.from_page_tuple(tup)
                new_values, issues = deform(new_td, reldef.attrs, pglz_decompress)
                notes.extend(issues)
                self.stats["fpi_tuple_read"] += 1
                notes.append("新元组取自 FPI 页像")
        else:
            notes.append("UPDATE 无新元组数据")

        undo_source = "record" if (flags & P.XLH_UPDATE_CONTAINS_OLD) else (
            "history" if old_td is not None else "incomplete")

        if new_td is not None:
            self._remember(db, relno, blk.blkno, new_offnum, new_td)

        self.stats["hot_update" if hot else "update"] += 1
        return Change(
            lsn=rec.start_lsn, xid=rec.xid, op="UPDATE", rel=reldef,
            relfilenode=relno, db_oid=db, block=blk.blkno, offset=new_offnum,
            new_values=new_values, old_values=old_values,
            undo_source=undo_source, notes=notes)

    @staticmethod
    def _apply_prefix_suffix(td_new: TupleData, td_old: TupleData,
                             prefixlen: int, suffixlen: int) -> TupleData:
        """walminer reassemble_tuplenew_from_wal_data 等价实现。

        WAL 新元组数据 = [bitmap/padding (hoff-23)B] + [prefix 之后的数据]；
        完整新元组 = WAL[0:hoff-23] + 旧元组用户数据[:prefixlen]
                     + WAL[hoff-23:] + 旧元组尾部[suffixlen:]
        """
        new_payload = td_new.data
        hoff = td_new.hoff
        bitmap_len = hoff - 23
        head = new_payload[:bitmap_len]
        mid = new_payload[bitmap_len:]

        old_user = td_old.data[td_old.hoff - 23:]
        prefix = old_user[:prefixlen]
        suffix = old_user[len(old_user) - suffixlen:] if suffixlen else b""

        combined = head + prefix + mid + suffix
        return TupleData(td_new.infomask2, td_new.infomask, hoff, combined)

    # ------------------------------------------------------------------

    def _multi_insert(self, rec, seeded: set) -> list:
        blk, reldef = self._resolve(rec, 0)
        if blk is None:
            return []
        spc, db, relno = blk.rlocator
        if reldef is None:
            self.stats["unresolved_rel"] += 1
            return []

        md = rec.main_data
        if len(md) < 3:
            return []
        flags = md[0]
        ntuples = _U16.unpack_from(md, 1)[0]
        is_init = bool(rec.info & P.XLOG_HEAP_INIT_PAGE)

        offsets = []
        if is_init:
            offsets = list(range(1, ntuples + 1))
        else:
            for i in range(ntuples):
                if 3 + 2 * (i + 1) > len(md):
                    break
                offsets.append(_U16.unpack_from(md, 3 + 2 * i)[0])

        out: list = []
        if blk.has_data and blk.data:
            data = blk.data
            pos = 0
            for i in range(min(ntuples, len(offsets))):
                offnum = offsets[i]
                pos = SHORTALIGN(pos)
                if pos + 7 > len(data):
                    break
                datalen, infomask2, infomask, hoff = struct.unpack_from("<HHHB", data, pos)
                pos += 7
                chunk = data[pos:pos + datalen]
                pos += datalen
                td = TupleData(infomask2, infomask, hoff, chunk)
                if 0 not in seeded:
                    header = bytearray(23)
                    struct.pack_into("<II", header, 0, rec.xid & 0xFFFFFFFF, 0)
                    struct.pack_into("<HHB", header, 18, infomask2, infomask, hoff)
                    self._redo_tuple(rec, blk, offnum, bytes(header) + chunk)
                values, issues = deform(td, reldef.attrs, pglz_decompress)
                self._resolve_toast_values(db, values, reldef.attrs)
                self._remember(db, relno, blk.blkno, offnum, td)
                self.stats["multi_insert"] += 1
                out.append(Change(
                    lsn=rec.start_lsn, xid=rec.xid, op="MULTI_INSERT", rel=reldef,
                    relfilenode=relno, db_oid=db, block=blk.blkno, offset=offnum,
                    new_values=values, undo_source="record", notes=issues))
        elif 0 in seeded:
            page = self.pages.get((db, relno, blk.blkno))
            for i in range(min(ntuples, len(offsets))):
                offnum = offsets[i]
                tup = read_tuple_from_page(bytes(page), offnum) if page else None
                if tup is None:
                    continue
                td = TupleData.from_page_tuple(tup)
                values, issues = deform(td, reldef.attrs, pglz_decompress)
                self._remember(db, relno, blk.blkno, offnum, td)
                self.stats["multi_insert"] += 1
                self.stats["fpi_tuple_read"] += 1
                out.append(Change(
                    lsn=rec.start_lsn, xid=rec.xid, op="MULTI_INSERT", rel=reldef,
                    relfilenode=relno, db_oid=db, block=blk.blkno, offset=offnum,
                    new_values=values, undo_source="record",
                    notes=["元组取自 FPI 页像"] + issues))
        return out
